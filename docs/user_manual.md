# Notion Secretary 用户手册

本文覆盖部署前准备、运行步骤、Telegram 交互方式及常见问题，帮助最终用户快速上手秘书系统。

## 1. 环境准备

1. **安装 Python 3.10+**  
   Windows 推荐通过 Microsoft Store / Python.org。确保命令行可以执行 `python --version`。
2. **克隆或下载项目**至本地，例如 `D:\Projects\notion_secretary`。
3. **创建虚拟环境（可选）**  
   ```bash
   python -m venv .venv
   .venv\Scripts\activate   # PowerShell
   pip install -r requirements.txt
   ```
4. **配置 `config/settings.toml`**  
   ```bash
   cp config/settings.example.toml config/settings.toml
   ```
   编辑 `config/settings.toml`，填写：
   - `[notion] api_key`: 在 Notion 集成后台生成，需具备读取目标数据库的权限。
   - `[notion] sync_interval`: 数据同步间隔（秒）。
   - `[paths] data_dir`: 存放 `raw_json/`, `json/`, `telegram_history/` 等文件的目录。
   - `[telegram] token`: 通过 @BotFather 创建 Bot 后获得。
   - `[telegram] admin_ids`: Telegram 数字 ID，可在 @userinfobot 查询。
   - `[llm] provider/base_url/model/api_key/temperature`: 指定 OpenAI 或兼容服务以及模型（如 `gpt-4o-mini`），温度控制秘书语气。

## 2. 数据同步

项目依赖 Notion 数据库中的 Tasks / Projects / Logs。首次使用需要显式拉取一次，并保持后台同步。

1. **确认数据库 ID**  
   将 `database_ids.json` 填好：
   ```json
   {
     "tasks": "29b2...d4",
     "logs": "29b2...a3",
     "projects": "2562...51"
   }
   ```
2. **手动同步一次**  
   ```bash
   python database_collect.py --force
   ```
   - 脚本会调用 Notion API，结果写入 `DATA_DIR/raw_json`.
   - 同步完成后自动执行 `data_pipeline.processors`（projects / tasks / logs），生成结构化的 `processed_*.json`。
3. **持续同步（可选）**  
   ```bash
   python database_collect.py --loop
   ```
   该模式会按 `NOTION_SYNC_INTERVAL` 轮询，可结合系统服务/任务计划保持运行。若只运行 Telegram Bot，程序在启动后也会依据 `notion.sync_interval` 自动触发后台同步，并复用同一 `last_updated.txt` 判断是否需要执行，因此无需额外守护进程（但 `--loop` 方式可作为独立任务继续运行）。

## 3. 启动 Telegram Bot

1. **运行命令**
   ```bash
   python -m apps.telegram_bot.bot
   ```
2. **执行流程**
   - 程序读取 `config/settings.toml` 配置，构建 `HistoryStore` 和各类 service。
   - 进入长轮询模式，持续调用 `getUpdates`。
   - Bot 每次发送消息时，会把 Telegram 返回的 `message` 对象保存到 `DATA_DIR/telegram_history/<chat_id>.jsonl`，确保可以重建完整对话。
3. **多端通信**
   - 用户可在 Telegram 手机端或网页版直接与 Bot 对话，Bot 无需公网 IP（使用 long polling）。
   - 如需 Webhook，参考 `docs/telegram_architecture.md` 改造为服务器模式。

## 4. 常用指令与场景

| 指令/输入 | 说明 |
| --- | --- |
| `/tasks` 或 `/today` | 返回当前任务概览，按任务状态分组并根据优先级排序。 |
| `/tasks light [N]` / `/tasks group light [N]` | 精简视图：前者按优先级排序，后者按项目分组。 |
| `/focus` | 触发实时巡检，若有即将到期或异常任务，Bot 会发送警告语。 |
| `#log <内容>` | 快速记录日志。可追加 `task=<任务ID>` 绑定到指定任务。 |
| `/trackings` | 按序号展示当前跟踪任务，可搭配 `/untrack`。 |
| `/untrack [序号/关键词]` | 取消对应的跟踪任务；先查看 `/trackings` 获得序号后更方便。 |
| `/board` | 与 `/next` 相同的全局状态看板。 |
| 自由文本 | 暂未引入复杂多轮，非指令输入会提示可用命令。 |

系统典型场景：
- **Daily Briefing**：管理员在固定时间运行 `DailyBriefingWorkflow` 或等待 `/today` 查询，快速掌握任务推进。
- **Evening Review**：结合 `/focus` + Notion 日志统计，提醒未写日志的任务。
- **强制干预**：`StatusGuard` 根据任务 due date 决定是否发送“别再拖”类提示。

## 5. 数据与日志

- `DATA_DIR/raw_json`: 保存 Notion API 原始结果，可用于排查数据缺失。
- `DATA_DIR/json`: `processed_tasks.json` 等结构化文件，也是服务层的输入。
- `DATA_DIR/telegram_history`: 以 chat_id 为文件名的 JSON Lines，存储用户与 Bot 的全部消息。
- `databases/last_updated.txt`: 最近一次成功同步 Notion 的时间。

## 6. 测试与验证

推荐在每次修改后运行：
```bash
python -m pytest
```

涵盖内容：
- 历史记录读写（`tests/apps/telegram_bot/test_history_store.py`）
- Telegram 客户端对 Bot API 的封装（`tests/apps/telegram_bot/test_telegram_client.py`）
- 任务摘要排序逻辑（`tests/core/test_task_summary_service.py`）

## 7. 常见问题

1. **Bot 无响应 / 无法获取更新**  
   - 检查 `config/settings.toml` 中 `[telegram] token` 是否正确，是否开启了代理。
   - 确认 `HistoryStore` 中的 `metadata.json` 是否记录了过大的 `update_id`，如需重新拉取历史，可删除该文件并重启。
2. **Notion 数据未刷新**  
   - 查看 `database_collect.py --force` 输出是否出现 `Failed to fetch database`。常见原因是 API Key 权限不足。
   - 确保 `DATA_DIR` 可写，Windows 下避免只读目录。
3. **日志绑定任务失败**  
   - `#log` 命令中 `task=<id>` 需要提供 `processed_tasks.json` 中存在的任务 ID。可通过 `/tasks` 输出或直接查 JSON。

## 8. 后续扩展

- 根据 `docs/development_guide.md` 扩展更多 handler、workflows。
- 替换长轮询为 Webhook，并接入数据库存储多用户会话。
- 使用 `apscheduler` 在 `infra/scheduler` 中定义每日自动 briefing / review 任务。

---
如遇未覆盖的问题，可查看 `README.md`、`docs/telegram_architecture.md` 与 `docs/development_guide.md` 获取更深入的设计说明。 
