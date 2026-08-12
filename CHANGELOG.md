# 更新日志

## 1.1.2

- 修复：群聊中「他人消息先于本轮请求入库」导致好感度提示错配到其他发言人的问题。
- 新增 `chat.receive.after_process` Hook：消息到达即建立「会话 → 消息 ID → 发送者」接收索引（与入库、请求调度时机无关）。
- `maisaka.planner.before_request` 改为从 prompt 最后一条用户消息解析 `msg_id` 精确锚定当前发言人（MaiSaka 前缀 `<message msg_id="...">`），彻底规避竞态；replyer 阶段与工具调用使用「当前轮发言人缓存 → 接收索引最近发言人 → 消息库」逐级兜底。

## 1.1.1

- 修复：群聊中无法解析发言人用户标识的问题。群聊会话按群维度建立，聊天流不携带发言人 QQ 号（`chat.get_all_streams` 反查恒为空），且 Tool 调用上下文在群聊中也不注入 `user_id`，导致好感度提示始终退化为通用版本、`query_affection`/`adjust_affection` 无法定位互动对象。
- 新增：通过 `message.get_recent`（过滤机器人消息）反查会话最近一条用户消息的发送者，作为「会话 ↔ 当前发言人」的权威解析来源，群聊与私聊通用；Tool 解析与 Hook 注入两条路径均已接入。
- `_manifest.json` 新增 `message.get_recent` 能力声明。

## 1.1.0

- 插件 ID 改为小写 `feyumy.affection-system`（符合 Manifest 校验规范），并新增旧数据目录自动迁移，升级后历史好感度不丢失。
- 补充 `README.md`、`LICENSE`、`CHANGELOG.md` 和插件内 `.gitignore`，达到插件仓库提交要求。
- `_manifest.json` 声明 `tomlkit` 依赖、`plugin_type` 与完整仓库链接。

## 1.0.3

- 修复：回复模型不再收到「请调用 adjust_affection 工具」的引导，避免把 `<function_calls>` 工具调用原文当正常消息发出。好感度工具调用引导仅注入给 planner。

## 1.0.2

- 优化 WebUI 好感度管理表的双向同步逻辑，避免重复写入配置。
