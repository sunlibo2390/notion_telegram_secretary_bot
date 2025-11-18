# Developer Overview

This document summarizes the current architecture, runtime flows, and operational conventions for the Notion Secretary project. It complements `README.md`, `docs/development_guide.md`, and user manuals by describing how each subsystem interacts, how data flows through the pipeline, and which invariants developers must preserve when adding features.

---

## 1. Top-Level Layout

```
notion_secretary/
├── apps/telegram_bot/        # Telegram entrypoint (bot, handlers, tracker, history)
├── core/                     # Domain models, repositories, services, LLM utilities
├── data_pipeline/            # Notion collectors/processors/transformers/storage utils
├── docs/                     # User manuals, developer guides, personas
├── infra/                    # Settings loader, Notion sync orchestrator
├── scripts/                  # Shortcut entrypoints (run bot, sync databases)
├── tests/                    # Unit/integration tests
└── databases/                # Runtime data dir (raw_json, json, telegram_history, tracker snapshots)
```

Key invariants:
* All external API credentials (Telegram, Notion, LLM, WeCom) are provided through `config/settings.toml` or environment variables. *Never* hardcode secrets.
* The Telegram bot runs as a single long-polling process; heavy operations (Notion sync) must not block command handling threads.
* Data ingestion and transformation is fully deterministic: collectors obtain data, processors normalize JSON into `databases/json`, repositories read those files; no other component should hit Notion directly.
* Local时区可在 `config/settings.toml -> [general].timezone_offset_hours` 配置（默认 8=UTC+8），所有 `format_beijing`/`to_beijing` 调用都会自动使用该偏移。

---

## 2. Data Pipeline

### 2.1 Collectors (`data_pipeline/collectors`)
* `NotionCollector` is the base class for all Notion pulls. It uses `NotionCollectorConfig`, which stores the API key, database IDs, data dir, and freshness thresholds (`duration_threshold_minutes`, `sync_interval_seconds`).
* `collector.update_needed()` checks `databases/last_updated.txt` to decide whether to refetch; this ensures the bot does not flood Notion.
* `collector.collect_once()` fetches each configured database, emits raw JSON to `databases/raw_json`, and then executes processor callables (see below) before writing the new `last_updated` timestamp.

### 2.2 Processors (`data_pipeline/processors`)
* Each processor (projects/tasks/logs) has a dedicated class:
  * `ProjectsProcessor`: filters out completed projects and keeps minimal metadata.
  * `TasksProcessor`: associates tasks with projects, fetches block content via Notion API, resolves subtasks, attaches Markdown text, and sets `page_url` if missing.
  * `LogsProcessor`: fetches block content, resolves related tasks, attaches Markdown text, and stores only active logs.
* Processors operate on local files (`raw_json/...` → `json/processed_...`). They never call Telegram or the LLM directly.
* The `data_pipeline/pipeline.py` module wires processors into `collector_from_settings()` so both CLI scripts and the runtime bot can reuse the same flow.

### 2.3 Storage Conveniences
* `data_pipeline/storage/paths.py` consolidates default directories (raw/processsed/history). `paths.configure()` ensures repository imports and settings initialization share the same directories.
* The `database_collect.py` script is the canonical CLI entrypoint (`python scripts/sync_databases.py --force`).

---

## 3. Telegram Bot Architecture

### 3.1 Entry Point (`apps/telegram_bot/bot.py`)
* `build_runtime()` loads settings, instantiates repositories/services, and creates the Telegram client.
* `TaskTracker` is passed a persistent storage path (`history_dir/tracker_entries.json`) so tracking state survives restarts.
* `NotionSyncService.start_background_sync()` is optional; `/update` now spawns a background thread instead of blocking the main loop.
* `BotRuntime.run_forever()` performs long-polling with exponential resilience. All command handling is synchronous within `CommandRouter`, so heavy operations must spawn threads or asynchronous tasks when necessary.

### 3.2 Command Router (`apps/telegram_bot/handlers/commands.py`)
* Commands and natural language flow eventually call `_maybe_auto_update_state()` and `LLMAgent` when no explicit handler matches.
* Key commands and special behaviors:
  * `/trackings`: enumerates active tracking entries with their next reminder time. Uses `_format_task_link_text()` to only hyperlink tasks with Notion URLs.
  * `/track <task> [minutes]`: sets up or updates tracking; `interval_minutes` can be any duration ≥5 minutes. When inside a rest window, the tracker may shift the first reminder to the rest end, but otherwise the original interval is preserved.
  * `/tasks` variants: `light` (simple name + project), `group light`, and `/tasks delete <indices...>` for batch removal of custom tasks. `ensure_task()` always creates custom tasks with `page_url=None`.
  * `/logs [N]` and `/logs tasks [N]`: outputs raw text to avoid Telegram Markdown parsing issues. Users can delete multiple logs via `/logs delete 1 2 5`.
  * `/update`: replies immediately (“后台同步…”), executes `NotionSyncService.sync()` inside a daemon thread, streams progress updates, and posts the result when finished.
* Misc modules used by the router:
  * `HistoryStore` (apps/telegram_bot/history/`): stores message history per chat and the last Telegram `update_id`.
  * `ProactivityService`: manages user state, forced prompts, and rest windows.
  * `RestScheduleService`: tracks rest/task blocks as JSON; the router routes `/blocks` operations to it.

### 3.3 Task Tracking (`apps/telegram_bot/tracker.py`)
* `TaskTracker` maintains per-chat dictionaries of `TrackerEntry` objects, each containing `task_id`, `timer`, metadata, `next_fire_at`, and `rest_resume_at`.
* State persistence:
  * `tracker_entries.json` is read on startup; each entry’s timer is recreated with the remaining delay.
  * Mutations (start, stop, consume, clear, rest defer) call `_persist()` to keep the JSON file consistent.
* Rest window logic:
  * When starting tracking inside a rest window, the first reminder is delayed only if the default reminder time would fall before the rest end.
  * `defer_for_rest()` checks each entry: if its `next_fire_at` already lies outside the rest window, nothing changes; otherwise it cancels the timer and re-schedules to the rest end.
* `list_next_events()` now always returns actual `entry.next_fire_at`, so `/trackings`, `/next`, and `/board` display consistent reminder times even during rest.

---

## 4. LLM Agent & Tooling

### 4.1 Prompt Construction
* `core/llm/context_builder.py` injects the persona (`docs/user_profile_doc.md`) plus runtime instructions. Important guardrails:
  * All messages use Markdown and must avoid table syntax.
  * When user describes reminder intervals in natural language (hours/seconds), the agent must convert to minutes (e.g., 8h→480) and execute, since tracking accepts any ≥5-minute interval.
  * Set deadlines and statuses precisely with UTC+8 conversions.

### 4.2 Tool Registry (`core/llm/tools.py`)
* `register_tracker_executor` accepts `interval_minutes` as integers; the agent is responsible for pre-parsing natural language durations before invoking it.
* Additional tools (`logs`, `tasks`, `rest_*`, etc.) expose deterministic operations to the agent flow. Each tool returns JSON so the LLM can decide the next action.

### 4.3 Agent Loop (`core/llm/agent.py`)
* Executes “ReAct” style loops: builds context → calls LLM → executes tools (if requested) → resumes until final response.
* `AgentRunLogger` persists the conversation for debugging and analytics.

---

## 5. Notion Sync Integration

* `NotionSyncService` wraps the CLI collector. It holds a mutex so only one sync runs at a time regardless of entrypoint.
* `sync(actor=..., force=False, progress_callback=None)` returns `NotionSyncResult` with status, message, and duration.
* `/update` and the `/notion_sync` tool call this service; progress callbacks stream status updates to Telegram.

---

## 6. Testing Strategy

* Unit tests live under `tests/apps/telegram_bot` and `tests/core`. Run `pytest` before committing.
* Regression coverage:
  * `tests/apps/telegram_bot/test_tracker.py` ensures multi-task tracking, waiting-state rehydration, custom intervals, and persistence.
  * `tests/apps/telegram_bot/test_command_router_tasks.py` validates `/tasks` variants, `/trackings`, `/logs`, and new batch delete behaviors.
  * `tests/core/test_llm_agent.py` covers the multi-stage tool loop.
* When adding new features, extend existing tests or create new ones to capture edge cases (rest windows, multi-chat concurrency, etc.).

---

## 7. Operational Notes

* **Data freshness**: developers should run `python scripts/sync_databases.py --force` when updating processors or schema, so local JSON reflects new logic.
* **Long-running operations**: anything that might block for more than a few seconds must use background threads or asynchronous callbacks; the main command router should stay responsive.
* **Custom tasks/logs**: all locally created items live under `databases/json/agent_tasks.json` and `agent_logs.json`. They should never be overwritten by Notion sync.
* **Error handling**: Telegram API errors surface as exceptions in the log; commands should catch predictable user errors and reply with actionable messages.

---

## 8. Extensibility Checklist

When implementing new functionality:
1. Decide whether the feature touches data ingestion (Notion) or runtime interaction (Telegram). Keep these responsibilities separated.
2. If adding a command:
   * Update `CommandRouter` with minimal branching.
   * Escape user-facing text with `escape_md` or send plain text (`markdown=False`) to prevent Telegram parse errors.
   * If the command has long-running steps, spawn threads or scheduled jobs.
3. If updating tracking logic:
   * Maintain `TrackerEntry` invariants (`next_fire_at`, `rest_resume_at`, persistence).
   * Update tests to reflect new behaviors.
4. Update docs (`README.md`, user manual, developer overview) to reflect user-visible command changes.

With these guidelines, contributors can develop features confidently without breaking existing flows or user expectations. If you introduce significant architectural changes, extend this document accordingly so future developers understand the rationale and the new invariants. 
