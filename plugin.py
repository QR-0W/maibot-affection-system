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
import math
import os
import random
import re
import time
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import tomlkit

from maibot_sdk import Command, HookHandler, MaiBotPlugin, Tool
from maibot_sdk.types import ErrorPolicy, HookMode, HookOrder, ToolParameterInfo, ToolParamType

from .config import AffectionConfig

# ════════════════════════════════════════════════════════════════
# 好感度等级定义 (下界含, 上界不含, 等级名称, 等级描述)
# 区间为左闭右开 [min, max)：边界值归属上一个等级，避免 0/10/20 等
# 边界值被同时匹配两个等级。仅最后一个等级（思慕）右端含 100。
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
    last_index = len(AFFECTION_LEVELS) - 1
    for index, (min_val, max_val, name, description) in enumerate(AFFECTION_LEVELS):
        # 左闭右开 [min, max)；最后一个区间右端含 100
        in_range = min_val <= clamped < max_val if index < last_index else min_val <= clamped <= max_val
        if in_range:
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
        # 接收索引：{ session_id: deque[(message_id, user_id)] }
        # 在 chat.receive.after_process 时建立（消息到达即记录，不依赖入库时机），
        # 供 before_request 按 prompt 中的 msg_id 精确反查当前发言人。
        self._session_message_index: dict[str, deque[tuple[str, str]]] = {}
        # 自动评价后台任务：{ user_id: asyncio.Task }
        # 由 chat.receive.after_process 调度，任务内调用 LLM 对互动做上下文评价，
        # 不阻塞消息接收链路；on_unload 时统一取消。
        self._evaluation_tasks: dict[str, asyncio.Task] = {}

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
        """插件卸载时取消后台任务、写入磁盘，并将运行时数据同步回配置。"""
        for user_id, task in list(self._evaluation_tasks.items()):
            task.cancel()
            self._evaluation_tasks.pop(user_id, None)
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
            initial = 10.0
            if self.config is not None:
                try:
                    initial = float(self.config.affection_rule.initial_affection or 10.0)
                except (AttributeError, TypeError, ValueError):
                    initial = 10.0
            initial = max(-20.0, min(100.0, initial))
            self._affection_data[user_id] = {
                "affection": initial,
                "history": [],
                "last_updated": datetime.now().isoformat(),
                "daily_count": 0,
                "last_modified_date": datetime.now().strftime("%Y-%m-%d"),
                "locked": False,
                "note": "",
                "managed_in_config": False,
            }
        return self._affection_data[user_id]

    def _resolve_user_id(self, requested_user_id: str) -> str:
        """好感记录统一使用 QQ 号，不把昵称或人物 SID 当成账号。"""
        user_id = requested_user_id.strip()
        return user_id if user_id.isdigit() else ""

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
            # 新的一天，重置计数（含负面螺旋计数）
            record["daily_count"] = 0
            record["daily_negative_count"] = 0
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

    def _apply_relationship_damping(
        self,
        change_value: float,
        record: dict[str, Any],
        interaction_type: str,
    ) -> float:
        """根据当前关系状态对好感度变化施加阻尼。

        两种阻尼（均只作用于负面扣分）：
            1. 高位阻尼：好感度较高（关系亲密）时，单次扣分按系数缩小，
               亲密关系经得起小摩擦；
            2. 负面螺旋阻尼：同一天连续负面调整超过上限后，后续扣分按系数
               逐次递减，防止短时间内好感度断崖式下跌。

        Args:
            change_value: 应用性格倍率后的变化值
            record: 用户好感度记录
            interaction_type: 互动类型（negative 才应用阻尼）

        Returns:
            应用关系阻尼后的变化值
        """
        if self.config is None:
            return change_value
        if change_value >= 0 or interaction_type != "negative":
            return change_value

        adjusted = change_value
        cfg = self.config.affection_rule
        affection = float(record.get("affection", 0.0) or 0.0)

        # 1. 高位阻尼
        if affection >= cfg.high_affection_negative_damper_threshold:
            adjusted *= cfg.high_affection_negative_damper_multiplier

        # 2. 负面螺旋阻尼（daily_negative_count 已在调用前自增，含本次）
        negative_count = int(record.get("daily_negative_count", 0) or 0)
        if negative_count > cfg.negative_spiral_limit:
            adjusted *= cfg.negative_spiral_decay ** (negative_count - cfg.negative_spiral_limit)

        return adjusted

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

        # 负面互动先累计当天负面计数（供负面螺旋阻尼使用），再应用关系阻尼
        if interaction_type == "negative":
            record["daily_negative_count"] = record.get("daily_negative_count", 0) + 1
        adjusted_change = self._apply_relationship_damping(adjusted_change, record, interaction_type)

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
            "在该等级内的进度百分比、最近几次变化记录、以及根据当前等级提供给角色的回应风格提示。"
            "在角色回复用户之前应调用此工具，以确定合适的语气和态度。"
            "当用户询问「你对我感觉怎样」「我们关系如何」等涉及关系评价的问题时也应调用。"
            "注意：必须填写关系索引中对应消息的用户 QQ 号；"
            "如需查询指定对象请填写其 QQ 号（不要填昵称）。"
        ),
        visibility="visible",
        parameters=[
            ToolParameterInfo(
                name="user_id",
                param_type=ToolParamType.STRING,
                description="目标用户 QQ 号，取自本轮消息关系索引，不接受昵称或人物 SID。",
                required=True,
            ),
        ],
    )
    async def query_affection(
        self,
        user_id: str = "",
        **kwargs: Any,
    ) -> dict[str, Any]:
        """查询指定用户的好感度。

        Args:
            user_id: 取自本轮消息关系索引的 QQ 号，不接受昵称或人物 SID
            **kwargs: Host 注入的上下文，不用于推测用户

        Returns:
            好感度查询结果
        """
        # 仅校验显式 QQ 号，不从 Host 上下文推测目标。
        resolved_user_id = self._resolve_user_id(user_id)
        if not resolved_user_id:
            return {"success": False, "error": "无法确定互动对象的用户标识"}

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
            "根据一次有实质意义的互动，调整该用户的好感度。"
            "好感度范围从 -20（极度反感）到 100（思慕倾心），初始为 10。"
            "判断依据（不要机械套用行为词表，应结合对话上下文与当前关系综合判断）：\n"
            "1. 用户的真实意图（善意 / 恶意 / 中性）；\n"
            "2. 互动是否推进关系：主动关心、记得角色说过的小事、顺着角色的话题、"
            "对角色情绪做出恰当回应、产生默契、主动陪伴、配合角色性格的玩笑等；\n"
            "3. 是否尊重角色边界：恶意贬低、背叛信任、无视边界、持续骚扰、恶意争吵。\n"
            "关系解读：同一言行在不同关系下意义不同。在「亲近」「亲密」「思慕」等级下，"
            "用户的玩笑、打趣、暧昧乃至略带露骨的调侃是关系亲密的体现，应视为正面或中性，"
            "只有越界、贬低或骚扰才是负面；在较低等级下，同样的言行才更容易构成冒犯。\n"
            "日常琐碎闲聊、单句问候、无实质内容的互动不需要调整好感度。\n"
            "重大事件（如表白、送礼、重大帮助、背叛信任）可填写 change_value 给出精确变化值"
            "（如轻微调侃 -1、诚心道歉 +2、救命之恩 +8）；不填写时插件按互动类型在配置范围内自动取值，"
            "并受角色性格倍率与关系阻尼限制，请勿填写极端数值。\n"
            "注意：插件会自动评价大多数日常互动，本工具主要用于重大事件或自动评价遗漏的情形。\n"
            "注意：必须填写关系索引中对应消息的用户 QQ 号；"
            "如需调整指定对象请填写其 QQ 号（不要填昵称）。"
        ),
        visibility="visible",
        parameters=[
            ToolParameterInfo(
                name="user_id",
                param_type=ToolParamType.STRING,
                description="目标用户 QQ 号，取自本轮消息关系索引，不接受昵称或人物 SID。",
                required=True,
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
                description="简短描述本次互动的内容（如「用户赞美了角色的新衣服」「用户送了生日礼物」）",
                required=True,
            ),
            ToolParameterInfo(
                name="change_value",
                param_type=ToolParamType.FLOAT,
                description=(
                    "可选的精确变化值（可正可负，如：轻微调侃 -1、诚心道歉 +2、"
                    "救命之恩 +8）。按互动的重要程度填写；不填时插件按互动类型自动取值。"
                ),
                required=False,
            ),
        ],
    )
    async def adjust_affection(
        self,
        user_id: str = "",
        interaction_type: str = "",
        description: str = "",
        change_value: float | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """根据互动调整用户好感度。

        Args:
            user_id: 取自本轮消息关系索引的 QQ 号，不接受昵称或人物 SID
            interaction_type: 互动类型
            description: 互动描述
            change_value: 可选的精确变化值（可正可负）；不填时按互动类型自动取值
            **kwargs: Host 注入的上下文，不用于推测用户

        Returns:
            调整结果
        """
        # 仅校验显式 QQ 号，不从 Host 上下文推测目标。
        resolved_user_id = self._resolve_user_id(user_id)
        if not resolved_user_id:
            return {"success": False, "error": "无法确定互动对象的用户标识"}

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
        requested_change = None
        if change_value is not None:
            try:
                requested_change = float(change_value)
            except (TypeError, ValueError):
                requested_change = None
            if requested_change is not None and not math.isfinite(requested_change):
                requested_change = None

        if requested_change is not None:
            # LLM 按互动重要程度给出的精确变化值，按类型约束在安全范围内
            if interaction_type == "negative":
                change_value = max(-10.0, min(requested_change, -0.1))
            elif interaction_type == "neutral":
                change_value = max(0.0, min(requested_change, 1.0))
            elif interaction_type == "special":
                change_value = max(1.0, min(requested_change, 20.0))
            else:
                change_value = max(0.1, min(requested_change, 10.0))
        elif interaction_type == "positive":
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
    # Hook: 接收时建立「会话 ↔ 消息 ↔ 发送者」索引
    # ════════════════════════════════════════════════════════════════
    # 群聊中会话按群维度建立，会话本身不携带发言人信息；唯一可靠的标识在
    # 每条消息的 message_info.user_info 中。本 Hook 在消息完成预处理后立即
    # 记录「会话 → (message_id, user_id)」映射（与入库、请求调度无关），
    # 之后 before_request 通过 prompt 中携带的 msg_id 精确反查当前发言人，
    # 不受「他人消息先于本轮请求写入消息库」的竞态影响。

    _PROMPT_MSG_ID_PATTERN = re.compile(r'msg_id="([^"]*)"')

    @HookHandler(
        "chat.receive.after_process",
        name="affection_index_speaker",
        description="在入站消息完成预处理后，记录会话内消息 ID 到发送者 QQ 号的映射，供好感度注入精确解析当前发言人",
        mode=HookMode.BLOCKING,
        order=HookOrder.EARLY,
        timeout_ms=2000,
        error_policy=ErrorPolicy.SKIP,
    )
    async def index_message_speaker(
        self,
        message: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """在消息接收阶段建立「会话 → 消息 ID → 发送者」索引。

        Args:
            message: 已完成预处理、序列化的入站 SessionMessage 字典
            **kwargs: 其他 Hook 参数

        Returns:
            Hook 分发结果：继续执行
        """
        del kwargs
        if not isinstance(message, dict):
            return {"action": "continue"}

        session_id = str(message.get("session_id") or "").strip()
        message_id = str(message.get("message_id") or "").strip()
        user_id = ""
        message_info = message.get("message_info")
        if isinstance(message_info, dict):
            user_info = message_info.get("user_info")
            if isinstance(user_info, dict):
                user_id = str(user_info.get("user_id") or "").strip()
        if not (session_id and message_id and user_id):
            return {"action": "continue"}

        # 保序去重：同一 message_id 重新接收时移到队尾
        dq = self._session_message_index.setdefault(session_id, deque(maxlen=100))
        for index, (existing_id, _existing_user) in enumerate(dq):
            if existing_id == message_id:
                del dq[index]
                break
        dq.append((message_id, user_id))

        # 调度自动评价（后台任务，不阻塞消息接收链路）
        self._schedule_auto_evaluation(session_id, user_id, message)
        return {"action": "continue"}

    @staticmethod
    def _extract_prompt_message_id(content: str) -> str:
        """从 planner prompt 消息内容中提取消息前缀携带的 msg_id。

        Maisaka 会把用户消息渲染为 `<message msg_id="xxx" time="..." user="...">\n内容`
        形式，序列化后的 content 保留完整前缀，因此可以从中解析出消息 ID，
        用于在接收索引中精确反查发送者。

        Args:
            content: prompt 中单条消息的 content 文本

        Returns:
            消息 ID；未找到时返回空字符串
        """
        match = AffectionPlugin._PROMPT_MSG_ID_PATTERN.search(content or "")
        return match.group(1).strip() if match else ""

    def _lookup_message_speaker(self, session_id: str, message_id: str) -> str:
        """按消息 ID 从接收索引反查发送者 QQ 号。

        Args:
            session_id: 会话 ID
            message_id: 目标消息 ID

        Returns:
            发送者用户标识；未命中时返回空字符串
        """
        dq = self._session_message_index.get(session_id)
        if not dq:
            return ""
        for indexed_id, indexed_user in dq:
            if indexed_id == message_id:
                return indexed_user
        return ""

    def _planner_speakers(self, items: list[dict[str, Any]], session_id: str) -> dict[str, str]:
        speakers = {}
        for item in items:
            if item["item_type"] != "UserMessageItem":
                continue
            for part in item["parts"]:
                if part["type"] != "text" or not part["text"].startswith("<message "):
                    continue
                message_id = self._extract_prompt_message_id(part["text"].splitlines()[0])
                user_id = self._lookup_message_speaker(session_id, message_id)
                if user_id:
                    speakers[message_id] = user_id
        return speakers

    @HookHandler(
        "maisaka.planner.before_request",
        name="affection_inject_before_request",
        mode=HookMode.BLOCKING,
        order=HookOrder.LATE,
        error_policy=ErrorPolicy.SKIP,
    )
    async def inject_affection_before_request(self, **kwargs: Any) -> dict[str, Any] | None:
        if (not self.config.plugin.enabled or not self.config.call_behavior.inject_affection_hint
                or random.random() >= self.config.call_behavior.inject_probability):
            return None
        users = {}
        for message_id, user_id in self._planner_speakers(kwargs["items"], kwargs["session_id"]).items():
            users.setdefault(user_id, []).append(message_id)
        records = [
            f"QQ={user_id}，msg_id={','.join(message_ids)}：\n{self._build_affection_hint(user_id)}"
            for user_id, message_ids in users.items()
        ]
        if not records:
            return None
        prompt = (
            "【按消息号匹配的好感度】身份认证决定固定身份，好感度只影响态度，不创建或取消主人身份。\n"
            + "\n".join(records)
        )
        item = {
            "item_type": "UserMessageItem",
            "meta": {"item_id": uuid4().hex, "logical_turn_id": None, "timestamp": datetime.now().isoformat()},
            "parts": [{"type": "text", "text": prompt}],
        }
        items = kwargs["items"]
        insert_at = len(items) - (bool(items) and items[-1]["item_type"] == "AssistantMessageItem")
        items.insert(insert_at, item)
        return {"action": "continue", "modified_kwargs": kwargs}

    @HookHandler(
        "maisaka.replyer.before_request",
        name="affection_inject_into_reply",
        mode=HookMode.BLOCKING,
        order=HookOrder.LATE,
        error_policy=ErrorPolicy.SKIP,
    )
    async def inject_affection_into_reply(self, **kwargs: Any) -> dict[str, Any] | None:
        if (not self.config.plugin.enabled or not self.config.call_behavior.inject_affection_hint
                or random.random() >= self.config.call_behavior.inject_probability):
            return None
        session_id = kwargs["session_id"]
        message_id = kwargs["reply_message_id"]
        if not message_id:
            return None
        user_id = self._lookup_message_speaker(session_id, message_id)
        if not user_id:
            result = await self.ctx.message.get_by_id(message_id, chat_id=session_id)
            if not result["success"]:
                raise RuntimeError(result["error"])
            message = result["message"]
            if message is None:
                return None
            user_id = str(message["message_info"]["user_info"]["user_id"])
        hint = self._build_affection_hint(user_id, for_replyer=True)
        hint += "\n以上仅表示对该账号的态度，不改变身份认证插件给出的固定关系。"
        kwargs["extra_prompt"] = "\n\n".join(filter(None, [kwargs["extra_prompt"], hint]))
        return {"action": "continue", "modified_kwargs": kwargs}

    # ════════════════════════════════════════════════════════════════
    # 自动评价：基于上下文与关系的互动综合评价
    # ════════════════════════════════════════════════════════════════
    # 设计说明：
    #   - 由 chat.receive.after_process 在用户消息到达时调度为后台任务，
    #     不阻塞消息接收与后续 Planner/Reply 流程。
    #   - 评价不依赖 Planner 的工具调用意愿：插件直接调用 Host 的 LLM，
    #     输入最近对话上下文、角色性格与当前关系等级，输出是否需要变化、
    #     方向、幅度与理由，再走统一的 _update_affection 落库。
    #   - 后台评价失败时记录异常，不阻断聊天主流程。

    def _schedule_auto_evaluation(
        self,
        session_id: str,
        user_id: str,
        message: dict[str, Any] | None,
    ) -> None:
        """调度一次自动评价后台任务（带用户级去重）。

        Args:
            session_id: 会话 ID
            user_id: 消息发送者用户标识
            message: 触发消息字典（用于前置过滤）
        """
        if self.config is None:
            return
        if not self.config.plugin.enabled or not self.config.evaluation.enabled:
            return
        session_id = str(session_id or "").strip()
        user_id = str(user_id or "").strip()
        if not (session_id and user_id):
            return

        # 命令 / 通知消息不评价
        if isinstance(message, dict):
            if message.get("is_command") or message.get("is_notify"):
                return

        # 同一用户已有活跃评价任务则跳过，避免跨群重复计分
        existing = self._evaluation_tasks.get(user_id)
        if existing is not None and not existing.done():
            return

        task = asyncio.create_task(self._auto_evaluate(session_id, user_id, message))
        self._evaluation_tasks[user_id] = task

    async def _auto_evaluate(self, session_id: str, user_id: str, message: dict[str, Any]) -> None:
        """执行一次自动评价：取上下文 → LLM 综合评价 → 按需调整好感度。

        后台任务失败时记录完整异常，不阻断聊天主流程。

        Args:
            session_id: 会话 ID
            user_id: 待评价的用户标识（QQ 号）
        """
        try:
            if self.config is None:
                return
            # 冷却期内不评价（也省一次 LLM 调用）
            if self._check_cool_down(user_id):
                return
            # 当日次数已满不评价
            if self._check_daily_limit(user_id):
                return
            # 被锁定用户不评价
            record = self._affection_data.get(user_id)
            if record is not None and record.get("locked", False):
                return

            # 1. 取最近对话上下文（含角色回复，用于判断互动氛围）
            context_messages = await self._fetch_recent_context_messages(
                session_id, self.config.evaluation.max_context_messages
            )
            context_messages = [msg for msg in context_messages if msg["message_id"] != message["message_id"]]
            context_messages = context_messages[-(self.config.evaluation.max_context_messages - 1):] + [message]

            # 2. 构造评价请求并调用 LLM
            prompt = self._build_evaluation_prompt(user_id, context_messages)
            result = await self.ctx.llm.generate(
                prompt,
                task_name=self.config.evaluation.model_task,
                temperature=self.config.evaluation.temperature,
                max_tokens=self.config.evaluation.max_response_tokens,
            )
            if not isinstance(result, dict) or not result.get("success"):
                self.ctx.logger.debug(
                    "自动评价调用失败 (session=%s, user=%s): %s",
                    session_id, user_id, result.get("error") if isinstance(result, dict) else result,
                )
                return
            response_text = str(result.get("response") or "").strip()
            if not response_text:
                return

            # 3. 解析评价结果
            evaluation = self._parse_evaluation_result(response_text)
            if not evaluation:
                self.ctx.logger.debug(
                    "自动评价输出解析失败 (session=%s, user=%s): %.200s",
                    session_id, user_id, response_text,
                )
                return
            if not evaluation["need_change"] or evaluation["direction"] == "neutral":
                return

            # 4. 幅度映射并落库
            direction = evaluation["direction"]
            change_value = self._magnitude_to_change_value(direction, evaluation["magnitude"])
            description = evaluation["reason"] or "自动评价"
            result = await self._update_affection(
                user_id=user_id,
                change_value=change_value,
                interaction_type=direction,
                description=f"[自动评价] {description}",
            )
            if result.get("success"):
                self.ctx.logger.info(
                    "自动评价生效: user=%s, %s %.1f → %.1f, 理由=%s",
                    user_id,
                    direction,
                    result.get("old_affection"),
                    result.get("new_affection"),
                    description,
                )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self.ctx.logger.warning("自动评价异常 (session=%s, user=%s): %s", session_id, user_id, e, exc_info=True)
        finally:
            # 仅当当前任务仍是注册中的任务时才清理，避免误删并发注册的新任务
            current = asyncio.current_task()
            if self._evaluation_tasks.get(user_id) is current:
                self._evaluation_tasks.pop(user_id, None)

    async def _fetch_recent_context_messages(
        self,
        session_id: str,
        limit: int,
    ) -> list[dict[str, Any]]:
        """获取会话最近消息用于评价上下文。

        Args:
            session_id: 会话 ID
            limit: 最多取多少条

        Returns:
            序列化消息字典列表（时间正序，不含命令/通知/无文本消息）
        """
        try:
            raw = await self.ctx.call_capability(
                "message.get_recent",
                chat_id=session_id,
                hours=24.0,
                limit=max(2, limit),
                limit_mode="latest",
                filter_mai=False,
            )
        except Exception as e:
            self.ctx.logger.debug("获取评价上下文失败 (%s): %s", session_id, e)
            return []
        if isinstance(raw, dict):
            raw = raw.get("messages") or []
        if not isinstance(raw, list):
            return []

        cleaned: list[dict[str, Any]] = []
        for msg in raw:
            if not isinstance(msg, dict):
                continue
            if msg.get("is_command") or msg.get("is_notify"):
                continue
            content = str(msg.get("processed_plain_text") or msg.get("content") or "").strip()
            if not content:
                continue
            cleaned.append(msg)
        return cleaned

    def _build_evaluation_prompt(
        self,
        user_id: str,
        context_messages: list[dict[str, Any]],
    ) -> str:
        """构造自动评价提示词。

        Args:
            user_id: 待评价的用户标识
            context_messages: 最近的序列化消息列表

        Returns:
            评价提示词文本
        """
        record = self._get_user_record(user_id)
        affection_value = record.get("affection", 0.0)
        level_info = _get_affection_level_info(affection_value)
        char_name = self.config.personality.character_name
        personality_hint = str(self.config.personality.character_personality_hint or "").strip()
        if not personality_hint:
            personality_hint = self.config.personality.personality_type

        lines = [
            f"你是一位「{char_name}」的角色好感度评估助手，"
            f"负责判断用户刚才的互动是否应改变{char_name}对该用户的好感度。",
            f"本次只评价 QQ={user_id} 的最后一条消息；其他成员的言行仅作上下文，不计入此人的好感。",
            f"角色性格概述：{personality_hint}",
            f"当前好感度：{level_info['value']} 点，等级「{level_info['level_name']}」"
            f"（{level_info['level_description']}）。",
            "",
            "判断规则：",
            "1. 同一言行在不同关系下的意义不同：在「亲近」「亲密」「思慕」等较高等级下，"
            "用户的关心、玩笑、打趣、暧昧、撒娇等是关系亲密的体现，应视为正向或中性；"
            "在较低等级下，轻浮或冒犯性的言行才更容易构成负面。",
            "2. 正向迹象：主动关心、记得角色说过的小事、顺着角色的话题、对角色情绪做出"
            "恰当回应、产生默契、主动陪伴、配合角色的玩笑等。",
            "3. 负面迹象：恶意贬低角色人格、背叛信任、无视边界、持续骚扰、恶意争吵。"
            "日常玩笑打闹与口是心非的傲娇互动不属于负面。",
            "4. 琐碎闲聊、单句问候、无实质内容的互动应判定为无需变化。",
            "",
            "最近对话：",
        ]
        for msg in context_messages:
            sender = self._extract_sender_label(msg)
            content = str(msg.get("processed_plain_text") or msg.get("content") or "").strip()
            speaker_id = msg["message_info"]["user_info"]["user_id"]
            lines.append(f"[QQ={speaker_id}，{sender}] {content}")

        lines.append("")
        lines.append("请只输出一个 JSON 对象，不要输出任何其他内容：")
        lines.append(
            '{"need_change": true或false, "direction": "positive"/"neutral"/"negative", '
            '"magnitude": 1-5, "reason": "一句话中文理由"}'
        )
        lines.append(
            "其中 magnitude：1=轻微，3=普通，5=明显；日常互动最高给 3，"
            "重大事件（如表白、送礼、重大帮助）通常由角色主动处理，自动评价一般给 1~2。"
        )
        return "\n".join(lines)

    @staticmethod
    def _extract_sender_label(msg: dict[str, Any]) -> str:
        """从序列化消息中提取发送者展示名。

        Args:
            msg: 序列化消息字典

        Returns:
            发送者昵称/群名片；无法识别时返回「未知用户」
        """
        message_info = msg.get("message_info")
        if not isinstance(message_info, dict):
            return "未知用户"
        user_info = message_info.get("user_info")
        if not isinstance(user_info, dict):
            return "未知用户"
        label = str(
            user_info.get("user_cardname") or user_info.get("user_nickname") or ""
        ).strip()
        return label or "未知用户"

    @staticmethod
    def _parse_evaluation_result(raw_text: str) -> dict[str, Any] | None:
        """从模型输出中容错解析评价 JSON。

        兼容模型输出带 markdown 代码块或前后解释性文字的情况。

        Args:
            raw_text: 模型原始输出

        Returns:
            规范化后的评价结果；解析失败返回 None
        """
        text = raw_text.strip()
        # 去掉 markdown 代码块围栏
        if text.startswith("```"):
            lines = text.splitlines()
            lines = lines[1:]
            if lines and lines[-1].strip().startswith("```"):
                lines = lines[:-1]
            text = "\n".join(lines).strip()
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            data = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return None
        if not isinstance(data, dict):
            return None

        need_change = bool(data.get("need_change", False))
        direction = str(data.get("direction", "neutral") or "").strip().lower()
        if direction not in {"positive", "negative", "neutral"}:
            direction = "neutral"
        try:
            magnitude = int(float(data.get("magnitude", 1)))
        except (TypeError, ValueError):
            magnitude = 1
        magnitude = max(1, min(5, magnitude))
        reason = str(data.get("reason", "") or "").strip()[:200]
        return {
            "need_change": need_change,
            "direction": direction,
            "magnitude": magnitude,
            "reason": reason,
        }

    def _magnitude_to_change_value(self, direction: str, magnitude: int) -> float:
        """把评价幅度映射为配置范围内的好感度变化值。

        magnitude 1~5 线性映射到配置区间：
            positive: [positive_interaction_min, positive_interaction_max]
            negative: [negative_interaction_max, negative_interaction_min]

        Args:
            direction: 互动方向（positive / negative）
            magnitude: 评价幅度 1~5

        Returns:
            配置范围内的变化值（含符号）
        """
        cfg = self.config.affection_rule
        ratio = (max(1, min(5, magnitude)) - 1) / 4.0
        if direction == "positive":
            return cfg.positive_interaction_min + (
                cfg.positive_interaction_max - cfg.positive_interaction_min
            ) * ratio
        if direction == "negative":
            return cfg.negative_interaction_max - (
                cfg.negative_interaction_max - cfg.negative_interaction_min
            ) * ratio
        return 0.0

    def _build_affection_hint(self, user_id: str, *, for_replyer: bool = False) -> str:
        """构造写入消息窗口的好感度提示文本。

        提示只包含当前等级与回应态度，不枚举行为清单——互动是否值得改变好感度
        由插件内自动评价与 Planner 按上下文综合判断，不在此处灌输规则。

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
            # 尚无记录，给出中性提示即可
            record = self._get_user_record(user_id)
        affection_value = record.get("affection", 0.0)
        level_info = _get_affection_level_info(affection_value)
        response_hint = self._get_response_hint(affection_value)

        char_name = self.config.personality.character_name if self.config else "角色"
        lines = [
            f"【好感度提示】你对当前这位用户的好感度为 {level_info['value']} 点，"
            f"属于「{level_info['level_name']}」等级：{level_info['level_description']}。",
            f"回应时应体现：{response_hint}",
        ]
        if record.get("locked", False):
            lines.append("（该用户好感度已被管理员锁定，请保持当前态度，不要试图改变。）")
        elif (
            not for_replyer
            and self.config
            and self.config.call_behavior.encourage_tool_calls
        ):
            lines.append(
                "如果本次互动与当前关系明显相关（值得加深或需要警惕），"
                "在回应之后可调用 adjust_affection 工具调整好感度。"
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
