from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from apps.telegram_bot.clients import TelegramBotClient
from apps.telegram_bot.rest import RestScheduleService
from apps.telegram_bot.user_state import UserStateService
from core.domain import Task
from core.utils.timezone import to_beijing

MD_SPECIAL_CHARS = "\\[]"


def escape_md(text: str) -> str:
    if not text:
        return ""
    escaped: list[str] = []
    for char in text:
        if char in MD_SPECIAL_CHARS:
            escaped.append(f"\\{char}")
        else:
            escaped.append(char)
    return "".join(escaped)


def _utcnow() -> datetime:
    return datetime.utcnow().replace(tzinfo=timezone.utc)


@dataclass
class TrackerEntry:
    task_id: str
    task_name: str
    task_url: str
    timer: Optional[threading.Timer]
    waiting: bool = False
    start_time: datetime = field(default_factory=_utcnow)
    interval_seconds: int = 1500


class TaskTracker:
    def __init__(
        self,
        client: TelegramBotClient,
        interval_seconds: int = 1500,
        follow_up_seconds: int = 600,
        rest_service: RestScheduleService | None = None,
        timer_factory=None,
        user_state: UserStateService | None = None,
    ):
        self._client = client
        self._initial_interval = interval_seconds
        self._follow_up_interval = follow_up_seconds
        self._rest_service = rest_service
        self._entries: Dict[int, TrackerEntry] = {}
        self._lock = threading.Lock()
        self._timer_factory = timer_factory or self._default_timer
        self._user_state = user_state
        self._rest_paused: Dict[int, datetime] = {}

    def _default_timer(self, delay, callback, args):
        timer = threading.Timer(delay, callback, args=args)
        timer.daemon = True
        return timer

    def start_tracking(
        self,
        chat_id: int,
        task: Task,
        interval_minutes: Optional[int] = None,
        update_action_state: bool = True,
        notify_user: bool = True,
    ) -> None:
        custom_interval = None
        if interval_minutes:
            custom_interval = max(5, min(180, interval_minutes)) * 60
        interval = custom_interval or self._initial_interval
        with self._lock:
            self._cancel(chat_id)
            timer = self._timer_factory(interval, self._send_reminder, (chat_id,))
            entry = TrackerEntry(
                task_id=task.id,
                task_name=task.name,
                task_url=task.page_url or f"https://www.notion.so/{task.id.replace('-', '')}",
                timer=timer,
                interval_seconds=interval,
            )
            self._entries[chat_id] = entry
            timer.start()
        if notify_user:
            minutes = interval // 60
            self._client.send_message(
                chat_id=chat_id,
                text=(
                    f"\u5df2\u5f00\u59cb\u8ddf\u8e2a {escape_md(task.name)}\uff0c"
                    f"{minutes} \u5206\u949f\u540e\u5c06\u518d\u6b21\u8be2\u95ee\u3002"
                ),
            )
        if update_action_state:
            self._sync_action_state(chat_id, "\u63a8\u8fdb\u4e2d", has_tracker=True)

    def _send_reminder(self, chat_id: int) -> None:
        with self._lock:
            entry = self._entries.get(chat_id)
            if not entry:
                return
            now = _utcnow()
            if self._rest_service and self._rest_service.is_resting(chat_id, now):
                resume = self._rest_service.next_resume_time(chat_id, now)
                delay = (resume - now).total_seconds() if resume else self._follow_up_interval
                entry.timer = self._timer_factory(delay, self._send_reminder, (chat_id,))
                entry.timer.start()
                return
            entry.waiting = True
            if self._follow_up_interval > 0:
                entry.timer = self._timer_factory(self._follow_up_interval, self._send_reminder, (chat_id,))
                entry.timer.start()
        self._client.send_message(
            chat_id=chat_id,
            text=(
                f"\u23f0 \u65f6\u95f4\u5230\u3002\u8bf7\u6c47\u62a5\u4efb\u52a1 [{escape_md(entry.task_name)}]"
                f"({entry.task_url}) \u7684\u8fdb\u5c55\uff0c\u5e76\u8bf4\u660e\u4e0b\u4e00\u6b65\u3002"
            ),
        )

    def consume_reply(self, chat_id: int, user_text: str) -> Optional[str]:
        with self._lock:
            entry = self._entries.get(chat_id)
            if not entry or not entry.waiting:
                return None
            if entry.timer:
                entry.timer.cancel()
            self._entries.pop(chat_id, None)
        response = (
            f"\u8ddf\u8e2a\u4efb\u52a1 {entry.task_name} \u7684\u8fdb\u5c55\u53cd\u9988\uff1a{user_text}\n"
            f"\u8bf7\u7ed3\u5408\u4efb\u52a1\u94fe\u63a5 {entry.task_url} \u7684\u72b6\u6001\uff0c\u7ed9\u51fa\u4e0b\u4e00\u6b65\u5efa\u8bae\u3002"
        )
        self._sync_action_state(chat_id, "unknown", has_tracker=False)
        return response

    def stop_tracking(self, chat_id: int, ensure_name: str | None = None) -> Optional[TrackerEntry]:
        with self._lock:
            entry = self._entries.get(chat_id)
            if not entry:
                return None
            if ensure_name and ensure_name.lower() not in entry.task_name.lower():
                return None
            cancelled = self._cancel(chat_id)
        if cancelled:
            self._sync_action_state(chat_id, "unknown", has_tracker=False)
        return cancelled

    def clear(self, chat_id: int) -> None:
        self.stop_tracking(chat_id)

    def list_active(self, chat_id: int) -> list[TrackerEntry]:
        with self._lock:
            entry = self._entries.get(chat_id)
            return [entry] if entry else []

    def next_event(self, chat_id: int) -> Optional[Dict[str, Any]]:
        with self._lock:
            entry = self._entries.get(chat_id)
            if not entry:
                return None
            now = _utcnow()
            if self._rest_service and self._rest_service.is_resting(chat_id, now):
                resume = self._rest_service.next_resume_time(chat_id, now)
                due = resume or now
                waiting = True
            elif entry.waiting and self._follow_up_interval > 0:
                due = now + timedelta(seconds=self._follow_up_interval)
                waiting = True
            else:
                due = entry.start_time + timedelta(seconds=self._initial_interval)
                waiting = False
            due_local = to_beijing(due if due.tzinfo else due.replace(tzinfo=timezone.utc))
            return {
                "task_name": entry.task_name,
                "due_time": due_local.isoformat(),
                "waiting": waiting or entry.waiting,
            }

    def _cancel(self, chat_id: int) -> Optional[TrackerEntry]:
        entry = self._entries.pop(chat_id, None)
        if entry and entry.timer:
            entry.timer.cancel()
        return entry

    def _sync_action_state(self, chat_id: int, action: str, has_tracker: bool) -> None:
        if not self._user_state:
            return
        is_resting = False
        if self._rest_service:
            is_resting = self._rest_service.is_resting(chat_id, _utcnow())
        self._user_state.update_state(
            chat_id,
            action=action,
            has_active_tracker=has_tracker,
            is_resting=is_resting,
        )

    def defer_for_rest(self, chat_id: int, start: datetime, end: datetime) -> None:
        with self._lock:
            entry = self._entries.get(chat_id)
            if not entry or not entry.timer:
                return
            now = _utcnow()
            if not (start <= now < end):
                return
            entry.timer.cancel()
            delay = max(1.0, (end - now).total_seconds())
            entry.timer = self._timer_factory(delay, self._send_reminder, (chat_id,))
            entry.timer.start()
            entry.waiting = False
