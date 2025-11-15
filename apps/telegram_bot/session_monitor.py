from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from apps.telegram_bot.clients import TelegramBotClient
from apps.telegram_bot.rest import RestScheduleService, RestWindow
from apps.telegram_bot.tracker import TaskTracker
from core.domain import Task
from core.repositories import TaskRepository
from core.utils.timezone import format_beijing


def _utcnow() -> datetime:
    return datetime.utcnow().replace(tzinfo=timezone.utc)


class TaskSessionMonitor:
    def __init__(
        self,
        client: TelegramBotClient,
        rest_service: RestScheduleService,
        tracker: TaskTracker | None = None,
        task_repository: TaskRepository | None = None,
    ):
        self._client = client
        self._rest_service = rest_service
        self._tracker = tracker
        self._task_repo = task_repository
        self._start_timers: Dict[str, threading.Timer] = {}
        self._end_timers: Dict[str, threading.Timer] = {}
        self._active_sessions: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._bootstrap()

    def _bootstrap(self) -> None:
        now = _utcnow()
        for window in self._rest_service.iter_windows(include_past=False):
            silent = window.start <= now <= window.end
            self.schedule(window, silent_start=silent)

    def schedule(self, window: RestWindow, silent_start: bool = False) -> None:
        if window.session_type != "task":
            return
        with self._lock:
            self._cancel_locked(window.id)
            self._active_sessions.pop(window.id, None)
            now = _utcnow()
            if window.end <= now:
                self._handle_end(window.id)
                return
            if window.start <= now:
                self._start_session(window, silent=silent_start)
            else:
                start_delay = max(1.0, (window.start - now).total_seconds())
                start_timer = threading.Timer(start_delay, self._handle_start, args=(window.id,))
                start_timer.daemon = True
                start_timer.start()
                self._start_timers[window.id] = start_timer
            end_delay = max(1.0, (window.end - now).total_seconds())
            end_timer = threading.Timer(end_delay, self._handle_end, args=(window.id,))
            end_timer.daemon = True
            end_timer.start()
            self._end_timers[window.id] = end_timer

    def cancel(self, window_id: str, window: RestWindow | None = None) -> None:
        window = window or self._rest_service.get_window(window_id)
        with self._lock:
            self._cancel_locked(window_id)
            active = self._active_sessions.pop(window_id, None)
        if active and self._tracker:
            self._tracker.stop_tracking(
                active.get("chat_id", window.chat_id if window else 0),
                ensure_name=active.get("task_name"),
            )
        # 主动取消的提示由命令/工具负责输出

    def _cancel_locked(self, window_id: str) -> None:
        for mapping in (self._start_timers, self._end_timers):
            timer = mapping.pop(window_id, None)
            if timer:
                timer.cancel()

    def _handle_start(self, window_id: str) -> None:
        window = self._rest_service.get_window(window_id)
        if not window:
            with self._lock:
                self._cancel_locked(window_id)
            return
        self._start_session(window)

    def _handle_end(self, window_id: str) -> None:
        window = self._rest_service.get_window(window_id)
        if not window:
            with self._lock:
                self._cancel_locked(window_id)
            return
        active = self._active_sessions.pop(window_id, None)
        active_task = None
        if active:
            active_task = active.get("task")
            if self._tracker:
                self._tracker.stop_tracking(
                    active.get("chat_id", window.chat_id),
                    ensure_name=active.get("task_name"),
                )
        self._notify(window)
        follow_up_task = active_task or self._resolve_task(window)
        if self._tracker and follow_up_task:
            prompt = (
                f"⌛ {follow_up_task.name} 的时间块已结束。\n"
                "请说明是否完成、遇到了哪些问题，以及下一步安排，我将根据反馈继续提醒。"
            )
            self._tracker.request_feedback(
                window.chat_id,
                follow_up_task,
                prompt=prompt,
                context="block_follow_up",
                metadata={"window_id": window.id},
            )
        self._rest_service.delete_window(window_id)
        with self._lock:
            self._cancel_locked(window_id)

    def _notify(self, window: RestWindow) -> None:
        task_label = window.task_name or window.note or "（未命名任务）"
        end_time = format_beijing(window.end)
        text = (
            f"⏰ 任务时间块已结束：{task_label}\n"
            f"结束时间：{end_time}\n"
            "请确认是否完成该任务，必要时重新规划新的时间段。"
        )
        self._client.send_message(chat_id=window.chat_id, text=text)

    def _start_session(self, window: RestWindow, silent: bool = False) -> None:
        if window.session_type != "task":
            return
        task = self._resolve_task(window)
        if not task:
            self._client.send_message(
                chat_id=window.chat_id,
                text="⚠️ 未能识别时间块目标任务，无法自动开启跟踪。",
            )
            return
        self._active_sessions[window.id] = {
            "chat_id": window.chat_id,
            "task_name": task.name,
            "task": task,
        }
        if self._tracker:
            self._tracker.start_tracking(
                window.chat_id,
                task,
                update_action_state=False,
                notify_user=False,
            )
        if not silent:
            self._client.send_message(
                chat_id=window.chat_id,
                text=f"🎯 任务时间块开始：{task.name}\n我已自动开启跟踪，请专注推进并及时反馈。",
            )

    def _resolve_task(self, window: RestWindow):
        if not self._task_repo:
            return None
        task = None
        if window.task_id:
            task = self._task_repo.get_task(window.task_id)
        if not task and window.task_name:
            task = self._task_repo.find_by_name(window.task_name)
        if not task:
            inferred = window.task_name or window.note or "任务时间块"
            task = self._task_repo.ensure_task(inferred)
        return task
