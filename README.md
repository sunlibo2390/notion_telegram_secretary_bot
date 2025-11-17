# Notion Secretary

AI secretary that同步 Notion 数据库、深度理解 `docs/user_profile_doc.md` 中的画像，并在 Telegram 中扮演一名“强硬秘书”。与传统 if/else 规则不同，本项目以 **OpenAI 大模型** 为核心：所有提醒、决策与对话策略均由 LLM Agent 生成，Bot 只是负责采集上下文、调用“技能”（task summary、日志补录等）并执行 LLM 的指令。

## 项目目标
- 将 Notion 中的任务、项目与日志数据整理成结构化视图，并与用户画像交叉引用。
- 通过 Telegram 建立实时对话渠道，支持状态同步、任务追踪和强制提醒。
- 将数据采集、智能决策、消息交互拆分成清晰的模块，便于多成员协作与持续迭代。

## 组织结构规划
```
notion_secretary/
├── apps/
│   └── telegram_bot/          # 基于 python-telegram-bot / aiogram 的入口
│       ├── bot.py             # Bot 启动脚本，注册 handlers
│       ├── handlers/          # 命令、回调、消息流逻辑
│       ├── keyboards/         # 内联/回复键盘定义
│       ├── middlewares/       # 会话态、限流、权限等
│       └── dto.py             # Telegram 消息 <-> 领域对象转换
├── core/
│   ├── domain/                # 领域模型：Task, Project, LogEntry, Intervention, UserProfile
│   ├── services/              # 任务聚合、日志写入、干预执行等“技能”实现
│   ├── repositories/          # 从 processed JSON/DB 读取任务、日志等
│   ├── workflows/             # LLM 触发的高层流程（每日检查、倒计时、主动干预）
│   └── llm/                   # Prompt 模板、技能注册、OpenAI Client/agent loop
├── data_pipeline/
│   ├── collectors/            # Notion 数据拉取（database_collect 重构版）
│   ├── processors/            # Projects/Tasks/Logs 处理器
│   ├── transformers/          # Markdown 转换、字段清洗
│   ├── notion_api.py          # 统一的 Notion API 客户端
│   └── storage/               # raw_json / processed / cache
├── prompts/                   # 对话、summary、干预话术模板
├── response_examples/         # Telegram 交互示例
├── config/
│   └── settings.example.toml    # 配置模板，复制为 settings.toml 使用
├── docs/
│   ├── user_profile_doc.md      # 现有人物画像
│   ├── telegram_architecture.md # Telegram bot 设计与场景细节
│   └── development_guide.md     # 接口契约、场景流程、配置与测试指引
├── scripts/
│   ├── run_bot.py             # 启动 Telegram bot
│   └── sync_databases.py      # 手动触发采集与处理
├── tests/                     # 单元/集成测试
└── infra/
    └── scheduler/             # 预留后台 cron / APScheduler 任务
```

> `database_collect.py` 会在采集后依次执行 `data_pipeline.processors` 中的 Projects / Tasks / Logs 处理器，并已将历史 `block2md.py`、`block_children_query.py` 等脚本内嵌为模块化依赖。Bot 启动后同样会按照 `notion.sync_interval` 在后台定时触发 `NotionSyncService`（内部也会检查 `last_updated.txt`，避免频繁拉取），保持 processed 数据的新鲜度。

## 数据流（Agent 视角）
1. **Collector**：轮询 Notion API，写入 `data_pipeline/storage/raw_json/`.
2. **Processor**：`data_pipeline.processors` 使用统一的 `NotionAPI` + Markdown transformer 标准化内容，落地 `processed/*.json`.
3. **Repository / Memory**：`core/repositories/*` + `docs/user_profile_doc.md` 组成 Agent 的 “事实记忆”。
4. **Agent Loop**
   - `context_builder` 汇总 Telegram 历史、最新任务、画像特征
   - `core/llm/agent.py` 组装 prompt 并调用 OpenAI Chat Completions（含 function-calling）
   - LLM 输出 `Action`：例如 `summarize_tasks`, `record_log`, `enforce_focus`
   - 对应 `core/services/*` 执行该 Action，把结果回写给 LLM 作为 `Observation`
   - LLM 最终产出 `FinalMessage` 或继续迭代
5. **Telegram Bot**：将 `FinalMessage` 发给用户，若 LLM 要求“再跟进”则继续循环。

## Telegram & Agent 交互
- **统一入口**：用户输入（指令或自然语言）均进入 Agent Loop，LLM 决定是否调用某个技能；这样 `/tasks` 可以衍生出“继续质问”“安排 follow-up”等智能行为。
- **主动推送**：`infra/scheduler` 触发 workflow → 调用 LLM 生成 briefing 文本或干预话术 → Bot 推送。
- **多轮上下文**：`apps/telegram_bot/history/HistoryStore` 持久化用户与 Bot 消息，LLM 在每轮 prompt 中可访问最近 N 条历史 + 长期画像。
- **可靠性**：仍采用 `getUpdates` 长轮询。Telegram 只返回用户留言，因此 `TelegramBotClient.send_message` 必须保存响应体用于复原历史。更多细节见 `docs/telegram_architecture.md`.
- **指令小抄**：
  - `/track <任务ID> [分钟]`：开启任务跟踪；未指定时默认 25 分钟提醒，可按需定制首个提醒间隔。
  - `/trackings`：查看当前会话内正在跟踪的任务。
  - `/untrack`：在用户确认后，取消当前的跟踪提醒（LLM 对话模式也可通过调用 stop_tracker 工具执行同一操作）。
  - `/logs [N]`：查看最近 N 条日志（默认 5，最多 20），便于快速回顾记录。
  - `/logs delete <序号>`：在查看日志后，可按显示的序号删除对应记录。
  - `/state` / `/next`：查看当前记录的行动/心理状态以及下一次主动提醒的预计时间（含跟踪任务、状态检查）。
  - `/logs update <序号> <内容>`：更新指定日志的内容（可附 `任务 XXX：...` 自动重绑任务）。
  - `/update`：立即从 Notion 拉取项目/任务/日志，刷新本地缓存。
- **时间块管理**：`/blocks` 统一展示「休息」与「任务专注」时间段；对 Bot 说 “14:00-16:00 专注 Magnet 代码” 会创建任务窗口，并自动开启该任务的追踪，到点提醒你收尾，避免任务无限拖延。
- **状态管控**：行动状态只会在“处于任务时间块 + 正在跟踪”时成为“推进中”，休息结束或任务块结束后自动回到 `unknown`。当行动/心理状态为 `unknown` 或已过期时，每隔 `state_unknown_retry_seconds`（默认 2 分钟）会持续追问。
- **日志智能**：Agent 触发 `record_log` 工具时会结合当前对话与近期历史自动匹配任务；若需要修订，可使用 `/logs update` 或 `update_log` 工具重新绑定。
- **本地记忆**：手动创建的任务写入 `json/agent_tasks.json`，日志写入 `json/agent_logs.json`，即使执行 `/update` 或重新跑 `scripts/sync_databases.py` 也不会被覆盖。
- **多渠道同步**：在 `config/settings.toml` 中配置 `[wecom].webhook_url` 后，Agent 的每条回复都会镜像到对应的企业微信机器人，方便在其他终端实时关注。
- **主动干预**（`apps/telegram_bot/proactivity.py`）：
  - 若 30 分钟未检测到有效进展描述，将主动 ping 用户并要求说明当前任务与预计完成时间。
  - 发送带问句的消息后 3 分钟仍未收到回复，会自动再次提醒，直到用户反馈。
  - 若用户连续多条回复均无进展信息，Agent 会直接输出 `【经评估，讨论已无意义】` 终止闲聊，迫使其回到主线。

## 配置与环境变量
将所有敏感信息写入 `config/settings.toml`（不要硬编码在源码内）：

| 配置键 | 用途 |
| --- | --- |
| `notion.api_key` | 访问 Notion API |
| `notion.sync_interval` | 周期性同步间隔（秒） |
| `notion.api_version` | Notion API 版本号，默认 `2022-06-28` |
| `paths.data_dir` | Raw/processed/telegram_history 数据根目录 |
| `paths.database_ids_path` | `database_ids.json` 所在路径 |
| `telegram.token` | Telegram Bot Token |
| `telegram.admin_ids` | 可使用管理命令的用户 ID |
| `telegram.poll_timeout` | `getUpdates` 长轮询的超时时间 |
| `llm.provider` / `llm.base_url` | LLM 服务端点（默认 OpenAI 兼容接口） |
| `llm.model` | Agent 所使用的模型（如 `gpt-4o-mini`、`gpt-4.1` 等） |
| `llm.temperature` | 控制秘书语气、创造力的参数 |

## 快速开始
1. 创建虚拟环境并安装依赖（示例）：
   ```bash
   uv venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```
2. 复制配置模板：
   ```bash
   cp config/settings.example.toml config/settings.toml
   ```
3. 在 `config/settings.toml` 中填写 Notion、Telegram、LLM 等凭据。
4. 更新 `database_ids.json`，然后运行一次同步：
   ```bash
   python scripts/sync_databases.py --force
   ```
5. 启动 Telegram bot：
   ```bash
   python scripts/run_bot.py
   ```

## 开发约定
- **数据层**：任何直接访问 Notion 的操作放在 `data_pipeline/collectors`，避免散落在 bot handlers 中。
- **领域层**：`core/domain` 中的对象应保持纯粹（不依赖 Telegram/Notion SDK），便于测试。
- **服务层**：先处理 deterministic 逻辑，再调用 LLM。所有 LLM prompt 模板集中在 `prompts/`.
- **日志与监控**：Bot 与数据同步脚本统一使用 logging 配置输出，写入 `logs/`.
- **测试**：重要流程（任务筛选、提醒策略）需要 `tests/` 中的单元测试，Telegram 交互可用 fixture 模拟；具体覆盖点与接口契约见 `docs/development_guide.md`.

## 后续迭代方向
- 将 processed JSON 替换为 SQLite/PostgreSQL，支持多用户。
- 引入 `apscheduler` 实现分钟级定时任务。
- Telegram 端接入多模态（语音/图片）以提高记录效率。
- 构建 web 控制台查看实时状态、手动干预历史。

---

> 更多细节：
> - Telegram 长轮询及历史拼接：`docs/telegram_architecture.md`
> - 接口契约、场景流程、配置与测试：`docs/development_guide.md`
> - 用户画像：`docs/user_profile_doc.md`

## 时间与主动策略说明

- **统一时区**：Agent 面向用户展示的所有时间（日志、提醒、`/blocks`、`/next` 等）均转换为北京时间（UTC+8），与代码中的 `core/utils/timezone.py` 保持一致，避免“本地时间/服务器时间”混淆。
- **休息与勿扰**：通过 `/blocks` 或 LLM 的 `rest_*` 工具创建的窗口会暂停主动提醒、追问和跟踪计时，恢复后自动顺延。
- **状态追问**：`state_unknown_retry_seconds` 控制 `unknown` 或过期状态下的追问频率（默认 120 秒），即使 `state_check_seconds` 较大，也会在该间隔内反复催促，直到用户给出有效状态。

### `[proactivity]` 配置项

`config/settings.toml` 中的 `[proactivity]` 决定 Agent 主动发言与追问的节奏：

| 参数 | 默认值 | 作用 |
| --- | --- | --- |
| `state_check_seconds` | 300 | 巡检间隔；到点会重新检查行动 / 心理状态是否需要更新 |
| `state_stale_seconds` | 3600 | 状态超过该时长未更新将被标记为“失效”，下一次巡检会发起询问 |
| `state_prompt_cooldown_seconds` | 600 | 同一状态被追问后，至少等待该冷却时间才会再次催促 |
| `question_follow_up_seconds` | 600 | Agent 提问后若未收到有效回复，将按此间隔重复追问（休息期自动顺延） |

> 这些参数在 `apps/telegram_bot/proactivity.py` 中被 `ProactivityService` 使用：  
> - `state_check_seconds` 设定轮询定时器；  
> - `state_stale_seconds` 与 `state_prompt_cooldown_seconds` 共同判断何时需要再次询问行动/心理状态；  
> - `question_follow_up_seconds` 控制“提问后未回复”的追问定时。  
> 所有判断都会先检查是否处于休息窗口，以确保勿扰策略生效。
