# 更新日志

## 1.1.0

- 插件 ID 改为小写 `feyumy.affection-system`（符合 Manifest 校验规范），并新增旧数据目录自动迁移，升级后历史好感度不丢失。
- 补充 `README.md`、`LICENSE`、`CHANGELOG.md` 和插件内 `.gitignore`，达到插件仓库提交要求。
- `_manifest.json` 声明 `tomlkit` 依赖、`plugin_type` 与完整仓库链接。

## 1.0.3

- 修复：回复模型不再收到「请调用 adjust_affection 工具」的引导，避免把 `<function_calls>` 工具调用原文当正常消息发出。好感度工具调用引导仅注入给 planner。

## 1.0.2

- 优化 WebUI 好感度管理表的双向同步逻辑，避免重复写入配置。
