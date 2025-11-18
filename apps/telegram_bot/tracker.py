from __future__ import annotations

import threading
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
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
    context: str = "task"
    metadata: Dict[str, Any] = field(default_factory=dict)
    next_fire_at: datetime = field(default_factory=_utcnow)
    rest_resume_at: Optional[datetime] = None


class TaskTracker:
    def __init__(
        self,
        client: TelegramBotClient,
        interval_seconds: int = 1500,
        follow_up_seconds: int = 600,
        rest_service: RestScheduleService | None = None,
        timer_factory=None,
        user_state: UserStateService | None = None,
        storage_path: Path | None = None,
    ):
        self._client = client
        self._initial_interval = interval_seconds
        self._follow_up_interval = follow_up_seconds
        self._rest_service = rest_service
        self._entries: Dict[int, Dict[str, TrackerEntry]] = {}
        self._lock = threading.Lock()
        self._timer_factory = timer_factory or self._default_timer
        self._user_state = user_state
        self._storage_path = Path(storage_path) if storage_path else None
        if self._storage_path:
            self._storage_path.parent.mkdir(parents=True, exist_ok=True)
        self._load_from_disk()

    def _default_timer(self, delay, callback, args):
        timer = threading.Timer(delay, callback, args=args)
        timer.daemon = True
        return timer

    def start_tracking(
        self,
        chat_id: int,
        task: Task,
        interval_minutes: Optional[int] = None,
        update_action_state: bool = False,
        notify_user: bool = True,
        context: str = "task",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        custom_interval = None
        if interval_minutes:
            custom_interval = max(5, interval_minutes) * 60
        interval = custom_interval or self._initial_interval
        now = _utcnow()
        delay = interval
        rest_end = None
        if self._rest_service and self._rest_service.is_resting(chat_id, now):
            resume = self._rest_service.next_resume_time(chat_id, now)
            if resume:
                rest_end = resume
                default_due = now + timedelta(seconds=interval)
                if default_due <= resume:
                    delay = max(1.0, (resume - now).total_seconds())
        now = _utcnow()
        with self._lock:
            entry_map = self._entries.setdefault(chat_id, {})
            existing = entry_map.pop(task.id, None)
            if existing and existing.timer:
                existing.timer.cancel()
            timer = self._timer_factory(delay, self._send_reminder, (chat_id, task.id))
            entry = TrackerEntry(
                task_id=task.id,
                task_name=task.name,
                task_url=task.page_url or f"https://www.notion.so/{task.id.replace('-', '')}",
                timer=timer,
                interval_seconds=interval,
                context=context,
                metadata=metadata or {},
                start_time=now,
                next_fire_at=now + timedelta(seconds=delay),
                rest_resume_at=rest_end,
            )
            entry_map[task.id] = entry
            timer.start()
        if notify_user:
            minutes = interval // 60
            self._client.send_message(
                chat_id=chat_id,
                text=(
                    f"已开始跟踪 {escape_md(task.name)}，"
                    f"{minutes} 分钟后将再次询问。"
                ),
            )
        if update_action_state:
            self._sync_action_state(chat_id, "推进中", has_tracker=True)
        self._persist()

    def _send_reminder(self, chat_id: int, task_id: str) -> None:
        with self._lock:
            entry_map = self._entries.get(chat_id)
            if not entry_map:
                return
            entry = entry_map.get(task_id)
            if not entry:
                return
            now = _utcnow()
            if self._rest_service and self._rest_service.is_resting(chat_id, now):
                resume = self._rest_service.next_resume_time(chat_id, now)
                delay = (resume - now).total_seconds() if resume else self._follow_up_interval
                entry.timer = self._timer_factory(delay, self._send_reminder, (chat_id, task_id))
                entry.timer.start()
                entry.start_time = now
                entry.waiting = False
                entry.next_fire_at = now + timedelta(seconds=delay)
                entry.rest_resume_at = resume
                return
            entry.waiting = True
            entry.start_time = now
            if self._follow_up_interval > 0:
                entry.timer = self._timer_factory(
                    self._follow_up_interval, self._send_reminder, (chat_id, task_id)
                )
                entry.timer.start()
                entry.next_fire_at = now + timedelta(seconds=self._follow_up_interval)
                entry.rest_resume_at = None
            else:
                entry.next_fire_at = now
        self._client.send_message(
            chat_id=chat_id,
            text=(
                f"⏰ 时间到。请汇报任务 [{escape_md(entry.task_name)}]"
                f"({entry.task_url}) 的进展，并说明下一步。"
            ),
        )
        self._persist()

    def consume_reply(self, chat_id: int, user_text: str) -> Optional[str]:
        with self._lock:
            entry_map = self._entries.get(chat_id)
            if not entry_map:
                return None
            waiting_items = [
                (task_id, entry)
                for task_id, entry in entry_map.items()
                if entry.waiting
            ]
            if not waiting_items:
                return None
            target_task_id: Optional[str] = None
            if len(waiting_items) == 1:
                target_task_id = waiting_items[0][0]
            else:
                lowered = user_text.lower()
                for task_id, entry in waiting_items:
                    if entry.task_id and entry.task_id in user_text:
                        target_task_id = task_id
                        break
                    if entry.task_name and entry.task_name.lower() in lowered:
                        target_task_id = task_id
                        break
            if not target_task_id:
                return None
            entry = entry_map.pop(target_task_id, None)
            if not entry:
                return None
            if entry.timer:
                entry.timer.cancel()
            has_tracker = bool(entry_map)
            if not entry_map:
                self._entries.pop(chat_id, None)
        response = (
            f"跟踪任务 {entry.task_name} 的进展反馈：{user_text}\n"
            f"请结合任务链接 {entry.task_url} 的状态，给出下一步建议。"
        )
        if not has_tracker:
            self._sync_action_state(chat_id, "unknown", has_tracker=False)
        self._persist()
        return response

    def request_feedback(
        self,
        chat_id: int,
        task: Task,
        prompt: str,
        context: str = "follow_up",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        with self._lock:
            entry_map = self._entries.setdefault(chat_id, {})
            existing = entry_map.pop(task.id, None)
            if existing and existing.timer:
                existing.timer.cancel()
            entry = TrackerEntry(
                task_id=task.id,
                task_name=task.name,
                task_url=task.page_url or f"https://www.notion.so/{task.id.replace('-', '')}",
                timer=None,
                waiting=True,
                interval_seconds=self._follow_up_interval,
                context=context,
                metadata=metadata or {},
                start_time=_utcnow(),
                next_fire_at=_utcnow() + timedelta(seconds=self._follow_up_interval)
            )
            if self._follow_up_interval > 0:
                entry.timer = self._timer_factory(
                    self._follow_up_interval, self._send_reminder, (chat_id, task.id)
                )
                entry.timer.start()
            entry_map[task.id] = entry
        self._client.send_message(chat_id=chat_id, text=prompt)
        self._persist()

    def stop_tracking(
        self,
        chat_id: int,
        task_hint: str | None = None,
    ) -> Optional[TrackerEntry]:
        with self._lock:
            entry_map = self._entries.get(chat_id)
            if not entry_map:
                return None
            target_id: Optional[str] = None
            if task_hint:
                lowered = task_hint.lower()
                target_id = next(
                    (task_id for task_id in entry_map.keys() if task_id.lower() == lowered),
                    None,
                )
                if not target_id:
                    target_id = next(
                        (
                            task_id
                            for task_id, entry in entry_map.items()
                            if lowered in entry.task_name.lower()
                        ),
                        None,
                    )
            else:
                if len(entry_map) == 1:
                    target_id = next(iter(entry_map.keys()))
                else:
                    return None
            if not target_id:
                return None
            entry = entry_map.pop(target_id, None)
            if entry and entry.timer:
                entry.timer.cancel()
            has_tracker = bool(entry_map)
            if not entry_map:
                self._entries.pop(chat_id, None)
        if entry and not has_tracker:
            self._sync_action_state(chat_id, "unknown", has_tracker=False)
        self._persist()
        return entry

    def clear(self, chat_id: int) -> None:
        with self._lock:
            entry_map = self._entries.pop(chat_id, None)
        if not entry_map:
            return
        for entry in entry_map.values():
            if entry.timer:
                entry.timer.cancel()
        self._sync_action_state(chat_id, "unknown", has_tracker=False)
        self._persist()

    def list_active(self, chat_id: int) -> list[TrackerEntry]:
        with self._lock:
            entry_map = self._entries.get(chat_id, {})
            return sorted(entry_map.values(), key=lambda item: item.start_time)

    def next_event(self, chat_id: int) -> Optional[Dict[str, Any]]:
        events = self.list_next_events(chat_id)
        return events[0] if events else None

    def list_next_events(self, chat_id: int) -> list[Dict[str, Any]]:
        with self._lock:
            entry_map = self._entries.get(chat_id)
            if not entry_map:
                return []
            now = _utcnow()
            results: list[Dict[str, Any]] = []
            resting = self._rest_service.is_resting(chat_id, now) if self._rest_service else False
            resume_time = (
                self._rest_service.next_resume_time(chat_id, now)
                if resting and self._rest_service
                else None
            )
            for entry in entry_map.values():
                waiting = entry.waiting or (entry.rest_resume_at is not None and entry.rest_resume_at > now)
                due = entry.next_fire_at
                if not due.tzinfo:
                    due = due.replace(tzinfo=timezone.utc)
                results.append(
                    {
                        "task_name": entry.task_name,
                        "due_time": due.isoformat(),
                        "waiting": waiting,
                    }
                )
            results.sort(key=lambda item: item["due_time"])
            return results

    def _sync_action_state(self, chat_id: int, action: str, has_tracker: bool) -> None:
        if not self._user_state:
            return
        now = _utcnow()
        is_resting = self._rest_service.is_resting(chat_id, now) if self._rest_service else False
        task_block_active = (
            self._rest_service.has_active_task_block(chat_id, now) if self._rest_service else False
        )
        if action == "推进中" and not task_block_active:
            return
        self._user_state.update_state(
            chat_id,
            action=action,
            has_active_tracker=has_tracker,
            is_resting=is_resting,
            has_task_block=task_block_active,
        )

    def defer_for_rest(self, chat_id: int, start: datetime, end: datetime) -> None:
        with self._lock:
            entry_map = self._entries.get(chat_id)
            if not entry_map:
                return
            now = _utcnow()
            if not (start <= now < end):
                return
            delay = max(1.0, (end - now).total_seconds())
            changed = False
            for task_id, entry in entry_map.items():
                if entry.timer is None:
                    continue
                if entry.rest_resume_at is None:
                    entry.rest_resume_at = end
                if entry.next_fire_at > end:
                    entry.rest_resume_at = None
                    continue
                entry.timer.cancel()
                entry.timer = self._timer_factory(delay, self._send_reminder, (chat_id, task_id))
                entry.timer.start()
                entry.waiting = False
                entry.start_time = now
                entry.next_fire_at = now + timedelta(seconds=delay)
                entry.rest_resume_at = end
                changed = True
        if changed:
            self._persist()

    def _persist(self) -> None:
        if not self._storage_path:
            return
        snapshot: Dict[str, Dict[str, Any]] = {}
        with self._lock:
            for chat_id, entry_map in self._entries.items():
                snapshot[str(chat_id)] = {}
                for task_id, entry in entry_map.items():
                    snapshot[str(chat_id)][task_id] = {
                        "task_id": entry.task_id,
                        "task_name": entry.task_name,
                        "task_url": entry.task_url,
                        "waiting": entry.waiting,
                        "start_time": entry.start_time.isoformat(),
                        "interval_seconds": entry.interval_seconds,
                        "context": entry.context,
                        "metadata": entry.metadata,
                        "next_fire_at": entry.next_fire_at.isoformat(),
                        "rest_resume_at": entry.rest_resume_at.isoformat() if entry.rest_resume_at else None,
                    }
        self._storage_path.write_text(
            json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def _load_from_disk(self) -> None:
        if not self._storage_path or not self._storage_path.exists():
            return
        try:
            data = json.loads(self._storage_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return
        now = _utcnow()
        with self._lock:
            for chat_id_str, payload in data.items():
                try:
                    chat_id = int(chat_id_str)
                except ValueError:
                    continue
                entry_map = self._entries.setdefault(chat_id, {})
                for task_id, entry_payload in payload.items():
                    try:
                        start_time = datetime.fromisoformat(entry_payload["start_time"])
                        next_fire = datetime.fromisoformat(entry_payload["next_fire_at"])
                        resume_at_raw = entry_payload.get("rest_resume_at")
                        resume_at = datetime.fromisoformat(resume_at_raw) if resume_at_raw else None
                    except (KeyError, ValueError):
                        continue
                    entry = TrackerEntry(
                        task_id=task_id,
                        task_name=entry_payload.get("task_name", task_id),
                        task_url=entry_payload.get("task_url") or f"https://www.notion.so/{task_id.replace('-', '')}",
                        timer=None,
                        waiting=entry_payload.get("waiting", False),
                        start_time=start_time,
                        interval_seconds=entry_payload.get("interval_seconds", self._initial_interval),
                        context=entry_payload.get("context", "task"),
                        metadata=entry_payload.get("metadata", {}),
                        next_fire_at=next_fire,
                        rest_resume_at=resume_at,
                    )
                    delay = max(1.0, (next_fire - now).total_seconds())
                    entry.timer = self._timer_factory(delay, self._send_reminder, (chat_id, task_id))
                    entry.timer.start()
                    entry_map[task_id] = entry
