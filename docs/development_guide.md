# Development Guide

围绕 Telegram long-polling Bot 与 Notion 数据同步，本文在 README 的基础上细化接口契约、场景流程、配置与测试策略，方便多角色协作。如果你需要先了解整体架构、数据流和运行约束，请先阅读 `docs/developer_overview.md`，再回到本文查看各模块的契约细节。

## 1. 接口契约

### 1.1 TelegramBotClient (`apps/telegram_bot/clients/telegram_client.py`)
```python
class TelegramBotClient:
    def get_updates(self, offset: int | None = None, timeout: int = 30) -> list[Update]: ...
    def send_message(self, chat_id: int, text: str, **kwargs) -> BotMessage: ...
    def send_photo(self, chat_id: int, photo: bytes | str, caption: str | None = None) -> BotMessage: ...
```
- `get_updates` 应封装 `https://api.telegram.org/bot{TOKEN}/getUpdates`，接受 `offset` 和 `timeout`（长轮询秒数），返回解析后的列表。
- `send_*` 方法必须在成功响应后把 `resp["result"]` 传给 `HistoryStore.append_bot`.
- 失败（网络 / 4xx / 5xx）必须抛出自定义 `TelegramAPIError`，由调用者决定重试策略。

### 1.2 HistoryStore (`apps/telegram_bot/history/history_store.py`)
```python
class HistoryStore:
    def append_user(self, update: Update) -> None: ...
    def append_bot(self, message: BotMessage) -> None: ...
    def get_history(self, chat_id: int, limit: int = 50) -> list[HistoryEntry]: ...
    def last_update_id(self) -> int | None: ...
```
- `HistoryEntry` 字段：`timestamp`, `chat_id`, `message_id`, `direction`, `text`, `entities`, `reply_to_message_id`.
- 推荐实现：`data_pipeline/storage/telegram_history/{chat_id}.jsonl` + `metadata.json` 存储 `last_update_id`; 可无缝替换为 SQLite。
- `append_*` 要保证幂等（同一 message_id 不重复写入）。

### 1.3 Repositories (`core/repositories`)
- `TaskRepository`：`list_active_tasks() -> list[Task]`, `get_task(task_id) -> Task | None`.
- `LogRepository`：`list_logs(from_date, to_date)`, `create_log(text, related_task_id)`.
- `ProjectRepository`：`list_active_projects()`.
每个 repository 统一读取 `data_pipeline/storage/processed/*.json`，必要时刷新缓存。

### 1.4 Services & Workflows（技能层）
- `TaskSummaryService.build_today_summary()`：聚合任务 + 日程 + 画像标签，供 LLM 工具 `summarize_tasks` 使用。
- `StatusGuard.evaluate()`：返回 `[Intervention]`，供 LLM 判断是否升级干预。
- `LogbookService.record_log(raw_text)`：解析 `#log` 指令或 LLM 生成的日志内容，写 Notion/本地缓存，返回执行结果。
- `DailyBriefingWorkflow.run(chat_id)`：当 LLM 选择 `generate_briefing` 工具时调用，返回 briefing 文本 + 关键信息。

### 1.5 LLM Agent Contracts (`core/llm`)
- `AgentContextBuilder.build(chat_id, latest_update)`：返回 prompt 片段（system persona + user profile + recent history + observation）。
- `ToolRegistry.register(name, schema, executor)`：向 LLM 暴露技能（task summary、logbook、status guard等）。
- `AgentLoop.run(chat_id, user_input)`：
  1. 组装上下文，调用 OpenAI Chat Completions（function calling）
  2. 如果模型返回 `tool_call`，执行对应 executor，得到 `observation`
  3. 将 observation 写回 prompt，继续循环，直到得到 `assistant` 最终答案或超出最大回合
  4. 返回 `FinalMessage`、使用过的工具、token 统计

接口文档放在此文件，实施代码时保持函数签名一致。

## 2. 场景流程

### 2.1 Daily Briefing（早间播报）
1. `infra/scheduler` 定时任务触发 `AgentLoop.run(chat_id, "/today")`。
2. Agent 根据指令决定是否调用 `summarize_tasks`、`generate_briefing`、`status_guard` 等工具。
3. LLM 将工具返回的数据组织成 summary + 行动要求，语气遵循画像（讽刺/强制）。
4. 最终消息经 `TelegramBotClient` 推送，并写入 `HistoryStore`。

### 2.2 Evening Review（晚间复盘）
1. Scheduler 在 21:30 调用 Agent，Observation 说明需要“复盘 + 未写日志追杀”。
2. Agent 调用 `TaskSummaryService`、`LogRepository` 工具获取数据。
3. 若用户拖延，Agent 可继续追问或直接调用 `record_log` 工具帮助草稿记录。

### 2.3 实时强制干预
1. `StatusGuard.evaluate` 检查 processed 数据（长时间未推进、逃避行为、临期任务等）。
2. Agent 获取 `Intervention`，结合画像判定语气、是否要求立即反馈。
3. LLM 决定是否设置后续提醒或调用其他技能（如 `record_log` 记录借口）。

### 2.4 用户指令 / 自然语言
1. Handler 不再区分命令与自由文本，统一将输入传入 Agent。
2. Agent 判断意图：需要数据时调用工具；需要情绪输出时直接生成语句；可主动请求更多细节。
3. 如 LLM 多次调用工具仍无结论，可输出“我没法继续，去检查 Notion”并提示用户。

### 2.5 `#log` 自由文本记录
1. Handler 识别 `message.text` 以 `#log` 开头。
2. `LogbookService` 解析出日期/任务/内容（可用正则+LLM）。
3. 更新 Notion（可直接调用 API 或写入待同步队列）。
4. 将结果（成功/失败原因）返回 Telegram，保存历史。

## 3. 配置与部署

### 3.1 `config/settings.toml`
```toml
[paths]
data_dir = "D:/Projects/codex_test/notion_secretary/databases"
database_ids_path = "database_ids.json"

[notion]
api_key = "secret_xxx"
sync_interval = 1800
force_update = false
api_version = "2022-06-28"

[telegram]
token = "8096:ABCDEF"
admin_ids = [6604771431]
poll_timeout = 25

[llm]
provider = "openai"
base_url = "https://api.openai.com/v1"
model = "gpt-4o-mini"
temperature = 0.3
api_key = "sk-..."
```
- 配置文件默认位置 `config/settings.toml`，可通过环境变量 `SECRETARY_CONFIG` 指向其他路径。
- `database_ids.json` 中保存 tasks/logs/projects ID，脚本运行前需填好。
- `data_dir` 下的 `raw_json` / `json` / `telegram_history` 会在首次运行时自动创建。

### 3.2 运行顺序
1. `python scripts/sync_databases.py --force` 获取最新 Notion 数据。
2. `python scripts/run_bot.py`  
   - 内部执行：加载配置 → 初始化 repositories/services/bot client/history store → `while True` 轮询。
   - `get_updates` 使用 `offset=history_store.last_update_id()+1`，每轮处理完立刻更新 offset，防止重复。
3. 若需要守护进程，可用 `pm2`, `supervisor`, `systemd` 或 Windows 任务计划运行 `run_bot.py`。

### 3.3 长轮询注意事项
- Telegram 官方建议 `timeout` <= 50s；设置 25-30s 较稳妥。
- 当 `get_updates` 返回空数组，应短暂 `sleep(1-2)`，避免频繁请求。
- 若 Bot 重启，需读取历史 `last_update_id`，以免重复处理旧消息。

## 4. 测试策略

### 4.1 单元测试
| 模块 | 覆盖点 |
| --- | --- |
| `HistoryStore` | append / 去重 / get_history 排序；使用临时目录或 in-memory DB。 |
| `TelegramBotClient` | 使用 `responses` / `pytest-httpx` 模拟 Bot API，验证错误处理与 `append_bot` 调用。 |
| `TaskSummaryService` | 输入伪造任务数据，验证优先级排序、空数据返回。 |
| `StatusGuard` | 不同状态组合触发正确的 `Intervention`。 |
| `LogbookService` | 解析 `#log` 文本的语法/异常路径。 |
| `AgentLoop` | mock OpenAI API，验证 tool-calling、异常重试、最大回合中止。

### 4.2 集成测试
- 启动本地 mock Telegram server（或 `responses`），模拟 `getUpdates` + `sendMessage` 流程，验证 handler 到 store 的闭环。
- 借助 sample `processed_tasks.json` 等文件，跑一次 `DailyBriefingWorkflow`，确保输出符合预期。

### 4.3 回归测试
- 为 `scripts/sync_databases.py` 提供 dry-run 模式，检测对 Notion API 的调用是否稳定。
- 在 CI 中运行 `pytest tests/apps/telegram_bot`、`pytest tests/core`，对 LLM 模块使用 mock，确保工具协议未被破坏。

---
通过以上约定，团队可以在共享契约下扩展 Bot 能力，确保长轮询模式、历史拼接、任务策略等逻辑在多人协作和多环境部署中保持一致。 
