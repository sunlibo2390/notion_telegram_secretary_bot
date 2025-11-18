# Docs Overview

| 文件 | 说明 |
| --- | --- |
| `user_manual.md` | 面向用户的部署&指令手册：覆盖环境准备、运行顺序、常用命令、FAQ。 |
| `development_guide.md` | 接口契约与测试策略，对应 Handler/Service/Repository 的签名约束。 |
| `developer_overview.md` | 当前架构、数据流、任务追踪持久化与扩展注意事项的总览。 |
| `telegram_architecture.md` | Telegram 长轮询、历史拼接、主动策略等细节。 |
| `user_profile_doc*.md` | 用户画像（敏感信息，Git 已忽略，按需本地维护）。 |

### 建议阅读路径
1. 想了解整体模块 & 运行方式：先读 `developer_overview.md`，再按需查看 `telegram_architecture.md`。
2. 要实现新功能：结合 `development_guide.md` 中的接口签名，以及 `tests/` 里的用例。
3. 使用/部署：查看 `README.md` 与 `docs/user_manual.md`。

### 编辑约定
- 更新文档时同步修改本 README 中的表格，保持“**该文件负责什么**”可见。
- 涉及隐私（`config/settings*.toml`, `user_profile_doc*.md`, `databases/`）的文件不应提交，必要时在 `.gitignore` 中维护忽略项。
