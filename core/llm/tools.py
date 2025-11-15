from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from apps.telegram_bot.history import HistoryStore
from apps.telegram_bot.rest import RestScheduleService, RestWindow
from core.utils.timezone import format_beijing, to_beijing
from apps.telegram_bot.tracker import TaskTracker
from apps.telegram_bot.user_state import UserStateService
from core.repositories import LogRepository, TaskRepository
from core.services import LogbookService, StatusGuard, TaskSummaryService

Executor = Callable[[Dict[str, Any], int], Dict[str, Any]]


@dataclass
class AgentTool:
    name: str
    description: str
    parameters: Dict[str, Any]
    executor: Executor

    def to_openai_schema(self) -> Dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    def execute(self, arguments: str | Dict[str, Any], chat_id: int) -> Dict[str, Any]:
        if isinstance(arguments, str):
            try:
                args = json.loads(arguments) if arguments else {}
            except json.JSONDecodeError:
                args = {"__raw": arguments}
        else:
            args = arguments
        return self.executor(args, chat_id)


def build_default_tools(
    task_service: TaskSummaryService,
    logbook_service: LogbookService,
    status_guard: StatusGuard,
    tracker: TaskTracker | None = None,
    task_repository: TaskRepository | None = None,
    log_repository: LogRepository | None = None,
    history_store: HistoryStore | None = None,
    user_state_service: UserStateService | None = None,
    rest_service: RestScheduleService | None = None,
    session_monitor: Any | None = None,
    notion_sync_service: Any | None = None,
) -> List[AgentTool]:
    def _split_queries(raw: str) -> List[str]:
        normalized = raw.replace("任务", " ")
        for sep in ["和", "、", ",", "，", ";", "；", "\n"]:
            normalized = normalized.replace(sep, " ")
        return [part.strip().lower() for part in normalized.split() if part.strip()]

    def _search_payloads(query: str) -> List[Dict[str, Any]]:
        tokens = _split_queries(query)
        if not tokens:
            return []
        candidates = task_service.build_task_payloads()
        scored: List[tuple[int, Dict[str, Any]]] = []
        for payload in candidates:
            haystack = " ".join(
                [
                    payload.get("name", ""),
                    payload.get("project", ""),
                    payload.get("content", ""),
                    " ".join(payload.get("subtasks", [])),
                    " ".join(log.get("content", "") for log in payload.get("logs", [])),
                ]
            ).lower()
            score = 0
            for token in tokens:
                if token in haystack:
                    score += 1
                if token in payload.get("name", "").lower():
                    score += 3
                if token in payload.get("project", "").lower():
                    score += 1
            if score > 0:
                scored.append((score, payload))
        scored.sort(key=lambda item: item[0], reverse=True)
        return [payload for _, payload in scored[:5]]

    task_extract_pattern = re.compile(r"任务\s*([^：:]+)\s*[：:]\s*(.+)")

    def _format_note(text: str) -> str:
        stripped = text.strip()
        if not stripped:
            return "空白日志"
        parts = [seg.strip() for seg in re.split(r"[；;]+", stripped) if seg.strip()]
        if len(parts) <= 1:
            return stripped
        return "\n".join(f"{idx+1}. {part}" for idx, part in enumerate(parts, start=1))

    def _extract_log_fields(raw: str) -> tuple[Optional[str], str]:
        text = raw.strip()
        if not text:
            return None, "空白日志"
        match = task_extract_pattern.search(text)
        if not match:
            return None, _format_note(text)
        return match.group(1).strip(), _format_note(match.group(2).strip())

    def _parse_datetime_arg(raw: str | None) -> Optional[datetime]:
        if not raw:
            return None
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None

    def _serialize_rest_window(window: RestWindow) -> Dict[str, Any]:
        start = to_beijing(window.start)
        end = to_beijing(window.end)
        created_at = to_beijing(window.created_at)
        duration_minutes = int((end - start).total_seconds() / 60)
        return {
            "id": window.id,
            "chat_id": window.chat_id,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "status": window.status,
            "note": window.note,
            "duration_minutes": duration_minutes,
            "created_at": created_at.isoformat(),
            "display_start": format_beijing(start),
            "display_end": format_beijing(end),
            "display_created_at": format_beijing(created_at),
        }

    def _infer_task_from_history(chat_id: int) -> Optional[str]:
        if not history_store or not task_repository:
            return None
        history = history_store.get_history(chat_id, limit=10)
        tasks = task_repository.list_active_tasks()
        for entry in reversed(history):
            if getattr(entry, "direction", "") != "user":
                continue
            candidate, _ = _extract_log_fields(entry.text)
            if candidate:
                return candidate
            lowered = entry.text.lower()
            for task in tasks:
                if task.name.lower() in lowered:
                    return task.name
        return None

    def summarize_executor(_: Dict[str, Any], __: int) -> Dict[str, Any]:
        return {
            "summary": task_service.build_today_summary(),
            "tasks": task_service.build_task_payloads(),
        }

    def refresh_notion_executor(args: Dict[str, Any], __: int) -> Dict[str, Any]:
        if not notion_sync_service:
            return {"status": "error", "message": "notion sync unavailable"}
        reason = (args.get("reason") or "").strip()
        actor = f"agent:{reason}" if reason else "agent"
        result = notion_sync_service.sync(actor=actor)
        status = "ok" if result.success else "error"
        return {"status": status, "message": result.message}

    def log_executor(args: Dict[str, Any], chat_id: int) -> Dict[str, Any]:
        raw_text = (args.get("text") or "").strip()
        explicit_note = (args.get("note") or "").strip()
        explicit_task_name = (args.get("task_name") or "").strip() or None
        task_id = args.get("task_id")
        inferred_task_name = None
        inferred_note = None
        if raw_text:
            inferred_task_name, inferred_note = _extract_log_fields(raw_text)
        task_name = explicit_task_name or inferred_task_name or _infer_task_from_history(chat_id)
        note = explicit_note or inferred_note or raw_text or "#log 自动记录"
        result = logbook_service.record_structured_log(
            content=note,
            task_name=task_name,
            task_id=task_id,
        )
        return {"message": result.message, "task_name": result.task_name}

    def focus_executor(_: Dict[str, Any], __: int) -> Dict[str, Any]:
        interventions = [asdict(i) for i in status_guard.evaluate()]
        return {"interventions": interventions}

    def tracker_executor(args: Dict[str, Any], chat_id: int) -> Dict[str, Any]:
        if not tracker or not task_repository:
            return {"status": "error", "message": "tracking not supported"}
        task_id = args.get("task_id")
        task_name = args.get("task_name") or args.get("query")
        if not task_id and task_name:
            matches = _search_payloads(task_name)
            if matches:
                task_id = matches[0].get("id")
        if not task_id and task_name:
            task = task_repository.ensure_task(task_name)
            task_id = task.id
        task = task_repository.get_task(task_id) if task_id else None
        if not task:
            return {"status": "error", "message": "task not found"}
        interval_minutes = None
        raw_interval = args.get("interval_minutes")
        if raw_interval is not None:
            try:
                interval_minutes = int(raw_interval)
            except (TypeError, ValueError):
                interval_minutes = None
        tracker.start_tracking(chat_id, task, interval_minutes=interval_minutes)
        return {"status": "ok", "task": task_id}

    def stop_tracker_executor(args: Dict[str, Any], chat_id: int) -> Dict[str, Any]:
        if not tracker:
            return {"status": "error", "message": "tracking not supported"}
        task_name = args.get("task_name")
        entry = tracker.stop_tracking(chat_id, ensure_name=task_name)
        if not entry:
            return {"status": "error", "message": "no matching tracking"}
        return {"status": "ok", "task": entry.task_id, "task_name": entry.task_name}

    def search_executor(args: Dict[str, Any], __: int) -> Dict[str, Any]:
        query = (args.get("query") or "").strip()
        if not query:
            return {"results": []}
        return {"results": _search_payloads(query)}

    def logs_executor(args: Dict[str, Any], __: int) -> Dict[str, Any]:
        if not log_repository:
            return {"logs": []}
        try:
            limit = int(args.get("limit", 5))
        except (TypeError, ValueError):
            limit = 5
        limit = max(1, min(20, limit))
        entries = log_repository.list_logs()
        payload = []
        for entry in entries[-limit:]:
            payload.append(
                {
                    "id": entry.id,
                    "name": entry.name,
                    "status": entry.status,
                    "content": entry.content,
                    "task_id": entry.task_id,
                    "task_name": entry.task_name,
                    "task_url": entry.task_id and f"https://www.notion.so/{entry.task_id.replace('-', '')}",
                }
            )
        return {"logs": payload}

    def update_log_executor(args: Dict[str, Any], __: int) -> Dict[str, Any]:
        log_id = args.get("log_id")
        if not log_id:
            return {"status": "error", "message": "log_id required"}
        note = (args.get("note") or "").strip()
        task_name = (args.get("task_name") or "").strip() or None
        task_id = args.get("task_id")
        result = logbook_service.update_log(
            log_id=log_id,
            content=note or None,
            task_name=task_name,
            task_id=task_id,
        )
        status = "ok" if result.stored else "error"
        return {"status": status, "message": result.message}

    def create_task_executor(args: Dict[str, Any], __: int) -> Dict[str, Any]:
        if not task_repository:
            return {"status": "error", "message": "task repository unavailable"}
        name = (args.get("name") or "").strip()
        if not name:
            return {"status": "error", "message": "name required"}
        task = task_repository.create_custom_task(
            name=name,
            content=(args.get("content") or "").strip(),
            priority=(args.get("priority") or "Medium").strip() or "Medium",
            status=(args.get("status") or "Undecomposed").strip() or "Undecomposed",
            project_name=(args.get("project_name") or "").strip(),
            due_date=(args.get("due_date") or "").strip() or None,
        )
        return {"status": "ok", "task_id": task.id, "task_name": task.name}

    def update_task_executor(args: Dict[str, Any], __: int) -> Dict[str, Any]:
        if not task_repository:
            return {"status": "error", "message": "task repository unavailable"}
        task_id = (args.get("task_id") or "").strip()
        if not task_id:
            return {"status": "error", "message": "task_id required"}
        task = task_repository.update_custom_task(
            task_id=task_id,
            name=(args.get("name") or "").strip() or None,
            content=args.get("content"),
            status=(args.get("status") or "").strip() or None,
            priority=(args.get("priority") or "").strip() or None,
            due_date=(args.get("due_date") or "").strip() or None,
            project_name=(args.get("project_name") or "").strip() or None,
        )
        if not task:
            return {"status": "error", "message": "task not found or not editable"}
        return {"status": "ok", "task_id": task.id, "task_name": task.name}

    def delete_task_executor(args: Dict[str, Any], __: int) -> Dict[str, Any]:
        if not task_repository:
            return {"status": "error", "message": "task repository unavailable"}
        task_id = (args.get("task_id") or "").strip()
        if not task_id:
            return {"status": "error", "message": "task_id required"}
        success = task_repository.delete_custom_task(task_id)
        if not success:
            return {"status": "error", "message": "task not found or not deletable"}
        return {"status": "ok", "task_id": task_id}

    def report_state_executor(args: Dict[str, Any], chat_id: int) -> Dict[str, Any]:
        if not user_state_service:
            return {"status": "error", "message": "state service unavailable"}
        action = (args.get("action") or "").strip()
        mental = (args.get("mental") or "").strip()
        if not action and not mental:
            return {"status": "error", "message": "action or mental required"}
        has_tracker = bool(tracker and tracker.list_active(chat_id)) if tracker else False
        is_resting = bool(rest_service and rest_service.is_resting(chat_id)) if rest_service else False
        if action == "推进中" and not has_tracker:
            return {"status": "error", "message": "需要先开启任务跟踪才能标记为推进中"}
        if is_resting and action and action != "休息中":
            action = "休息中"
        state = user_state_service.update_state(
            chat_id,
            action=action or None,
            mental=mental or None,
            has_active_tracker=has_tracker,
            is_resting=is_resting,
        )
        return {
            "status": "ok",
            "action": state.action,
            "mental": state.mental,
        }

    def rest_list_executor(args: Dict[str, Any], chat_id: int) -> Dict[str, Any]:
        if not rest_service:
            return {"windows": []}
        include_past = bool(args.get("include_past"))
        windows = rest_service.list_windows(chat_id, include_past=include_past)
        return {
            "windows": [
                {
                    **_serialize_rest_window(window),
                    "session_type": window.session_type,
                    "task_id": window.task_id,
                    "task_name": window.task_name,
                }
                for window in windows
            ]
        }

    def rest_propose_executor(args: Dict[str, Any], chat_id: int) -> Dict[str, Any]:
        if not rest_service:
            return {"status": "error", "message": "rest service unavailable"}
        start = _parse_datetime_arg(args.get("start"))
        end = _parse_datetime_arg(args.get("end"))
        if not start or not end:
            return {"status": "error", "message": "start/end must be ISO8601 datetime"}
        note = (args.get("note") or "").strip()
        mode = (args.get("session_type") or args.get("mode") or "rest").strip().lower()
        if mode not in {"rest", "task"}:
            return {"status": "error", "message": "session_type must be rest or task"}
        task_name_arg = (args.get("task_name") or "").strip()
        task_id = (args.get("task_id") or "").strip() or None
        if mode == "task" and not (task_id or task_name_arg):
            return {"status": "error", "message": "task sessions require task_name or task_id"}
        try:
            window = rest_service.add_window(
                chat_id,
                start,
                end,
                note=note,
                status="approved",
                session_type=mode,
                task_id=task_id,
                task_name=task_name_arg or None,
            )
        except ValueError as exc:
            return {"status": "error", "message": str(exc)}
        if tracker and mode == "rest":
            tracker.defer_for_rest(chat_id, window.start, window.end)
        if session_monitor and window.session_type == "task":
            session_monitor.schedule(window)
        if user_state_service and mode == "rest":
            has_tracker = bool(tracker and tracker.list_active(chat_id)) if tracker else False
            user_state_service.update_state(
                chat_id,
                action="休息中",
                has_active_tracker=has_tracker,
                is_resting=True,
            )
        return {
            "status": "approved",
            "window": {
                **_serialize_rest_window(window),
                "session_type": window.session_type,
                "task_id": window.task_id,
                "task_name": window.task_name,
            },
        }

    def rest_cancel_executor(args: Dict[str, Any], chat_id: int) -> Dict[str, Any]:
        if not rest_service:
            return {"status": "error", "message": "rest service unavailable"}
        window_id = (args.get("window_id") or "").strip()
        if not window_id:
            return {"status": "error", "message": "window_id required"}
        window = rest_service.get_window(window_id)
        success = rest_service.cancel_window(window_id)
        if not success:
            return {"status": "error", "message": "window not found"}
        if session_monitor and window and window.session_type == "task":
            session_monitor.cancel(window_id, window=window)
        if user_state_service:
            has_tracker = bool(tracker and tracker.list_active(chat_id)) if tracker else False
            still_resting = rest_service.is_resting(chat_id)
            next_action = (
                "推进中"
                if (has_tracker and not still_resting)
                else ("unknown" if not still_resting else None)
            )
            if window and window.session_type == "rest":
                user_state_service.update_state(
                    chat_id,
                    action=next_action,
                    has_active_tracker=has_tracker,
                    is_resting=still_resting,
                )
        return {"status": "ok", "window_id": window_id}

    tools = [
        AgentTool(
            name="today_tasks",
            description="读取任务与日志数据，返回今日关键任务（按优先级、截止时间排序）。可由 /tasks 命令触发。",
            parameters={"type": "object", "properties": {}},
            executor=summarize_executor,
        )
    ]
    if notion_sync_service:
        tools.append(
            AgentTool(
                name="refresh_notion_data",
                description="立即同步 Notion 项目/任务/日志，避免信息滞后。",
                parameters={
                    "type": "object",
                    "properties": {
                        "reason": {
                            "type": "string",
                            "description": "可选说明，记录在日志中便于排查",
                        }
                    },
                },
                executor=refresh_notion_executor,
            )
        )
    tools.extend(
        [
            AgentTool(
                name="record_log",
                description="记录一条任务进展日志，可只提供自然语言描述，工具会自动匹配/创建任务。",
                parameters={
                    "type": "object",
                    "properties": {
                        "text": {"type": "string"},
                        "task_name": {"type": "string"},
                        "task_id": {"type": "string"},
                        "note": {"type": "string"},
                    },
                },
                executor=log_executor,
            ),
            AgentTool(
                name="check_status_guard",
                description="返回即将到期或异常任务列表，用于触发强制提醒。",
                parameters={"type": "object", "properties": {}},
                executor=focus_executor,
            ),
            AgentTool(
                name="search_task",
                description="根据模糊描述在任务库中检索任务，返回任务信息。",
                parameters={
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "任务名称或描述中的关键词",
                        }
                    },
                    "required": ["query"],
                },
                executor=search_executor,
            ),
        ]
    )
    if tracker and task_repository:
        tools.append(
            AgentTool(
                name="start_tracker",
                description="为指定任务创建 25 分钟的跟踪提醒，可传任务 ID 或名称。",
                parameters={
                    "type": "object",
                    "properties": {
                        "task_id": {"type": "string"},
                        "task_name": {"type": "string"},
                        "interval_minutes": {
                            "type": "integer",
                            "description": "自定义首次提醒间隔（分钟，默认 25）",
                        },
                    },
                },
                executor=tracker_executor,
            )
        )
        tools.append(
            AgentTool(
                name="stop_tracker",
                description="取消当前正在进行的跟踪提醒。",
                parameters={
                    "type": "object",
                    "properties": {
                        "task_name": {"type": "string"},
                    },
                },
                executor=stop_tracker_executor,
            )
        )
    if log_repository:
        tools.append(
            AgentTool(
                name="list_logs",
                description="返回最近的日志记录（含内容、状态与关联任务），用于回顾进展或引用历史事实。",
                parameters={
                    "type": "object",
                    "properties": {
                        "limit": {
                            "type": "integer",
                            "description": "需要返回的日志数量，默认 5，最大 20。",
                        }
                    },
                },
                executor=logs_executor,
            )
        )
        tools.append(
            AgentTool(
                name="update_log",
                description="更新一条已有日志的内容或关联任务。",
                parameters={
                    "type": "object",
                    "properties": {
                        "log_id": {"type": "string"},
                        "note": {"type": "string"},
                        "task_name": {"type": "string"},
                        "task_id": {"type": "string"},
                    },
                    "required": ["log_id"],
                },
                executor=update_log_executor,
            )
        )
    if task_repository:
        tools.extend(
            [
                AgentTool(
                    name="create_task",
                    description="创建一个新的本地任务（仅供 Agent 使用）。",
                    parameters={
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "content": {"type": "string"},
                            "priority": {"type": "string"},
                            "status": {"type": "string"},
                            "project_name": {"type": "string"},
                            "due_date": {"type": "string"},
                        },
                        "required": ["name"],
                    },
                    executor=create_task_executor,
                ),
                AgentTool(
                    name="update_task",
                    description="更新本地任务（仅限 Agent 创建的任务）。",
                    parameters={
                        "type": "object",
                        "properties": {
                            "task_id": {"type": "string"},
                            "name": {"type": "string"},
                            "content": {"type": "string"},
                            "priority": {"type": "string"},
                            "status": {"type": "string"},
                            "project_name": {"type": "string"},
                            "due_date": {"type": "string"},
                        },
                        "required": ["task_id"],
                    },
                    executor=update_task_executor,
                ),
                AgentTool(
                    name="delete_task",
                    description="删除本地任务（仅限 Agent 创建的任务）。",
                    parameters={
                        "type": "object",
                        "properties": {
                            "task_id": {"type": "string"},
                        },
                        "required": ["task_id"],
                    },
                    executor=delete_task_executor,
                ),
            ]
        )
    if user_state_service:
        tools.append(
            AgentTool(
                name="report_state",
                description="汇报当前的行动状态/心理状态，供主动提醒逻辑使用。",
                parameters={
                    "type": "object",
                    "properties": {
                        "action": {"type": "string", "description": "例如 推进中/休息中"},
                        "mental": {"type": "string", "description": "例如 稳定/波动/高危"},
                    },
                },
                executor=report_state_executor,
            )
        )
    if rest_service:
        tools.extend(
            [
                AgentTool(
                    name="rest_list",
                    description="查看当前或即将生效的时间块（休息/任务专注）。",
                    parameters={
                        "type": "object",
                        "properties": {
                            "include_past": {
                                "type": "boolean",
                                "description": "true 时包含已经结束的记录",
                            }
                        },
                    },
                    executor=rest_list_executor,
                ),
                AgentTool(
                    name="rest_propose",
                    description="创建一个新的时间块，可以是休息或指定任务的专注窗口（立即生效）",
                    parameters={
                        "type": "object",
                        "properties": {
                            "start": {
                                "type": "string",
                                "description": "ISO8601 起始时间，例如 2025-11-14T09:00:00+08:00",
                            },
                            "end": {
                                "type": "string",
                                "description": "ISO8601 结束时间，必须晚于 start",
                            },
                            "note": {
                                "type": "string",
                                "description": "可选理由或补充说明",
                            },
                            "session_type": {
                                "type": "string",
                                "description": "rest 表示休息，task 表示针对某个任务的时间块",
                            },
                            "task_name": {
                                "type": "string",
                                "description": "session_type=task 时填写的人类可读任务名称",
                            },
                            "task_id": {
                                "type": "string",
                                "description": "session_type=task 时可以传任务 ID",
                            },
                        },
                        "required": ["start", "end"],
                    },
                    executor=rest_propose_executor,
                ),
                AgentTool(
                    name="rest_cancel",
                    description="取消一个休息窗口（立即生效）",
                    parameters={
                        "type": "object",
                        "properties": {
                            "window_id": {"type": "string"},
                        },
                        "required": ["window_id"],
                    },
                    executor=rest_cancel_executor,
                ),
            ]
        )
    return tools
