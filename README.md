# 好感度系统插件（affection-system）

为 MaiBot 角色扮演提供好感度追踪与长期记忆的第三方插件。记录不同用户与角色的好感度，基于对话上下文与角色关系综合评价互动质量，动态调整好感度，并让角色按对应好感度等级自然回应。

- 插件 ID：`feyumy.affection-system`
- 许可证：MIT

## 本地适配版本 1.2.2

适配 MaiBot 1.3.8 的 Context Items：Planner 按消息号注入 QQ 与好感关系，Replyer 按 `reply_message_id` 精确匹配，不使用群内最近发言者推测目标。工具参数 `user_id` 必须填写关系索引中的 QQ 号，不接受昵称或人物 SID。两个工具不公开 `stream_id` 参数，也不根据 Host 附带的上下文推测目标用户。

好感记录按 QQ 跨群共用；好感只改变态度，不创建或取消身份认证插件的固定关系。自动评价明确绑定触发消息及其 QQ 号，其他成员消息只作上下文；同一用户跨群只运行一个评价任务。具体经历仍由 MaiBot 原生记忆管理。

Replyer 的消息索引未命中时，按当前 SDK 返回的消息本体读取发送者 QQ 号。消息不存在时跳过注入；查询失败或消息缺少有效发送者时，由现有 Hook 错误处理记录诊断，不使用昵称或其他发言人推测目标。

`query_affection` 仅在当前上下文缺少完成任务所需的好感度信息时按需调用。已有对应用户的信息且足够使用时，无需重复查询；普通回复和关系评价提问都不要求先调用查询工具。工具的参数、返回内容和可见性不变。

自动注入和入站自动评价按宿主 `bot.qq_account` 排除机器人自身，不按昵称判断。聊天历史中的自身消息仍保留，但不会附加对自身的好感度，也不会因此创建自身好感记录；已有自身记录不自动删除。当前没有黑名单配置，管理表的「锁定」仅禁止自动改分，不控制提示注入。

## 功能特性

- 记录每位用户与角色的好感度值（范围 `-20 ~ 100`）。
- 六级好感度划分：**不喜 / 正常 / 熟悉 / 亲近 / 亲密 / 思慕**。
- **插件内 LLM 自动评价**：用户消息到达后，插件直接调用 LLM，基于最近对话上下文、角色性格与当前关系等级综合判断本次互动是否值得改变好感度（不依赖关键词行为分类，也不需要 Planner 主动调用工具）。
- **关系感知的数值阻尼**：高好感度时负面扣分按比例缩小（亲密关系经得起小摩擦）；同一天连续负面调整超过上限后扣分递减（防止好感度断崖下跌）。
- 提供 `query_affection` / `adjust_affection` 工具供 Planner 直接调用（无需 `tool_search` 发现），用于重大事件与自动评价遗漏的场景。
- 不同好感度等级对应不同的回应风格提示，注入给 LLM 让角色言行更贴合关系亲疏。
- 好感度数据持久化存储于插件数据目录（`data/plugins/<插件 ID>/affection_data.json`）。
- 支持 WebUI 可视化配置与管理：
  - 「好感度管理」配置节以可编辑表格展示各用户好感度，可直接查看、修改，保存后立即生效。
  - 锁定条目后，LLM 将不再自动调整该用户好感度。
  - 运行时变化会自动同步回配置表，保证 WebUI 展示最新数值。
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
| `character_personality_hint` | 角色性格简述（供自动评价参考），如「傲娇、贪财、口嫌体正直的巫女」 | 空 |
| `affection_gain_multiplier` | 好感度变化倍率（>1 更快，<1 更慢） | `1.0` |
| `affection_decay_enabled` | 是否启用好感度自然衰减 | `false` |
| `affection_decay_days` | 超过多少天未互动开始衰减 | `30` |
| `affection_decay_rate` | 每次衰减的好感度点数 | `0.5` |

### `[affection_rule]` 变化规则

| 字段 | 说明 | 默认值 |
| --- | --- | --- |
| `initial_affection` | 新用户首次互动时的初始好感度 | `10.0` |
| `positive_interaction_min/max` | 正面互动增加范围 | `0.5 ~ 3.0` |
| `negative_interaction_min/max` | 负面互动减少范围 | `-3.0 ~ -0.5` |
| `neutral_interaction_value` | 中性互动（普通聊天）增值 | `0.1` |
| `special_interaction_value` | 特殊互动（送礼、表白等）增值 | `5.0` |
| `cool_down_seconds` | 同一位用户两次变更的最小间隔（秒） | `60` |
| `daily_limit` | 每位用户每天最多变更次数 | `20` |
| `high_affection_negative_damper_threshold` | 高位阻尼阈值：好感度高于此值时负面扣分缩小 | `40.0` |
| `high_affection_negative_damper_multiplier` | 高位阻尼系数（扣分乘以该系数） | `0.5` |
| `negative_spiral_limit` | 同一天连续负面调整达到该次数后扣分递减 | `3` |
| `negative_spiral_decay` | 负面螺旋阻尼系数（每次扣分乘以该系数） | `0.5` |

> 提示：`adjust_affection` 工具支持可选参数 `change_value`，Planner 可按互动的重要程度给出精确变化值（如轻微调侃 `-1`、诚心道歉 `+2`、重大帮助 `+8`）；不填写时按各类型的默认范围自动取值。所有变化都会经过角色性格倍率与关系阻尼。

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
| `encourage_tool_calls` | 是否在提示末尾附上「可调用 adjust_affection」提醒 | `false` |

### `[evaluation]` 自动评价

| 字段 | 说明 | 默认值 |
| --- | --- | --- |
| `enabled` | 是否启用插件内 LLM 自动评价 | `true` |
| `model_task` | 评价使用的模型任务名（`model_config.toml` 中的 `[model_task_config.*]`） | `utils` |
| `max_context_messages` | 评价时提供的最近对话条数 | `8` |
| `max_response_tokens` | 评价请求最大输出 token | `1024` |
| `temperature` | 评价温度（越低越稳定） | `0.2` |

## 命令

| 命令 | 说明 |
| --- | --- |
| `/affection` | 查询当前用户与角色的好感度及等级 |

## 权限 / 能力说明

插件仅在需要时向 Host 申请以下能力：

- `send.text`：发送查询结果等文本消息。
- `config.get`：读取宿主机器人 QQ 号，用于排除自身。
- `message.get_by_id`：热重载后索引未命中时，按回复消息号精确查询目标发送者。
- `message.get_recent`：读取自动评价上下文，不用于猜测回复目标。
- `llm.generate`：插件内调用 LLM 对互动做上下文综合评价。

插件运行在独立 Runner 子进程中，不导入 `src.*`，所有能力均通过 `self.ctx` 代理调用。

## 常见问题

### 聊天里出现 `<function_calls>...` 之类的工具调用原文

这是旧版本插件把「请调用 adjust_affection 工具」的引导同时注入给了 replyer 模型（replyer 没有工具定义），模型便以 `<function_calls>` 文本形式模仿调用并原样发出。`1.0.3` 起回复模型只会收到好感度态度提示，不再收到工具调用引导；`1.2.0` 起好感度工具直接可见，注入提示也不再包含工具调用引导。

### 好感度一直没有变化

按顺序检查：

1. 是否在冷却期（默认 60 秒）或今日次数已达上限。
2. 该用户是否在「好感度管理」表中被 `locked` 锁定。
3. `evaluation.enabled` 是否开启（自动评价）；`call_behavior.inject_affection_hint` 是否开启（回应风格注入）。
4. 评价模型任务名（`evaluation.model_task`）是否有效；日志中搜索 `自动评价` 或 `好感度` 可看到具体原因。

### 为什么有时互动明显却不变好感度

自动评价与 Planner 工具都会判断「本次互动是否有实质意义」：琐碎闲聊、单句问候、重复的打闹通常不改变好感度；同时冷却期（60 秒）与每日上限会限制变更频率。若某个互动确实重要（表白、送礼、重大帮助等），Planner 可主动调用 `adjust_affection` 记录。

### 数据保存在哪里

好感度数据保存在 `data/plugins/<插件 ID>/affection_data.json`（默认 ID 为 `feyumy.affection-system`）。手动备份插件数据时复制该文件即可。

### 如何更新插件

用 Git 管理插件目录，服务器上 `git pull` 后热重载即可（WebUI 插件管理页或 `/pm plugin reload affection-system`），无需重启 MaiBot。注意插件自身 `config.toml` 不入库，更新时不会被覆盖。

## 开发与提交

- 入口文件 `plugin.py`，工厂函数 `create_plugin()`。
- 插件仅修改 `plugins/affection-system/` 内文件，不改动主程序。
- 插件目录内的 `.gitignore` 已忽略 `/config.toml`、`config_back/`、`__pycache__/` 等本地文件。
- 提交前请阅读 [MaiBot 插件仓库贡献指南](https://github.com/Mai-with-u/plugin-repo/blob/main/CONTRIBUTING.md)。
