"""好感度系统插件 — MaiBot SDK v2

为 MaiBot 角色扮演提供好感度追踪与长期记忆功能。

功能特性：
    - 记录不同用户与角色的好感度值（范围 -20 ~ 100）
    - 六级好感度划分：不喜 / 正常 / 熟悉 / 亲近 / 亲密 / 思慕
    - 根据角色性格和用户行为动态调整好感度
    - 不同好感度等级对应不同的回应风格提示
    - 好感度数据持久化存储于插件数据目录
    - 支持 WebUI 可视化配置与管理：
        * 「好感度管理」配置节以可编辑表格展示各用户好感度
        * 在 WebUI 中可直接查看并手动修改好感度，保存后立即生效
        * 锁定条目将禁止 LLM 自动调整
        * 运行时变化会自动同步回配置表，保证界面展示最新数值
    - 提供 Tool 供 LLM Agent 查询和调整好感度
    - 提供 /affection 命令供用户查询自己的好感度

用法示例：
    - LLM 调用 query_affection 工具：获取指定用户的好感度及回应风格提示
    - LLM 调用 adjust_affection 工具：根据交互质量调整好感度
    - 用户发送 /affection 命令：查看自己与角色的好感度
    - 在 WebUI 插件配置页的「好感度管理」表格中：查看/新增/修改/删除各用户好感度
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import tomlkit

from maibot_sdk import Command, HookHandler, MaiBotPlugin, Tool
from maibot_sdk.types import ErrorPolicy, HookMode, HookOrder, ToolParameterInfo, ToolParamType

from .config import AffectionConfig

# ════════════════════════════════════════════════════════════════
# 好感度等级定义 (最小值, 最大值, 等级名称, 等级描述)
# ════════════════════════════════════════════════════════════════

AFFECTION_LEVELS: list[tuple[int, int, str, str]] = [
    (-20, 0, "不喜", "对你没什么好感，态度冷淡疏远"),
    (0, 10, "正常", "普普通通的认识关系，礼貌相待"),
    (10, 20, "熟悉", "已经比较熟悉了，能自然交流"),
    (20, 40, "亲近", "关系亲近，愿意多聊天分享"),
    (40, 70, "亲密", "亲密无间，很信任你"),
    (70, 100, "思慕", "对你有着特别的依恋与情感"),
]

# ════════════════════════════════════════════════════════════════
# 性格类型对应的好感度倍率映射
# ════════════════════════════════════════════════════════════════

PERSONALITY_MULTIPLIERS: dict[str, float] = {
    "默认": 1.0,
    "傲娇": 0.6,
    "温柔": 1.0,
    "高冷": 0.4,
    "开朗": 1.5,
    "毒舌": 0.7,
}


def _get_affection_level_info(affection_value: float) -> dict[str, Any]:
    """根据好感度数值获取等级信息。

    Args:
        affection_value: 好感度数值

    Returns:
        包含等级名称、描述、范围的字典
    """
    clamped = max(-20.0, min(100.0, affection_value))
    for min_val, max_val, name, description in AFFECTION_LEVELS:
        if min_val <= clamped <= max_val:
            # 计算在该等级内的进度百分比
            progress = (clamped - min_val) / (max_val - min_val) if max_val > min_val else 0
            return {
                "value": round(clamped, 1),
                "level_name": name,
                "level_description": description,
                "level_min": min_val,
                "level_max": max_val,
                "level_progress": round(progress * 100, 1),
            }
    # 兜底：理论上不会到这里
    return {
        "value": round(clamped, 1),
        "level_name": "未知",
        "level_description": "无法确定",
        "level_min": -20,
        "level_max": 100,
        "level_progress": 0,
    }


class AffectionPlugin(MaiBotPlugin):
    """好感度系统插件主类。

    负责好感度数据的加载、持久化、查询和修改。
    """

    config_model = AffectionConfig

    def __init__(self) -> None:
        super().__init__()
        # 好感度数据：{ user_id: { affection: float, history: list, last_updated: str, daily_count: int, last_modified_date: str } }
        self._affection_data: dict[str, dict[str, Any]] = {}
        # 冷却时间记录：{ user_id: float (timestamp) }
        self._cool_down_map: dict[str, float] = {}
        # 数据文件路径（在 on_load 中初始化）
        self._data_file_path: str = ""
        # 文件写入锁，避免并发写入冲突
        self._file_lock: asyncio.Lock = asyncio.Lock()
        # 同步配置表时的去重记录：避免重复写入 config.toml
        self._last_config_sync_key: str = ""
        # 会话 → 最近发言用户映射缓存：{ session_id: user_id }
        # 群聊流的 user_id 为 None，无法从 stream 直接反查；通过在 Tool 调用时
        # 记录「会话 ↔ 权威 QQ 号」映射，供注入 Hook 使用。
        self._session_user_map: dict[str, str] = {}

    # ════════════════════════════════════════════════════════════════
    # 生命周期方法
    # ════════════════════════════════════════════════════════════════

    async def on_load(self) -> None:
        """插件加载时初始化数据文件并读取已有数据。"""
        # 确定数据文件路径
        data_dir = self.ctx.paths.data_dir
        os.makedirs(data_dir, exist_ok=True)
        self._data_file_path = os.path.join(data_dir, "affection_data.json")
        # 迁移旧版插件 ID 的数据目录（避免改 ID 后历史数据丢失）
        await self._migrate_legacy_data_dir()
        # 加载已有数据
        await self._load_data()
        # 迁移历史昵称记录到 QQ 号（避免同一人被拆成两条记录）
        await self._migrate_nickname_records()
        # 执行衰减检查
        await self._apply_decay_check()
        # 将运行时数据同步到 config.toml，让 WebUI 管理表展示最新好感度
        await self._sync_runtime_to_config()
        self.ctx.logger.info(
            "好感度系统插件已加载 (角色: %s, 已记录 %d 位用户的好感度)",
            self.config.personality.character_name if self.config else "未知",
            len(self._affection_data),
        )

    async def on_unload(self) -> None:
        """插件卸载时确保数据写入磁盘，并将运行时数据同步回配置。"""
        await self._sync_runtime_to_config()
        await self._save_data()
        self.ctx.logger.info("好感度系统插件已卸载，数据已保存")

    async def on_config_update(
        self, scope: str, config_data: dict[str, object], version: str
    ) -> None:
        """配置热更新时处理。

        当用户通过 WebUI 修改「好感度管理」表后，Host 会把新配置写回
        config.toml 并触发本回调。这里把 WebUI 中手动修改的好感度
        同步到运行时数据（即「修改生效」的方向）。
        """
        del version
        self.ctx.logger.info(
            "好感度系统配置已热更新, scope=%s", scope
        )
        if scope == "self":
            await self._sync_config_to_runtime(config_data)

    # ════════════════════════════════════════════════════════════════
    # 数据持久化（JSON 文件方式）
    # ════════════════════════════════════════════════════════════════

    async def _migrate_legacy_data_dir(self) -> None:
        """将旧版插件 ID 的数据目录迁移到当前数据目录。

        早期版本插件 ID 首字母大写（Feyumy.affection-system），Host 据此分配的
        数据目录与当前小写 ID（feyumy.affection-system）不同。这里在首次以新 ID
        加载时，把旧目录中的 affection_data.json 复制到新目录，避免历史好感度丢失。
        仅在当前数据文件不存在且旧数据存在时执行，迁移后不做删除，保证安全。
        """
        if not self._data_file_path:
            return
        if os.path.exists(self._data_file_path):
            return
        data_dir = os.path.dirname(self._data_file_path)
        legacy_dir = os.path.join(os.path.dirname(data_dir), "Feyumy.affection-system")
        legacy_file = os.path.join(legacy_dir, "affection_data.json")
        if not os.path.exists(legacy_file):
            return
        try:
            os.makedirs(data_dir, exist_ok=True)
            loop = asyncio.get_running_loop()

            def _copy() -> None:
                with open(legacy_file, "r", encoding="utf-8") as f:
                    content = f.read()
                with open(self._data_file_path, "w", encoding="utf-8") as f:
                    f.write(content)

            await loop.run_in_executor(None, _copy)
            self.ctx.logger.info("已从旧版数据目录迁移好感度数据: %s", legacy_dir)
        except Exception as e:
            self.ctx.logger.warning("迁移旧版好感度数据失败: %s", e)

    async def _load_data(self) -> None:
        """从磁盘加载好感度数据。"""
        if not self._data_file_path:
            return
        try:
            if os.path.exists(self._data_file_path):
                loop = asyncio.get_running_loop()

                def _read() -> dict[str, dict[str, Any]]:
                    with open(self._data_file_path, "r", encoding="utf-8") as f:
                        return json.load(f)

                data = await loop.run_in_executor(None, _read)
                if isinstance(data, dict):
                    self._affection_data = data
                    self.ctx.logger.debug("已加载 %d 条好感度记录", len(data))
                else:
                    self._affection_data = {}
            else:
                self._affection_data = {}
                self.ctx.logger.debug("好感度数据文件不存在，将创建新文件")
        except json.JSONDecodeError as e:
            self.ctx.logger.warning("好感度数据文件损坏，将重置: %s", e)
            self._affection_data = {}
        except Exception as e:
            self.ctx.logger.error("加载好感度数据失败: %s", e)
            self._affection_data = {}

    async def _migrate_nickname_records(self) -> None:
        """把历史遗留的昵称记录迁移/合并到对应的 QQ 号记录。

        早期版本在群聊中可能用昵称作为 user_id 存储，私聊中用 QQ 号存储，
        导致同一个人被拆成两条记录。这里在加载后尝试把昵称记录解析为 QQ 号，
        若 QQ 号记录已存在则合并，否则将昵称键替换为 QQ 号键。

        昵称无法解析时保留原记录，避免数据丢失。
        """
        # 找出非纯数字的 user_id（视为昵称记录）
        nickname_keys = [
            uid for uid in self._affection_data
            if str(uid or "").strip() and not str(uid).strip().isdigit()
        ]
        if not nickname_keys:
            return

        migrated = 0
        for nick in nickname_keys:
            try:
                person_id = await self.ctx.person.get_id_by_name(nick)
            except Exception as e:
                self.ctx.logger.debug("迁移昵称记录失败 (%s): %s", nick, e)
                continue
            if not person_id:
                continue
            person_id_str = str(person_id).strip()
            if not person_id_str:
                continue
            if person_id_str == nick:
                continue

            nick_record = self._affection_data.pop(nick)
            if person_id_str in self._affection_data:
                # 已存在 QQ 号记录：合并（取好感度较高者，历史合并）
                existing = self._affection_data[person_id_str]
                old_affection = existing.get("affection", 0.0)
                nick_affection = nick_record.get("affection", 0.0)
                # 历史记录合并
                merged_history = list(existing.get("history", [])) + list(
                    nick_record.get("history", [])
                )
                if len(merged_history) > 200:
                    merged_history = merged_history[-200:]
                existing["history"] = merged_history
                # 记录一次迁移事件
                existing["history"].append({
                    "time": datetime.now().isoformat(),
                    "change": 0.0,
                    "actual_change": 0.0,
                    "type": "migration",
                    "description": f"合并昵称记录「{nick}」到 QQ 号 {person_id_str}",
                    "old_value": round(old_affection, 1),
                    "new_value": round(old_affection, 1),
                })
                # 取两者中较高好感度，避免历史拆分后丢失
                existing["affection"] = max(old_affection, nick_affection)
                self.ctx.logger.info(
                    "合并昵称记录: %s -> %s (好感度取较高值 %.1f)",
                    nick, person_id_str, existing["affection"],
                )
            else:
                # 直接替换键为 QQ 号
                self._affection_data[person_id_str] = nick_record
                self.ctx.logger.info(
                    "迁移昵称记录: %s -> %s", nick, person_id_str
                )
            migrated += 1

        if migrated > 0:
            await self._save_data()
            self.ctx.logger.info("昵称记录迁移完成: 共 %d 条", migrated)

    async def _save_data(self) -> None:
        """将好感度数据写入磁盘（带锁保护）。"""
        if not self._data_file_path:
            return
        async with self._file_lock:
            try:
                loop = asyncio.get_running_loop()

                def _write() -> None:
                    # 先写入临时文件，成功后再替换，避免写入中断导致文件损坏
                    tmp_path = self._data_file_path + ".tmp"
                    with open(tmp_path, "w", encoding="utf-8") as f:
                        json.dump(self._affection_data, f, ensure_ascii=False, indent=2)
                    os.replace(tmp_path, self._data_file_path)

                await loop.run_in_executor(None, _write)
                self.ctx.logger.debug("好感度数据已保存 (%d 条记录)", len(self._affection_data))
            except Exception as e:
                self.ctx.logger.error("保存好感度数据失败: %s", e)

    # ════════════════════════════════════════════════════════════════
    # WebUI 可视化配置双向同步
    # ════════════════════════════════════════════════════════════════
    # 设计说明：
    #   - 「好感度管理」配置节（user_management.managed_users）在 WebUI 中
    #     渲染为可编辑表格，是可视化查看/修改的界面。
    #   - 运行时好感度数据存于 affection_data.json（唯一事实来源）。
    #   - 双向同步：
    #       1. 运行时 → config.toml：LLM 通过 Tool 调整好感度后，把最新数值
    #          写回自身 config.toml，保证 WebUI 表格展示最新数据。
    #       2. config.toml → 运行时：用户在 WebUI 表格中手动修改并保存后，
    #          Host 触发 on_config_update，把修改应用到运行时数据。
    #   - 同步目标路径：plugin.py 同目录的 config.toml（即插件自身配置）。

    def _get_config_path(self) -> Path:
        """返回插件自身 config.toml 的路径。

        Returns:
            Path: config.toml 绝对路径
        """
        return Path(__file__).resolve().parent / "config.toml"

    async def _sync_runtime_to_config(self) -> None:
        """将运行时好感度数据同步回 config.toml 的管理表。

        仅在开启了「自动同步到管理表」且数据确实发生变化时才会写文件，
        避免反复触发配置热更新。
        """
        if self.config is None:
            return
        try:
            if not self.config.user_management.sync_to_config:
                return
        except AttributeError:
            return

        # 构造当前运行时数据的同步快照
        entries = []
        for user_id, record in self._affection_data.items():
            entries.append({
                "user_id": user_id,
                "affection": round(float(record.get("affection", 0.0) or 0.0), 1),
                "locked": bool(record.get("locked", False)),
                "note": str(record.get("note", "") or ""),
            })
            # 标记该用户已进入配置管理表，供 _sync_config_to_runtime 清理逻辑使用
            record["managed_in_config"] = True
        # 按好感度降序排列，方便在 WebUI 中浏览
        entries.sort(key=lambda e: e["affection"], reverse=True)

        # 计算同步键，用于跳过重复写入
        sync_key = json.dumps(entries, ensure_ascii=False, sort_keys=True)
        if sync_key == self._last_config_sync_key:
            return
        self._last_config_sync_key = sync_key

        try:
            config_path = self._get_config_path()
            if not config_path.exists():
                return

            loop = asyncio.get_running_loop()

            def _write_sync() -> None:
                # 使用 tomlkit 保留原有注释与格式，仅更新 user_management 节
                with open(config_path, "r", encoding="utf-8") as f:
                    document = tomlkit.parse(f.read())
                # 确保 user_management 节存在
                um = document.get("user_management")
                if um is None or not isinstance(um, dict):
                    um = tomlkit.table()
                    document["user_management"] = um
                # 将好感度条目写入配置表
                user_table = tomlkit.aot()
                for entry in entries:
                    row = tomlkit.table()
                    row["user_id"] = entry["user_id"]
                    row["affection"] = entry["affection"]
                    row["locked"] = entry["locked"]
                    row["note"] = entry["note"]
                    user_table.append(row)
                um["managed_users"] = user_table
                # 原子写入
                tmp_path = config_path.with_suffix(".toml.tmp")
                with open(tmp_path, "w", encoding="utf-8") as f:
                    f.write(tomlkit.dumps(document))
                os.replace(tmp_path, config_path)

            await loop.run_in_executor(None, _write_sync)
            self.ctx.logger.debug("好感度已同步到配置表 (%d 位用户)", len(entries))
        except Exception as e:
            self.ctx.logger.warning("同步好感度到配置表失败: %s", e)

    async def _sync_config_to_runtime(self, config_data: dict[str, object]) -> None:
        """将 WebUI 中手动修改的好感度配置同步到运行时数据。

        用户在 WebUI 表格中修改/新增/删除好感度并保存后，Host 会触发
        on_config_update，这里把改动应用进运行时数据，并持久化。

        Args:
            config_data: Host 传来的最新插件配置字典
        """
        if not isinstance(config_data, dict):
            return
        um = config_data.get("user_management")
        if not isinstance(um, dict):
            return
        managed_users = um.get("managed_users")
        if not isinstance(managed_users, list):
            return

        # 记录本配置表中出现的用户 ID，用于清理被删除的条目
        active_user_ids: set[str] = set()
        changed = False

        for entry in managed_users:
            if not isinstance(entry, dict):
                continue
            user_id = str(entry.get("user_id", "") or "").strip()
            if not user_id:
                continue
            active_user_ids.add(user_id)

            record = self._get_user_record(user_id)
            # 应用 WebUI 中设置的好感度值
            try:
                new_affection = float(entry.get("affection", 0.0) or 0.0)
            except (TypeError, ValueError):
                new_affection = 0.0
            new_affection = max(-20.0, min(100.0, new_affection))

            if abs(record.get("affection", 0.0) - new_affection) > 1e-9:
                # 记录一条手动修改的历史
                history = record.get("history", [])
                history.append({
                    "time": datetime.now().isoformat(),
                    "change": round(new_affection - record.get("affection", 0.0), 1),
                    "actual_change": round(new_affection - record.get("affection", 0.0), 1),
                    "type": "manual",
                    "description": "WebUI 手动修改好感度",
                    "old_value": round(record.get("affection", 0.0), 1),
                    "new_value": round(new_affection, 1),
                })
                if len(history) > 200:
                    history = history[-200:]
                record["history"] = history
                record["affection"] = new_affection
                changed = True

            # 应用锁定状态
            locked = bool(entry.get("locked", False))
            if record.get("locked") != locked:
                record["locked"] = locked
                changed = True

            # 应用备注
            note = str(entry.get("note", "") or "")
            if record.get("note") != note:
                record["note"] = note
                changed = True

            record["last_updated"] = datetime.now().isoformat()

        # 清理管理表中已删除的用户（仅删除由该表管理过的记录）
        removed_ids = [
            user_id for user_id in list(self._affection_data.keys())
            if user_id not in active_user_ids
            and self._affection_data[user_id].get("managed_in_config", False)
        ]
        for user_id in removed_ids:
            self._affection_data.pop(user_id, None)
            changed = True

        if changed:
            await self._save_data()
            self.ctx.logger.info(
                "已应用 WebUI 手动好感度修改 (%d 位用户)", len(active_user_ids)
            )

    # ════════════════════════════════════════════════════════════════
    # 好感度查询与控制逻辑
    # ════════════════════════════════════════════════════════════════

    def _get_user_record(self, user_id: str) -> dict[str, Any]:
        """获取指定用户的好感度记录，不存在则返回默认记录。

        Args:
            user_id: 用户唯一标识

        Returns:
            用户好感度记录字典
        """
        if user_id not in self._affection_data:
            self._affection_data[user_id] = {
                "affection": 0.0,
                "history": [],
                "last_updated": datetime.now().isoformat(),
                "daily_count": 0,
                "last_modified_date": datetime.now().strftime("%Y-%m-%d"),
                "locked": False,
                "note": "",
                "managed_in_config": False,
            }
        return self._affection_data[user_id]

    async def _resolve_user_id(
        self,
        requested_user_id: str,
        **kwargs: Any,
    ) -> str:
        """将互动对象统一解析为稳定、唯一的 QQ 号标识。

        背景：群聊中 LLM 可能通过昵称识别互动对象，私聊中可能通过 QQ 号识别，
        导致同一个人被当成两个人分别记录。这里统一识别口径。

        解析优先级：
            1. 运行时注入的 user_id（消息元数据中的真实平台 ID，即 QQ 号）
            2. 显式传入且为纯数字的 user_id（视为 QQ 号）
            3. 尝试通过 person 能力把昵称解析为人物 ID
            4. 兜底：原样返回

        Args:
            requested_user_id: LLM 传入的 user_id 参数（可能是昵称或 QQ 号）
            **kwargs: 运行时注入的上下文参数（含权威 user_id 等）

        Returns:
            归一化后的稳定用户标识（优先为 QQ 号）
        """
        # 1. 运行时注入的 user_id 是消息元数据中的真实平台 ID，优先采用。
        #    注意：当 Tool 方法签名含 user_id 参数时，注入值会绑定到命名参数
        #    而非 kwargs，因此此处同时兼容两种传递方式。
        injected = str(kwargs.get("user_id", "") or "").strip()
        if injected and injected.isdigit():
            self.ctx.logger.debug("使用运行时注入的用户标识: %s", injected)
            return injected

        # 2. 显式传入的纯数字参数直接视为 QQ 号（可能是注入绑定值或 LLM 填的）
        requested = str(requested_user_id or "").strip()
        if requested.isdigit():
            return requested

        # 3. 尝试把昵称解析为人物 ID（人物 ID 通常为纯数字，即平台 user_id）
        if requested:
            try:
                person_id = await self.ctx.person.get_id_by_name(requested)
                if person_id:
                    person_id_str = str(person_id).strip()
                    if person_id_str.isdigit():
                        self.ctx.logger.debug(
                            "已通过昵称解析用户标识: %s -> %s", requested, person_id_str
                        )
                        return person_id_str
                    # 人物 ID 非纯数字时仍可安全使用（唯一且稳定）
                    return person_id_str
            except Exception as e:
                self.ctx.logger.debug("昵称解析失败 (%s): %s", requested, e)

        # 4. 兜底：返回注入值或原参数
        return injected or requested

    def _check_cool_down(self, user_id: str) -> bool:
        """检查用户是否处于好感度变更冷却期。

        Args:
            user_id: 用户唯一标识

        Returns:
            True 表示在冷却期（不可变更），False 表示不在冷却期（可变更）
        """
        if self.config is None:
            return False
        cool_down = self.config.affection_rule.cool_down_seconds
        now = time.time()
        last_time = self._cool_down_map.get(user_id, 0.0)
        return (now - last_time) < cool_down

    def _check_daily_limit(self, user_id: str) -> bool:
        """检查用户今日好感度变更是否已达上限。

        Args:
            user_id: 用户唯一标识

        Returns:
            True 表示已达上限，False 表示未达上限
        """
        if self.config is None:
            return False
        record = self._get_user_record(user_id)
        today = datetime.now().strftime("%Y-%m-%d")
        last_date = record.get("last_modified_date", "")
        daily_count = record.get("daily_count", 0)
        if last_date != today:
            # 新的一天，重置计数
            record["daily_count"] = 0
            record["last_modified_date"] = today
            return False
        limit = self.config.affection_rule.daily_limit
        return daily_count >= limit

    def _apply_personality_multiplier(self, change_value: float) -> float:
        """根据配置的性格类型计算实际好感度变化值。

        Args:
            change_value: 原始变化值

        Returns:
            应用性格倍率后的变化值
        """
        if self.config is None:
            return change_value
        personality = self.config.personality.personality_type
        multiplier = PERSONALITY_MULTIPLIERS.get(personality, 1.0)
        # 再乘上用户配置的倍率
        cfg_multiplier = self.config.personality.affection_gain_multiplier
        return change_value * multiplier * cfg_multiplier

    async def _update_affection(
        self,
        user_id: str,
        change_value: float,
        interaction_type: str,
        description: str,
    ) -> dict[str, Any]:
        """更新指定用户的好感度。

        包含冷却检查、每日上限检查、历史记录追加和数据保存。

        Args:
            user_id: 用户唯一标识
            change_value: 好感度变化值（正值增加，负值减少）
            interaction_type: 互动类型（positive/neutral/negative/special）
            description: 互动简述

        Returns:
            更新结果字典
        """
        # 参数校验
        if not user_id or not user_id.strip():
            return {"success": False, "error": "用户 ID 不能为空"}

        # 冷却检查
        if self._check_cool_down(user_id):
            remaining = self.config.affection_rule.cool_down_seconds - (
                time.time() - self._cool_down_map.get(user_id, 0.0)
            )
            return {
                "success": False,
                "error": f"变更冷却中，请等待 {max(0, int(remaining))} 秒后再试",
            }

        # 每日上限检查（仅统计增加/减少的变更，查询不在此列）
        if change_value != 0 and self._check_daily_limit(user_id):
            return {
                "success": False,
                "error": "今日好感度变更次数已达上限",
            }

        # 获取用户记录
        record = self._get_user_record(user_id)

        # 应用性格倍率
        adjusted_change = self._apply_personality_multiplier(change_value)

        # 计算新的好感度值
        old_affection = record["affection"]
        new_affection = max(-20.0, min(100.0, old_affection + adjusted_change))
        actual_change = round(new_affection - old_affection, 1)

        # 更新记录
        record["affection"] = new_affection
        record["last_updated"] = datetime.now().isoformat()
        record["daily_count"] = record.get("daily_count", 0) + 1
        record["last_modified_date"] = datetime.now().strftime("%Y-%m-%d")

        # 追加历史记录（最多保留 200 条）
        history_entry = {
            "time": datetime.now().isoformat(),
            "change": round(adjusted_change, 1),
            "actual_change": actual_change,
            "type": interaction_type,
            "description": description,
            "old_value": round(old_affection, 1),
            "new_value": round(new_affection, 1),
        }
        history = record.get("history", [])
        history.append(history_entry)
        # 限制历史记录数量
        if len(history) > 200:
            history = history[-200:]
        record["history"] = history

        # 记录冷却时间
        self._cool_down_map[user_id] = time.time()

        # 持久化保存
        await self._save_data()

        # 将最新好感度同步回 config.toml 管理表（供 WebUI 展示）
        await self._sync_runtime_to_config()

        # 获取旧等级和新等级信息
        old_level = _get_affection_level_info(old_affection)
        new_level = _get_affection_level_info(new_affection)
        level_changed = old_level["level_name"] != new_level["level_name"]

        # 生成变更摘要
        change_direction = "增加" if actual_change > 0 else ("减少" if actual_change < 0 else "不变")
        result = {
            "success": True,
            "user_id": user_id,
            "old_affection": round(old_affection, 1),
            "new_affection": round(new_affection, 1),
            "actual_change": actual_change,
            "change_direction": change_direction,
            "current_level": new_level["level_name"],
            "current_level_description": new_level["level_description"],
            "level_changed": level_changed,
        }
        if level_changed:
            result["old_level"] = old_level["level_name"]
            result["level_change_message"] = (
                f"好感度等级从「{old_level['level_name']}」变为「{new_level['level_name']}」"
            )

        self.ctx.logger.info(
            "好感度变更: user=%s, %s %.1f (%.1f → %.1f), 等级=%s, 原因=%s",
            user_id,
            change_direction,
            actual_change,
            old_affection,
            new_affection,
            new_level["level_name"],
            description,
        )

        return result

    async def _apply_decay_check(self) -> None:
        """检查并应用好感度衰减。

        如果启用了衰减功能，遍历所有用户记录，对超过衰减触发天数未互动的用户降低好感度。
        """
        if self.config is None:
            return
        if not self.config.personality.affection_decay_enabled:
            return

        decay_days = self.config.personality.affection_decay_days
        decay_rate = self.config.personality.affection_decay_rate
        now = datetime.now()
        decay_threshold = now - timedelta(days=decay_days)
        decayed_count = 0

        for user_id, record in list(self._affection_data.items()):
            try:
                # 已锁定的用户不受自然衰减影响
                if record.get("locked", False):
                    continue
                last_updated_str = record.get("last_updated", "")
                if not last_updated_str:
                    continue
                last_updated = datetime.fromisoformat(last_updated_str)
                if last_updated > decay_threshold:
                    continue
                # 已超过触发生天数，应用衰减
                old_val = record["affection"]
                new_val = max(-20.0, old_val - decay_rate)
                if new_val < old_val:
                    record["affection"] = new_val
                    record["last_updated"] = now.isoformat()
                    # 追加历史
                    history = record.get("history", [])
                    history.append({
                        "time": now.isoformat(),
                        "change": round(new_val - old_val, 1),
                        "actual_change": round(new_val - old_val, 1),
                        "type": "decay",
                        "description": f"超过 {decay_days} 天未互动，好感度自然衰减",
                        "old_value": round(old_val, 1),
                        "new_value": round(new_val, 1),
                    })
                    if len(history) > 200:
                        history = history[-200:]
                    record["history"] = history
                    decayed_count += 1
            except (ValueError, KeyError, TypeError):
                continue

        if decayed_count > 0:
            await self._save_data()
            await self._sync_runtime_to_config()
            self.ctx.logger.info("好感度衰减检查完成: %d 位用户好感度已衰减", decayed_count)

    def _get_response_hint(self, affection_value: float) -> str:
        """根据好感度值获取对应的回应风格提示。

        Args:
            affection_value: 好感度数值

        Returns:
            回应风格提示字符串
        """
        level_info = _get_affection_level_info(affection_value)
        level_name = level_info["level_name"]
        if self.config is None:
            return "请根据你的角色设定自然地回应"

        cfg = self.config.response
        hint_map = {
            "不喜": cfg.dislike_response_hint,
            "正常": cfg.normal_response_hint,
            "熟悉": cfg.familiar_response_hint,
            "亲近": cfg.close_response_hint,
            "亲密": cfg.intimate_response_hint,
            "思慕": cfg.adore_response_hint,
        }
        return hint_map.get(level_name, "请根据你的角色设定自然地回应")

    # ════════════════════════════════════════════════════════════════
    # Tool: 查询好感度
    # ════════════════════════════════════════════════════════════════

    @Tool(
        "query_affection",
        description=(
            "查询指定用户与角色的当前好感度。返回好感度数值、等级名称、等级描述、"
            "在该等级内的进度百分比、以及根据当前等级提供给角色的回应风格提示。"
            "在角色回复用户之前应调用此工具，以确定合适的语气和态度。"
            "当用户询问「你对我感觉怎样」「我们关系如何」等涉及关系评价的问题时也应调用。"
            "注意：通常无需填写 user_id，运行时会自动注入当前互动对象的 QQ 号；"
            "如需查询指定对象请填写其 QQ 号（不要填昵称）。"
        ),
        parameters=[
            ToolParameterInfo(
                name="user_id",
                param_type=ToolParamType.STRING,
                description="要查询好感度的用户 QQ 号。通常可留空，运行时会自动注入当前互动对象的 QQ 号。",
                required=False,
            ),
            ToolParameterInfo(
                name="stream_id",
                param_type=ToolParamType.STRING,
                description="当前聊天流 ID，由运行时自动注入，无需手动填写",
                required=False,
            ),
        ],
    )
    async def query_affection(
        self,
        user_id: str = "",
        stream_id: str = "",
        **kwargs: Any,
    ) -> dict[str, Any]:
        """查询指定用户的好感度。

        Args:
            user_id: 用户 QQ 号或昵称（可留空，运行时注入当前对象；会统一解析为 QQ 号）
            stream_id: 聊天流 ID（由运行时注入）
            **kwargs: 额外参数

        Returns:
            好感度查询结果
        """
        # 统一识别：优先使用运行时注入的真实 QQ 号，避免群聊昵称/私聊 QQ 号歧义
        resolved_user_id = await self._resolve_user_id(user_id, **kwargs)
        if not resolved_user_id:
            return {"success": False, "error": "无法确定互动对象的用户标识"}

        # 记录「会话 ↔ 用户」映射，供注入 Hook 反查使用
        self._record_session_user(stream_id, resolved_user_id)

        # 获取好感度数据
        record = self._get_user_record(resolved_user_id)
        affection_value = record["affection"]
        level_info = _get_affection_level_info(affection_value)
        response_hint = self._get_response_hint(affection_value)

        # 获取最近几条历史记录
        history = record.get("history", [])
        recent_history = history[-5:] if history else []

        # 拼接角色名
        char_name = self.config.personality.character_name if self.config else "角色"

        # 构建返回结果
        result = {
            "success": True,
            "user_id": resolved_user_id,
            "affection_value": level_info["value"],
            "affection_level": level_info["level_name"],
            "level_description": level_info["level_description"],
            "level_progress_percent": level_info["level_progress"],
            "level_range": f"{level_info['level_min']} ~ {level_info['level_max']}",
            "response_hint": response_hint,
            "character_name": char_name,
            "locked": bool(record.get("locked", False)),
            "note": str(record.get("note", "") or ""),
            "recent_changes": [
                {
                    "change": h.get("change"),
                    "type": h.get("type"),
                    "description": h.get("description"),
                    "time": h.get("time"),
                }
                for h in recent_history
            ],
            "suggestion": (
                f"当前好感度等级为「{level_info['level_name']}」，"
                f"{level_info['level_description']}。"
                f"回应时应：{response_hint}"
            ),
        }
        if result["locked"]:
            result["suggestion"] = (
                f"该用户的好感度已被管理员锁定（当前 {result['affection_value']} 点，"
                f"「{level_info['level_name']}」），你无法自动调整它，请在回复中保持当前态度。"
            )

        self.ctx.logger.debug(
            "查询好感度: user=%s, affection=%.1f, level=%s",
            user_id,
            affection_value,
            level_info["level_name"],
        )

        return result

    # ════════════════════════════════════════════════════════════════
    # Tool: 调整好感度
    # ════════════════════════════════════════════════════════════════

    @Tool(
        "adjust_affection",
        description=(
            "根据用户与角色的互动情况，调整该用户的好感度值。"
            "在每一次有意义的互动后（正面的赞美帮助、负面的冒犯争吵、中性的普通聊天、"
            "特殊的送礼表白等），应当调用此工具来记录并更新好感度。"
            "好感度范围从 -20（极度反感）到 100（思慕倾心）。"
            "调用时机："
            "- 用户说了赞美/关心/感谢/安慰的话 → interaction_type='positive'"
            "- 用户说了冒犯/辱骂/嘲讽/冷漠的话 → interaction_type='negative'"
            "- 用户进行了普通日常聊天 → interaction_type='neutral'"
            "- 用户送了礼物/表白（比较亲近的时候）/做了特别的事 → interaction_type='special'"
            "注意：好感度并非每次说话都应调整，应根据角色性格和互动质量判断是否需要调整。"
            "琐碎的闲聊不需要调用此工具。"
            "注意：通常无需填写 user_id，运行时会自动注入当前互动对象的 QQ 号；"
            "如需调整指定对象请填写其 QQ 号（不要填昵称）。"
        ),
        parameters=[
            ToolParameterInfo(
                name="user_id",
                param_type=ToolParamType.STRING,
                description="要调整好感度的用户 QQ 号。通常可留空，运行时会自动注入当前互动对象的 QQ 号。",
                required=False,
            ),
            ToolParameterInfo(
                name="interaction_type",
                param_type=ToolParamType.STRING,
                description=(
                    "互动类型: positive(正面), neutral(中性), "
                    "negative(负面), special(特殊)"
                ),
                required=True,
            ),
            ToolParameterInfo(
                name="description",
                param_type=ToolParamType.STRING,
                description="简短描述本次互动的内容（如「用户赞美了角色的新衣服」）",
                required=True,
            ),
            ToolParameterInfo(
                name="stream_id",
                param_type=ToolParamType.STRING,
                description="当前聊天流 ID，由运行时自动注入，无需手动填写",
                required=False,
            ),
        ],
    )
    async def adjust_affection(
        self,
        user_id: str = "",
        interaction_type: str = "",
        description: str = "",
        stream_id: str = "",
        **kwargs: Any,
    ) -> dict[str, Any]:
        """根据互动调整用户好感度。

        Args:
            user_id: 用户 QQ 号或昵称（可留空，运行时注入当前对象；会统一解析为 QQ 号）
            interaction_type: 互动类型
            description: 互动描述
            stream_id: 聊天流 ID（由运行时注入）
            **kwargs: 额外参数

        Returns:
            调整结果
        """
        # 统一识别：优先使用运行时注入的真实 QQ 号，避免群聊昵称/私聊 QQ 号歧义
        resolved_user_id = await self._resolve_user_id(user_id, **kwargs)
        if not resolved_user_id:
            return {"success": False, "error": "无法确定互动对象的用户标识"}

        # 记录「会话 ↔ 用户」映射，供注入 Hook 反查使用
        self._record_session_user(stream_id, resolved_user_id)

        # 参数校验
        valid_types = {"positive", "neutral", "negative", "special"}
        interaction_type = interaction_type.strip().lower()
        if interaction_type not in valid_types:
            return {
                "success": False,
                "error": f"无效的互动类型: {interaction_type}，有效类型为: {', '.join(sorted(valid_types))}",
            }

        if not description or not description.strip():
            return {"success": False, "error": "互动描述不能为空"}

        if self.config is None:
            return {"success": False, "error": "插件配置未加载"}

        # 锁定检查：WebUI 中锁定的用户，LLM 无法自动调整好感度
        existing = self._affection_data.get(resolved_user_id, {})
        if existing.get("locked", False):
            self.ctx.logger.debug(
                "用户 %s 的好感度已被锁定，拒绝 LLM 自动调整", resolved_user_id
            )
            return {
                "success": False,
                "error": "该用户的好感度已被管理员锁定，请在 WebUI 中手动修改",
            }

        # 根据互动类型和配置计算好感度变化值
        cfg = self.config.affection_rule
        if interaction_type == "positive":
            min_val = cfg.positive_interaction_min
            max_val = cfg.positive_interaction_max
            change_value = random.uniform(min_val, max_val)
        elif interaction_type == "negative":
            min_val = cfg.negative_interaction_min  # 更负的值（如 -3.0）
            max_val = cfg.negative_interaction_max  # 较不值的值（如 -0.5）
            # 注意：min_val 比 max_val 更小，所以 random.uniform 需要正确顺序
            change_value = random.uniform(min_val, max_val)
        elif interaction_type == "special":
            change_value = cfg.special_interaction_value
        else:
            # neutral
            change_value = cfg.neutral_interaction_value

        # 执行更新
        result = await self._update_affection(
            user_id=resolved_user_id,
            change_value=change_value,
            interaction_type=interaction_type,
            description=description,
        )

        return result

    # ════════════════════════════════════════════════════════════════
    # Command: /affection 查询当前好感度
    # ════════════════════════════════════════════════════════════════

    @Command(
        "check_affection",
        description="查询当前用户与角色的好感度",
        pattern=r"^/affection$",
    )
    async def check_affection(
        self, stream_id: str = "", **kwargs: Any
    ) -> tuple[bool, str, int]:
        """处理 /affection 命令，返回用户的好感度信息。

        Args:
            stream_id: 聊天流 ID（由运行时注入）
            **kwargs: 额外参数（包含 user_id 等）

        Returns:
            (success, message, priority) 元组
        """
        # 获取当前用户的 ID
        user_id = str(kwargs.get("user_id", "") or "").strip()
        if not user_id:
            message = "无法识别你的身份，请稍后再试。"
            if stream_id:
                await self.ctx.send.text(message, stream_id)
            return True, message, 1

        if self.config is None:
            message = "好感度系统配置未加载，请联系管理员。"
            if stream_id:
                await self.ctx.send.text(message, stream_id)
            return True, message, 1

        char_name = self.config.personality.character_name
        record = self._get_user_record(user_id)
        affection_value = record["affection"]
        level_info = _get_affection_level_info(affection_value)
        history = record.get("history", [])

        if not history:
            # 首次互动，没有历史记录
            message = (
                f"你和{char_name}的好感度为 {level_info['value']} 点，"
                f"属于「{level_info['level_name']}」等级。\n"
                f"{level_info['level_description']}。"
            )
        else:
            last_change = history[-1]
            change_time = last_change.get("time", "未知")
            try:
                dt = datetime.fromisoformat(change_time)
                change_time_str = dt.strftime("%m月%d日 %H:%M")
            except (ValueError, TypeError):
                change_time_str = change_time

            message = (
                f"你和{char_name}的好感度为 {level_info['value']} 点，"
                f"属于「{level_info['level_name']}」等级。\n"
                f"{level_info['level_description']}。\n"
                f"（最近一次变化: {change_time_str}）"
            )

        if stream_id:
            await self.ctx.send.text(message, stream_id)

        self.ctx.logger.debug(
            "用户 %s 查询好感度: %.1f (%s)", user_id, affection_value, level_info["level_name"]
        )

        return True, message, 1

    # ════════════════════════════════════════════════════════════════
    # Hook: 注入好感度提示（参考 ns_auto-planning_schedule 的调用方式）
    # ════════════════════════════════════════════════════════════════
    # 目的：提高 LLM 对好感度的感知，从而：
    #   1. 按照对应好感度等级自然回应；
    #   2. 更倾向于在互动后调用 adjust_affection 工具更新好感度。
    #
    # 参考 AutoPlanning 插件经过验证的机制：
    #   - 主通道：maisaka.planner.after_response（BLOCKING），在 planner 决定调用
    #     reply 后，把好感度上下文注入 reply 工具的 reference_info 参数。replyer
    #     生成回复时会读取 reference_info，因此 LLM 一定能感知到好感度。
    #   - 辅助通道：maisaka.planner.before_request（BLOCKING），在 LLM 规划前
    #     向消息窗口插入一条系统提示，进一步促使 LLM 调用 adjust_affection。
    # 两条通道每次请求都会注入（不再走概率采样）。

    @HookHandler(
        "maisaka.planner.after_response",
        name="affection_inject_into_reply",
        description="在 planner 决定调用 reply 后，将当前用户好感度上下文注入 reply 的 reference_info，使 LLM 回复体现好感度并倾向调用 adjust_affection",
        mode=HookMode.BLOCKING,
        order=HookOrder.LATE,
        timeout_ms=3000,
        error_policy=ErrorPolicy.SKIP,
    )
    async def inject_affection_into_reply(
        self,
        tool_calls: list[dict[str, Any]] | None = None,
        session_id: str = "",
        **kwargs: Any,
    ) -> dict[str, Any]:
        """参考 AutoPlanning 插件：在 planner 决定调用 reply 后注入好感度参考。

        找到 reply 工具调用，把好感度上下文注入其 reference_info 参数，
        使 replyer 生成回复时参考好感度等级。

        Args:
            tool_calls: planner 生成的工具调用列表
            session_id: 当前会话 ID
            **kwargs: 其他 Hook 参数

        Returns:
            Hook 分发结果：{"action": "continue", "modified_kwargs": {...}}
        """
        del kwargs
        if self.config is None:
            return {"action": "continue"}
        if not self.config.plugin.enabled:
            return {"action": "continue"}
        if not self.config.call_behavior.inject_affection_hint:
            return {"action": "continue"}
        if not isinstance(tool_calls, list) or not tool_calls:
            return {"action": "continue"}

        # 通过 session_id 反查发言者 QQ 号；解析失败时退化为通用好感度提示，
        # 确保注入始终发生（参考 AutoPlanning 插件每次请求都注入）。
        #
        # 注意：本 Hook 注入的是 replyer 参考信息。replyer 模型没有工具也没有
        # 工具 Schema，若提示里出现「请调用 adjust_affection 工具」之类的指令，
        # 模型会以 <function_calls> 文本形式模仿工具调用并原样发出，造成工具
        # 调用信息泄漏到聊天。因此这里只注入好感度态度提示，绝不包含工具调用引导。
        user_id = await self._resolve_user_id_from_session(session_id)
        if user_id:
            hint_text = self._build_affection_hint(user_id, for_replyer=True)
        else:
            hint_text = self._build_generic_affection_hint(for_replyer=True)
            self.ctx.logger.info(
                "未解析到会话 %s 的用户标识，注入通用好感度提示", session_id
            )
        if not hint_text:
            return {"action": "continue"}

        modified_tool_calls = self._inject_into_reply_reference(tool_calls, hint_text)
        if modified_tool_calls is tool_calls:
            return {"action": "continue"}

        result = {"action": "continue", "modified_kwargs": {"tool_calls": modified_tool_calls}}
        self.ctx.logger.info(
            "已向会话 %s 的 reply 注入好感度参考 (%s)", session_id, user_id
        )
        return result

    @staticmethod
    def _inject_into_reply_reference(
        tool_calls: list[dict[str, Any]],
        hint_text: str,
    ) -> list[dict[str, Any]]:
        """把好感度提示注入 reply 工具的 reference_info 参数。

        参考 AutoPlanning 插件的 inject_schedule_into_reply 实现。

        Args:
            tool_calls: planner 生成的工具调用列表
            hint_text: 好感度提示文本

        Returns:
            修改后的工具调用列表；未找到 reply 时返回原列表
        """
        reply_idx = None
        for i, tc in enumerate(tool_calls):
            func = tc.get("function", {}) if isinstance(tc, dict) else {}
            if isinstance(func, dict) and func.get("name") == "reply":
                reply_idx = i
                break
        if reply_idx is None:
            return tool_calls

        modified = [dict(tc) for tc in tool_calls]
        tc = modified[reply_idx]
        func = dict(tc.get("function", {}))
        args = dict(func.get("arguments", {}))
        existing_ref = str(args.get("reference_info", "") or "").strip()
        if existing_ref:
            args["reference_info"] = f"{existing_ref}\n\n{hint_text}"
        else:
            args["reference_info"] = hint_text
        func["arguments"] = args
        tc["function"] = func
        modified[reply_idx] = tc
        return modified

    @HookHandler(
        "maisaka.planner.before_request",
        name="affection_planner_injector",
        description="在 LLM 规划请求前，将当前用户的好感度提示注入消息窗口，提高 LLM 感知与 adjust_affection 工具调用频率",
        mode=HookMode.BLOCKING,
        order=HookOrder.LATE,
        timeout_ms=3000,
        error_policy=ErrorPolicy.SKIP,
    )
    async def inject_affection_before_request(
        self,
        messages: list[dict[str, Any]] | None = None,
        session_id: str = "",
        **kwargs: Any,
    ) -> dict[str, Any]:
        """在 LLM 请求前注入好感度提示。

        每次都注入（不受概率控制）。通过 session_id 反查发言者 QQ 号，
        构造好感度系统提示并插入 messages 列表首部，确保 LLM 一定感知到。

        Args:
            messages: 即将发给模型的 PromptMessage 列表
            session_id: 当前会话 ID
            **kwargs: 其他 Hook 参数

        Returns:
            Hook 分发结果：{"action": "continue", "modified_kwargs": {...}}
        """
        del kwargs
        if self.config is None:
            return {"action": "continue"}
        if not self.config.plugin.enabled:
            return {"action": "continue"}
        if not self.config.call_behavior.inject_affection_hint:
            return {"action": "continue"}
        if not isinstance(messages, list) or not messages:
            return {"action": "continue"}

        # 通过 session_id 反查发言者 QQ 号；解析失败时退化为通用好感度提示，
        # 确保注入始终发生。
        user_id = await self._resolve_user_id_from_session(session_id)
        if user_id:
            hint_text = self._build_affection_hint(user_id)
        else:
            hint_text = self._build_generic_affection_hint()
            self.ctx.logger.info(
                "未解析到会话 %s 的用户标识，注入通用好感度提示", session_id
            )
        if not hint_text:
            return {"action": "continue"}

        # 注入一条 system 提示。若消息中已有 system 消息，则插在最后一个
        # system 之后；否则直接作为首条。这样既不影响原有系统提示，又能
        # 保证 LLM 一定看到好感度提示。
        hint_msg = {"role": "system", "content": hint_text}
        last_system_idx = -1
        for idx, msg in enumerate(messages):
            if isinstance(msg, dict) and msg.get("role") == "system":
                last_system_idx = idx
        if last_system_idx < 0:
            modified_messages = [hint_msg] + list(messages)
        else:
            modified_messages = (
                list(messages[: last_system_idx + 1])
                + [hint_msg]
                + list(messages[last_system_idx + 1 :])
            )

        self.ctx.logger.info(
            "已向会话 %s 注入好感度提示 (%d 条消息 -> %d 条, user=%s)",
            session_id,
            len(messages),
            len(modified_messages),
            user_id,
        )
        return {"action": "continue", "modified_kwargs": {"messages": modified_messages}}

    async def _resolve_user_id_from_session(self, session_id: str) -> str:
        """根据会话 ID 反查发言者的权威用户标识（QQ 号）。

        优先使用运行时 Tool 调用时缓存的「会话↔用户」映射；其次尝试从
        聊天流列表反查（仅对私聊有效，群聊流 user_id 为 None）。

        Args:
            session_id: 会话 ID

        Returns:
            用户平台 ID；无法解析时返回空字符串
        """
        session_id = str(session_id or "").strip()
        if not session_id:
            return ""

        # 1. 命中缓存（由 Tool 调用时记录，最可靠）
        cached = self._session_user_map.get(session_id, "")
        if cached:
            return cached

        # 2. 从聊天流列表反查（私聊场景有效）
        try:
            streams = await self.ctx.chat.get_all_streams(platform="all_platforms")
        except Exception as e:
            self.ctx.logger.debug("获取聊天流列表失败 (%s): %s", session_id, e)
            return ""
        if not isinstance(streams, list):
            return ""
        for stream in streams:
            if not isinstance(stream, dict):
                continue
            sid = str(stream.get("session_id") or stream.get("stream_id") or "").strip()
            if sid == session_id:
                uid = str(stream.get("user_id") or "").strip()
                if uid:
                    self._session_user_map[session_id] = uid
                return uid
        return ""

    def _record_session_user(self, session_id: str, user_id: str) -> None:
        """记录「会话 ID → 权威 QQ 号」映射，供注入 Hook 使用。

        Args:
            session_id: 会话 ID
            user_id: 权威用户标识（QQ 号）
        """
        session_id = str(session_id or "").strip()
        user_id = str(user_id or "").strip()
        if session_id and user_id:
            self._session_user_map[session_id] = user_id

    def _build_affection_hint(self, user_id: str, *, for_replyer: bool = False) -> str:
        """构造写入消息窗口的好感度提示文本。

        Args:
            user_id: 权威用户标识（QQ 号）
            for_replyer: 是否用于 replyer 参考信息。为 True 时只保留好感度态度提示，
                不包含「调用 adjust_affection 工具」的引导——replyer 模型没有工具，
                若提示它调用工具，模型会以 <function_calls> 文本形式模仿并原样发出。

        Returns:
            提示文本；无有效数据时返回空字符串
        """
        record = self._affection_data.get(user_id)
        if record is None:
            # 尚无记录，给出中性提示即可（仍鼓励 LLM 感知并调用工具）
            record = self._get_user_record(user_id)
        affection_value = record.get("affection", 0.0)
        level_info = _get_affection_level_info(affection_value)
        response_hint = self._get_response_hint(affection_value)

        char_name = self.config.personality.character_name if self.config else "角色"
        lines = [
            f"【好感度提示】你对当前这位用户的好感度为 {level_info['value']} 点，"
            f"属于「{level_info['level_name']}」等级：{level_info['level_description']}。",
            f"与这位{char_name}的互动应体现的态度：{response_hint}",
        ]
        if record.get("locked", False):
            lines.append("（该用户好感度已被管理员锁定，请保持当前态度，不要试图改变。）")
        elif (
            not for_replyer
            and self.config
            and self.config.call_behavior.encourage_tool_calls
        ):
            lines.append(
                "如果本次互动有明显的正面或负面情绪（如赞美、帮助、冒犯、争吵、"
                "感谢、关心等），在回应之后请调用 adjust_affection 工具更新好感度。"
            )
        return "\n".join(lines)

    def _build_generic_affection_hint(self, *, for_replyer: bool = False) -> str:
        """构造不绑定具体用户的好感度提示文本。

        当无法从会话解析出发言者 QQ 号时使用（例如首次群聊、缓存未建立时），
        仍然提醒 LLM 感知好感度系统并鼓励调用 adjust_affection 工具。

        Args:
            for_replyer: 是否用于 replyer 参考信息。为 True 时不包含工具调用引导，
                避免 replyer（无工具）以 <function_calls> 文本形式模仿并泄漏。

        Returns:
            提示文本
        """
        char_name = self.config.personality.character_name if self.config else "角色"
        lines = [
            f"【好感度提示】你与这位用户的好感度系统已启用，请根据过往互动自然回应，"
            f"态度与亲近程度保持角色一致（{char_name}的性格）。",
        ]
        if not for_replyer and self.config and self.config.call_behavior.encourage_tool_calls:
            lines.append(
                "如果本次互动有明显的正面或负面情绪（如赞美、帮助、冒犯、争吵、"
                "感谢、关心等），在回应之后请调用 adjust_affection 工具更新好感度。"
            )
        return "\n".join(lines)


# ════════════════════════════════════════════════════════════════
# 工厂函数（MaiBot 加载入口）
# ════════════════════════════════════════════════════════════════


def create_plugin() -> AffectionPlugin:
    """MaiBot 插件工厂函数。

    MaiBot 运行时会调用此函数来创建插件实例。
    """
    return AffectionPlugin()
