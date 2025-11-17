# Telegram Bot Architecture

## 背景与通信约束
- Bot 已创建，当前通过手机端 / 网页端与用户交互。
- 由于暂时没有公网可达的 webhook 入口，采用 **long polling (`getUpdates`)** 拉取消息；后续可平滑切换到 webhook。
- `getUpdates` 返回的聊天记录只包含 **用户发送的消息**，因此需要在每一次 `sendMessage`/`sendPhoto` 等调用后，将 Telegram 返回的 `message` 对象持久化，才能完整复原对话历史。

## 运行拓扑
```
┌──────────────┐    ┌───────────────────┐    ┌─────────────────────┐    ┌─────────────────┐
│ data_pipeline│ -> │ repositories/memory│ -> │ LLM Agent (core/llm)│ -> │ apps/telegram_bot│
└──────────────┘    └───────────────────┘    └─────────────────────┘    └─────────────────┘
                                                          │                       │
                                                          ▼                       ▼
                                                skills/core/services      Telegram Bot API
                                                          │                       │
                                                          ▼                       ▼
                                               Notion updates / actions   Telegram Client (user)
```

## 模块划分

| 路径 | 说明 |
| --- | --- |
| `apps/telegram_bot/bot.py` | 入口脚本，创建 `TelegramBotClient`，启动 long polling 循环。 |
| `apps/telegram_bot/clients/telegram_client.py` | 对 Bot API 进行封装，处理重试、解析以及响应缓存。 |
| `apps/telegram_bot/history/history_store.py` | 负责持久化 `update`（用户消息）和 Bot 回执，支持基于 `chat_id` 获取完整上下文。推荐 JSONL / SQLite。 |
| `apps/telegram_bot/handlers/` | 统一入口，仅负责把用户消息转交给 LLM Agent，并处理 function-call 的执行结果。 |
| `apps/telegram_bot/dialogs/context_builder.py` | 构建 Agent prompt：包含 Telegram 历史、Notion 状态、画像。 |
| `core/llm/agent.py` | 与 OpenAI Chat Completions 交互；维护 ReAct/Tool-Calling loop。 |
| `core/services/*` | 由 LLM 调用的“技能”，涵盖任务聚合、日志写入、干预推送等。 |
| `core/workflows/daily_briefing.py` | 可作为 LLM 指定的 Action，被动/主动触发。 |
| `infra/scheduler/` | 可使用 APScheduler/cron 启动周期性推送任务，触发 bot 的主动消息。 |
| `prompts/` | Secretary persona、工具描述、系统指令。 |

> 历史上的 Jupyter 示例已合并进正式代码，可在 `apps/telegram_bot/clients/telegram_client.py` 中找到 `sendMessage` / `getUpdates` 的最小实现。

## 对话历史管理
1. **拉取用户消息**：`getUpdates(offset=last_update_id+1)`，遍历 `result`，将用户消息写入 `history_store`，并将 `update_id` 标记为已消费。
2. **发送 Bot 消息**：统一通过 `TelegramBotClient.send_message` 等接口；在成功响应后拿到 `resp["result"]`，序列化保存：
   ```python
   class TelegramBotClient:
       def send_message(self, chat_id, text, **kwargs):
           resp = requests.post(self.base_url + "/sendMessage", data={...})
           resp.raise_for_status()
           message = resp.json()["result"]
           self.history_store.append_bot(message)
           return message
   ```
   保存字段建议：`timestamp`, `chat_id`, `message_id`, `direction` (`bot/user`), `text`, `entities`, `reply_to_message_id`.
3. **构造历史**：`history_store.get_history(chat_id, limit)` 同时读取两种方向的消息，按 `timestamp` 排序即可获得完整上下文，供 prompt、可视化、debug 使用。
4. **持久化介质**：初期可用 `data_pipeline/storage/telegram_history/{chat_id}.jsonl`；若后续多用户，建议引入 SQLite/PostgreSQL。

## LLM Secretary Agent
1. **Observation 组装**：`context_builder` -> `[system persona] + [user_profile] + [recent history] + [latest tasks/logs/projects] + [当前用户输入]`。
2. **LLM 推理**：调用 OpenAI Chat Completions（推荐 `gpt-4o-mini` / `gpt-4.1`），启用工具描述：
   - `summarize_tasks`
   - `enforce_focus`
   - `record_log`
   - `generate_briefing`
   - `query_memory`
3. **技能调用**：LLM 若输出 `tool_call`，由 `core/services` 执行并返回 `observation`；Agent 再次思考，直到产出 `assistant` 最终语句。
4. **记忆更新**：每次 `assistant` 发言都会写入 `HistoryStore`；若执行了写日志/修改任务等操作，也将结果同步到数据层。

## 核心场景与逻辑
- **Daily Briefing / Evening Review**  
  Scheduler 触发 `daily_briefing` Action → LLM 根据当日任务生成多段总结 + 质问语气 → Bot 推送并等待用户回复，必要时继续追问。

- **实时状态监控与强制提醒**  
  `StatusGuard`只是提供“检测结果”工具；LLM Agent 根据检测结果决定如何讽刺/威胁、是否追加 follow-up。

- **用户指令 / 自然语言**  
  无论是 `/tasks` 还是“我今天状态很差”，都会被视为 Observation，由 LLM 决定是返回结果、继续提问、还是调用 `record_log` 等工具。

- **Log 记录 / 补录**  
  LLM 先自行理解用户描述，再调用 `LogbookService` 完成落地，并把日志 ID/任务关联反馈给用户。

- **多轮辅导**  
  Agent 可以要求更多细节、设置倒计时、甚至引用 `docs/user_profile_doc.md` 中的性格标签制定强制策略。

## 长轮询与可靠性
- 采用 `while True` + `sleep` 的长轮询方式；每次拉取后立即设置 `offset`，避免重复。
- 可记录最近一次成功轮询时间，写入 `logs/telegram_bot.log` 便于排障。
- 若需要多实例部署，可将 `offset` 存在 Redis / DB，保证只有一个 worker 消费。

## 未来升级
- 切换到 webhook：在可访问公网的 server（或 cloud function）上暴露 HTTPS 入口，使用同样的 handler 层即可。
- 消息队列：将 Notion 事件、LLM 请求、Telegram 发送解耦，提升吞吐。
- 历史持久化：接入向量数据库，支持根据对话内容检索历史干预片段，优化上下文质量。

--- 
通过以上模块和历史管理策略，可以保证即使 Telegram API 默认不返回 bot 的消息，也能稳定地拼接完整对话，从而为 Secretary 的场景化推理提供可靠上下文。 
