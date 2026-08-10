# 好感度系统插件（affection-system）

为 MaiBot 角色扮演提供好感度追踪与长期记忆的第三方插件。记录不同用户与角色的好感度，根据互动质量动态调整，并让角色按对应好感度等级自然回应。

- 插件 ID：`feyumy.affection-system`
- 许可证：MIT

## 功能特性

- 记录每位用户与角色的好感度值（范围 `-20 ~ 100`）。
- 六级好感度划分：**不喜 / 正常 / 熟悉 / 亲近 / 亲密 / 思慕**。
- 根据角色性格类型（傲娇 / 温柔 / 高冷 / 开朗 / 毒舌 等）和互动质量自动调整好感度。
- 不同好感度等级对应不同的回应风格提示，注入给 LLM 让角色言行更贴合关系亲疏。
- 好感度数据持久化存储于插件数据目录（`data/plugins/<插件 ID>/affection_data.json`）。
- 支持 WebUI 可视化配置与管理：
  - 「好感度管理」配置节以可编辑表格展示各用户好感度，可直接查看、修改，保存后立即生效。
  - 锁定条目后，LLM 将不再自动调整该用户好感度。
  - 运行时变化会自动同步回配置表，保证 WebUI 展示最新数值。
- 提供 `query_affection` / `adjust_affection` 工具供 LLM 调用。
- 提供 `/affection` 命令供用户查询自己的好感度。

## 安装

1. 将整个 `affection-system` 目录放入 MaiBot 的 `plugins/` 目录（注意不要修改主程序代码）。
2. 在 MaiBot 插件管理页中启用「好感度系统」，或使用 `/pm plugin load affection-system`。
3. 首次加载后，Runner 会根据 `config_model` 自动生成 `config.toml`，无需手动创建。

若从旧版本（插件 ID 为 `Feyumy.affection-system`）升级，插件首次加载时会自动把旧数据目录中的 `affection_data.json` 迁移到新目录，历史好感度不会丢失。

## 配置项

配置通过 WebUI 插件配置页或 `plugins/affection-system/config.toml` 修改，保存后立即生效。

### `[plugin]` 插件基础设置

| 字段 | 说明 | 默认值 |
| --- | --- | --- |
| `enabled` | 是否启用好感度系统 | `true` |

### `[personality]` 角色性格

| 字段 | 说明 | 默认值 |
| --- | --- | --- |
| `character_name` | 角色名称，用于提示与展示 | `麦麦` |
| `personality_type` | 性格类型：默认 / 傲娇 / 温柔 / 高冷 / 开朗 / 毒舌 | `默认` |
| `affection_gain_multiplier` | 好感度变化倍率（>1 更快，<1 更慢） | `1.0` |
| `affection_decay_enabled` | 是否启用好感度自然衰减 | `false` |
| `affection_decay_days` | 超过多少天未互动开始衰减 | `30` |
| `affection_decay_rate` | 每次衰减的好感度点数 | `0.5` |

### `[affection_rule]` 变化规则

| 字段 | 说明 | 默认值 |
| --- | --- | --- |
| `positive_interaction_min/max` | 正面互动（赞美、关心等）增加范围 | `0.5 ~ 3.0` |
| `negative_interaction_min/max` | 负面互动（冒犯、争吵等）减少范围 | `-3.0 ~ -0.5` |
| `neutral_interaction_value` | 中性互动（普通聊天）增值 | `0.1` |
| `special_interaction_value` | 特殊互动（送礼、表白等）增值 | `5.0` |
| `cool_down_seconds` | 同一位用户两次变更的最小间隔（秒） | `60` |
| `daily_limit` | 每位用户每天最多变更次数 | `20` |

### `[response]` 回应风格

不同好感度等级下注入给 LLM 的回应风格提示，共 6 项：`dislike_response_hint`、`normal_response_hint`、`familiar_response_hint`、`close_response_hint`、`intimate_response_hint`、`adore_response_hint`。

### `[user_management]` 好感度管理

| 字段 | 说明 | 默认值 |
| --- | --- | --- |
| `sync_to_config` | 运行时好感度变化是否自动同步到下方管理表 | `true` |
| `managed_users` | 好感度管理表（WebUI 中可编辑），支持 `user_id`、`affection`、`locked`、`note` | 空 |

### `[call_behavior]` 调用行为

| 字段 | 说明 | 默认值 |
| --- | --- | --- |
| `inject_affection_hint` | 是否在回复前自动注入好感度提示 | `true` |
| `inject_probability` | 好感度提示注入概率（0~1） | `1.0` |
| `encourage_tool_calls` | 是否提示 LLM 在互动后主动调用 `adjust_affection` | `true` |

## 命令

| 命令 | 说明 |
| --- | --- |
| `/affection` | 查询当前用户与角色的好感度及等级 |

## 权限 / 能力说明

插件仅在需要时向 Host 申请以下能力：

- `send.text`：发送查询结果等文本消息。
- `chat.get_all_streams`：根据会话反查发言者身份（私聊场景）。
- `person.get_id_by_name`：把昵称解析为稳定的用户 ID，避免同一人被拆成两条记录。

插件运行在独立 Runner 子进程中，不导入 `src.*`，所有能力均通过 `self.ctx` 代理调用。

## 常见问题

### 聊天里出现 `<function_calls>...` 之类的工具调用原文

这是旧版本插件把「请调用 adjust_affection 工具」的引导同时注入给了 replyer 模型（replyer 没有工具定义），模型便以 `<function_calls>` 文本形式模仿调用并原样发出。`1.0.3` 起回复模型只会收到好感度态度提示，不再收到工具调用引导。请更新插件并重载；若仍出现，可临时关闭 `call_behavior.encourage_tool_calls`。

### 好感度一直没有变化

按顺序检查：

1. 是否在冷却期（默认 60 秒）或今日次数已达上限。
2. 该用户是否在「好感度管理」表中被 `locked` 锁定。
3. `call_behavior.encourage_tool_calls` 是否开启。
4. planner 模型是否支持工具调用；日志中搜索 `好感度` 可看到具体拒绝原因。

### 数据保存在哪里

好感度数据保存在 `data/plugins/<插件 ID>/affection_data.json`（默认 ID 为 `feyumy.affection-system`）。手动备份插件数据时复制该文件即可。

### 如何更新插件

用 Git 管理插件目录，服务器上 `git pull` 后热重载即可（WebUI 插件管理页或 `/pm plugin reload affection-system`），无需重启 MaiBot。注意插件自身 `config.toml` 不入库，更新时不会被覆盖。

## 开发与提交

- 入口文件 `plugin.py`，工厂函数 `create_plugin()`。
- 插件仅修改 `plugins/affection-system/` 内文件，不改动主程序。
- 插件目录内的 `.gitignore` 已忽略 `/config.toml`、`config_back/`、`__pycache__/` 等本地文件。
- 提交前请阅读 [MaiBot 插件仓库贡献指南](https://github.com/Mai-with-u/plugin-repo/blob/main/CONTRIBUTING.md)。
