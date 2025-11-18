# Notion Secretary

[English Version](README.en.md)

> 面向 Telegram 的全能助理：实时同步 Notion 任务/日志，记录用户行为，并通过 LLM 技能驱动提醒与自动化。  
> 所有命令与提醒都会进入 Agent 回路：Bot 负责收集上下文，LLM 选择应调用的“技能”，再由 Bot 确定性地执行。

---

## 🌟 Key Features

| 能力 | 说明 |
| --- | --- |
| Notion 同步 | `database_collect.py` / `/update` 拉取数据库 → processors 标准化 → repositories 缓存。 |
| Telegram Bot | `/tasks`、`/logs`、`/track`, `/trackings`, `/board` 等命令 + 自然语言对话；支持批量删除、精简视图等。 |
| LLM Agent | `core/llm/agent.py` + `core/llm/tools.py` 实现 ReAct/工具调用；所有指令都走模型判定。 |
| 跟踪持久化 | `TaskTracker` 将所有跟踪任务存入 `history_dir/tracker_entries.json`，重启后自动恢复。 |
| Rest / 勿扰 | `/blocks` 创建休息/任务窗口，`TaskTracker` 会避开休息期，`ProactivityService` 也会暂停追问。 |
| 配置与日志 | `config/settings.toml` 控制 Notion/Telegram/LLM/时区，所有日志写入 `logs/`。 |

更多细节请参考：`docs/developer_overview.md`（架构）、`docs/development_guide.md`（接口契约）、`docs/user_manual.md`（部署与指令）。

---

## 🗂 目录结构

```
notion_secretary/
├── apps/telegram_bot/         # Bot 运行时：命令处理器、跟踪器、会话监控
├── core/                      # 领域模型、服务、仓库与 LLM glue 逻辑
├── data_pipeline/             # Notion 抽取/处理/转换/存储流水线
├── docs/                      # 用户手册与开发者文档（参见 docs/README.md）
├── infra/                     # 配置解析、Notion 同步编排
├── scripts/                   # run_bot.py / sync_databases.py 等脚本
├── tests/                     # pytest 测试
└── databases/                 # 运行期数据（raw_json/json/telegram_history 等）
```

---

## ⚙️ 配置（config/settings.toml）

```toml
[general]
timezone_offset_hours = 8              # 本地时区，默认 UTC+8，可设为 -12~+14

[paths]
data_dir = "./databases"
database_ids_path = "database_ids.json"

[notion]
api_key = "secret_xxx"
sync_interval = 1800                    # /update 背景轮询间隔
force_update = false
api_version = "2022-06-28"

[telegram]
token = "123456:ABCDE"                  # BotFather 获取
poll_timeout = 25
admin_ids = [ {telegram user id} ]                  # GetUserID 获取

[llm]
provider = "openai"
base_url = "https://api.openai.com/v1"
model = "gpt-4o-mini"
temperature = 0.3
api_key = "sk-..."

[tracker]
interval_seconds = 1500                 # 默认跟踪间隔（秒），自定义命令可覆盖
follow_up_seconds = 600

[proactivity]
state_check_seconds = 300
state_stale_seconds = 3600
state_prompt_cooldown_seconds = 600
question_follow_up_seconds = 600
state_unknown_retry_seconds = 120
```

> **敏感文件**（settings、user_profile、databases/**、tracker_entries.json 等）已加入 `.gitignore`，请勿提交。

---

## 🗃️ Notion 数据库字段

为了保证 processors 与本地缓存工作正常，Notion 中的数据库需要提供下列属性（大小写需保持一致）：

### Tasks 数据库
| 字段 | 类型 | 用途 |
| --- | --- | --- |
| `Name` | title | 任务标题，`TaskRepository` 用于展示和查找 |
| `Priority` | select | 优先级标签，用于排序 |
| `Status` | status | 用于过滤完成/休眠任务 |
| `Projects` | relation | 关联项目，`/tasks group` 需要 |
| `Due Date` | date | 截止时间，`/next` 与提醒逻辑参考 |
| `Subtasks` | relation | 反查子任务名并在提示中展示 |

### Projects 数据库
| 字段 | 类型 | 用途 |
| --- | --- | --- |
| `Name` | title | 项目名称 |
| `Status` | status | 判定项目是否激活 |

### Logs 数据库
| 字段 | 类型 | 用途 |
| --- | --- | --- |
| `Name` | title | 日志标题／摘要 |
| `Status` | status | 过滤 Done/Dormant 日志 |
| `Task` | relation | 用于 `/logs` 里展示关联任务 |

对应的数据库 ID 统一写在 `database_ids.json`（已被 `.gitignore` 忽略）；如果需要自定义路径，可以在 `config/settings.toml` 的 `[paths].database_ids_path` 中覆盖。

---

## 🚀 快速上手

1. **安装依赖**
   ```bash
   python -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```
2. **复制配置**
   ```bash
   cp config/settings.example.toml config/settings.toml
   ```
3. **填充 Notion database IDs** → `database_ids.json`
4. **首次同步数据**
   ```bash
   python scripts/sync_databases.py --force
   ```
5. **启动 Telegram Bot**
   ```bash
   python scripts/run_bot.py
   ```

> `/update` 会拉起后台线程同步 Notion 数据，不会阻塞其他命令。

---

## 💬 常用命令

| 命令 | 说明 |
| --- | --- |
| `/tasks [N]` | 任务列表，带状态/优先级/截止时间。自建任务无链接。 |
| `/tasks light [N]` / `/tasks group light [N]` | 精简视图：只展示任务名+项目，可按项目分组。 |
| `/tasks delete <序号...>` | 批量删除自建任务（Notion 任务不可删）。 |
| `/logs [N]` | 查看最近 N 条日志（纯文本输出，避免 Telegram Markdown 错误）。 |
| `/logs delete <序号...>` | 批量删除最近 `/logs` 输出中选定的日志。 |
| `/track <任务ID/名称> [分钟]` | 启动跟踪，任意 ≥5 分钟间隔（LLM 会自动换算 8 小时→480 分钟）。 |
| `/trackings` | 列出跟踪任务，含“下一次提醒”时间；`/untrack <序号/关键词>` 取消。 |
| `/board` / `/next` | 综合看板：行动/心理状态、提问追踪、所有跟踪任务时间、时间块 diff。 |
| `/blocks` / `/blocks cancel <序号>` | 查看或取消休息/任务时间块。 |
| `/update` | 后台同步 Notion → processors → repositories（执行结果会另行通知）。 |

详尽命令说明见 `docs/user_manual.md`。

---

## 🔁 数据流与运行

1. **Notion -> 本地**：`NotionCollector` 根据 `last_updated.txt` 判定是否拉取 → processors（projects/tasks/logs） → `databases/json`.
2. **本地 -> Repositories**：`TaskRepository` `LogRepository` `ProjectRepository` 读取 processed JSON + 自建缓存（`agent_tasks.json`/`agent_logs.json`）。
3. **Agent Loop**：`LLMAgent` 汇总 Telegram 历史 + 画像 + repositories 数据 → 调用工具 → 生成回复或下一步指令。
4. **Tracker/Rest**：`TaskTracker` 将活动持久化，遇到休息窗口只在必要时顺延；`ProactivityService` 在后台监控状态并触发 `/next`/追问。
5. **Telemetry & Logs**：所有命令输出存入 `databases/telegram_history/`，tracker 状态写入 `history_dir/tracker_entries.json`。

---

## 🧪 测试与调试

```bash
python -m pytest
```

重点用例：
* `tests/apps/telegram_bot/test_command_router_tasks.py`：命令路由、批量删除、跟踪序号等。
* `tests/apps/telegram_bot/test_tracker.py`：多任务跟踪、休息窗口、持久化恢复。
* `tests/core/test_llm_agent.py`：LLM 工具循环。

---

## 📚 文档索引

| 文件 | 内容 |
| --- | --- |
| [`docs/README.md`](docs/README.md) | 文档导航。 |
| [`docs/developer_overview.md`](docs/developer_overview.md) | 架构/数据流/扩展注意事项。 |
| [`docs/development_guide.md`](docs/development_guide.md) | 接口契约、流程与测试策略。 |
| [`docs/user_manual.md`](docs/user_manual.md) | 部署与指令说明。 |
| [`docs/telegram_architecture.md`](docs/telegram_architecture.md) | 长轮询、历史拼接、主动策略。 |

用户画像 (`docs/user_profile_doc*.md`) 含隐私信息，实际部署时请在本地维护，不要提交。

---

## ❓ FAQ

1. **命令被卡住？**  
   `/update`、Notion 同步等耗时操作均在后台线程执行，如仍阻塞请检查是否有长时间运行的自定义逻辑。

2. **日志展示 400 错误？**  
   `/logs` 输出已改为纯文本（`markdown=False`）。如二次开发中重新启用 Markdown，请务必转义超长内容。

3. **跟踪与休息冲突？**  
   `TaskTracker` 会在启动时检查当前是否处于休息期，仅当默认提醒会落入休息窗口时才顺延，否则保持原提醒时间；`/trackings` 与 `/board` 显示的时间一致。

4. **时区如何设置 UTC-12？**  
   在 `config/settings.toml` 设置 `[general].timezone_offset_hours = -12`（或设置环境变量 `TIMEZONE_OFFSET_HOURS=-12`）。启动时会自动调用 `configure_timezone(-12)`。

---

## 🤝 贡献指南

1. 修改前阅读 `docs/developer_overview.md` 和 `docs/development_guide.md`。
2. 遵循模块边界：数据采集 → processors → repositories → services → handlers → agent；不要跨层访问。
3. 新增命令要考虑 Telegram Markdown 兼容性；需要输出大量文本时可改为纯文本。
4. 所有长耗时操作（Notion 同步、批量任务处理）都应使用后台线程或异步流程。
5. 提交前运行 `pytest`，敏感文件确保未加入版本控制。

欢迎 issue / PR，内置文档与测试可帮助你快速定位改动影响。祝 hacking 愉快！
