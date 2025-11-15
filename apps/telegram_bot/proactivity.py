from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional

from apps.telegram_bot.rest import RestScheduleService
from apps.telegram_bot.tracker import TaskTracker
from apps.telegram_bot.user_state import UserStateService
from core.utils.timezone import to_beijing


def _utcnow() -> datetime:
    return datetime.utcnow().replace(tzinfo=timezone.utc)


STATE_EVENT = "state_prompt"
QUESTION_EVENT = "question_follow_up"


@dataclass
class PendingQuestion:
    text: str
    asked_at: datetime
    timer: Optional[threading.Timer] = None


@dataclass
class ChatTimers:
    state_timer: Optional[threading.Timer] = None
    pending_question: Optional[PendingQuestion] = None


class ProactivityService:
    def __init__(
        self,
        state_service: UserStateService,
        rest_service: RestScheduleService,
        state_check_seconds: int = 300,
        state_stale_seconds: int = 3600,
        state_prompt_cooldown_seconds: int = 600,
        follow_up_seconds: int = 600,
        tracker: TaskTracker | None = None,
    ) -> None:
        self._state_service = state_service
        self._rest_service = rest_service
        self._tracker = tracker
        self._state_check_seconds = max(60, state_check_seconds)
        self._state_stale_seconds = max(300, state_stale_seconds)
        self._state_prompt_cooldown = max(300, state_prompt_cooldown_seconds)
        self._follow_up_seconds = max(60, follow_up_seconds)
        self._timers: Dict[int, ChatTimers] = {}
        self._lock = threading.Lock()
        self._event_handler: Optional[Callable[[int, Dict[str, Any]], None]] = None
        self._timer_factory = lambda delay, cb, args: self._make_timer(delay, cb, args)

    @staticmethod
    def _make_timer(delay, callback, args):
        timer = threading.Timer(delay, callback, args=args)
        timer.daemon = True
        return timer

    def set_event_handler(self, handler: Callable[[int, Dict[str, Any]], None]) -> None:
        self._event_handler = handler

    def record_user_message(self, chat_id: int, _: str) -> bool:
        with self._lock:
            timers = self._timers.setdefault(chat_id, ChatTimers())
            self._clear_pending_question(timers)
            self._schedule_state_check(chat_id, timers)
        return False

    def record_agent_message(self, chat_id: int, text: str) -> None:
        text = text or ""
        if "?" not in text and "？" not in text:
            return
        with self._lock:
            timers = self._timers.setdefault(chat_id, ChatTimers())
            self._set_pending_question(chat_id, timers, text)

    def reset(self, chat_id: int) -> None:
        with self._lock:
            timers = self._timers.pop(chat_id, None)
        if not timers:
            return
        if timers.state_timer:
            timers.state_timer.cancel()
        if timers.pending_question and timers.pending_question.timer:
            timers.pending_question.timer.cancel()

    def describe_next_prompts(self, chat_id: int) -> Dict[str, Any]:
        now = _utcnow()
        is_resting = self._is_resting(chat_id, now)
        state = self._state_service.get_state(
            chat_id,
            has_active_tracker=self._has_active_tracker(chat_id),
            is_resting=is_resting,
        )
        action_pending, action_due = self._state_due(chat_id, state.action, state.action_updated_at, state.action_prompted_at, now)
        mental_pending, mental_due = self._state_due(chat_id, state.mental, state.mental_updated_at, state.mental_prompted_at, now)
        def _iso_beijing(dt: Optional[datetime]) -> Optional[str]:
            return to_beijing(dt).isoformat() if dt else None
        with self._lock:
            timers = self._timers.get(chat_id)
            question_info = None
            if timers and timers.pending_question:
                question_info = {
                    "pending": True,
                    "question": timers.pending_question.text,
                    "due_time": _iso_beijing(
                        timers.pending_question.asked_at + timedelta(seconds=self._follow_up_seconds)
                    ),
                }
            else:
                question_info = {"pending": False}
        rest_window = self._rest_service.current_window(chat_id, now)
        next_resume = self._rest_service.next_resume_time(chat_id, now)
        next_window = self._rest_service.next_window(chat_id)
        return {
            "action": {
                "pending": action_pending,
                "due_time": _iso_beijing(action_due),
                "value": state.action,
            },
            "mental": {
                "pending": mental_pending,
                "due_time": _iso_beijing(mental_due),
                "value": state.mental,
            },
            "question": question_info,
            "rest": {
                "active": bool(rest_window),
                "current_end": _iso_beijing(rest_window.end) if rest_window else None,
                "next_resume": _iso_beijing(next_resume),
                "next_window_start": _iso_beijing(next_window.start) if next_window else None,
                "next_window_end": _iso_beijing(next_window.end) if next_window else None,
                "next_window_status": next_window.status if next_window else None,
            },
        }

    def _schedule_state_check(self, chat_id: int, timers: ChatTimers) -> None:
        if timers.state_timer:
            timers.state_timer.cancel()
        timer = self._timer_factory(self._state_check_seconds, self._handle_state_check, (chat_id,))
        timers.state_timer = timer
        timer.start()

    def _handle_state_check(self, chat_id: int) -> None:
        with self._lock:
            timers = self._timers.get(chat_id)
            if not timers:
                return
            self._schedule_state_check(chat_id, timers)
        now = _utcnow()
        if self._is_resting(chat_id, now):
            return
        state = self._state_service.get_state(
            chat_id,
            has_active_tracker=self._has_active_tracker(chat_id),
            is_resting=False,
        )
        events: List[Dict[str, Any]] = []
        action_pending, _ = self._state_due(chat_id, state.action, state.action_updated_at, state.action_prompted_at, now)
        mental_pending, _ = self._state_due(chat_id, state.mental, state.mental_updated_at, state.mental_prompted_at, now)
        missing = []
        if action_pending:
            missing.append("action")
        if mental_pending:
            missing.append("mental")
        if missing and self._event_handler:
            events.append(
                {
                    "type": STATE_EVENT,
                    "missing": missing,
                    "last_action": state.action,
                    "last_mental": state.mental,
                }
            )
            self._state_service.mark_prompt(
                chat_id,
                action=("action" in missing),
                mental=("mental" in missing),
            )
        for event in events:
            self._event_handler(chat_id, event)

    def _state_due(
        self,
        chat_id: int,
        value: str,
        updated_at: Optional[datetime],
        prompted_at: Optional[datetime],
        now: datetime,
    ) -> tuple[bool, Optional[datetime]]:
        threshold = (
            updated_at + timedelta(seconds=self._state_stale_seconds)
            if updated_at
            else now
        )
        due = threshold
        if due.tzinfo is None:
            due = due.replace(tzinfo=timezone.utc)
        due = self._adjust_due_for_rest(chat_id, due, now)
        if prompted_at:
            cooldown_due = prompted_at + timedelta(seconds=self._state_prompt_cooldown)
            if due < cooldown_due:
                due = cooldown_due
        pending = value == "unknown" or not updated_at or now >= threshold
        if pending and due < now:
            due = now
        return pending, due

    def _adjust_due_for_rest(self, chat_id: int, due: Optional[datetime], now: datetime) -> Optional[datetime]:
        if not due or not self._rest_service:
            return due
        due_ref = due if due.tzinfo else due.replace(tzinfo=timezone.utc)
        active_now = self._rest_service.current_window(chat_id, now)
        if active_now and now <= active_now.end:
            return active_now.end
        window_at_due = self._rest_service.current_window(chat_id, due_ref)
        if window_at_due and window_at_due.end > due_ref:
            return window_at_due.end
        recent_cancel = self._rest_service.recent_cancelled_at(chat_id)
        if recent_cancel:
            candidate = recent_cancel + timedelta(minutes=2)
            if candidate > now:
                return candidate
        return due_ref

    def _set_pending_question(self, chat_id: int, timers: ChatTimers, text: str) -> None:
        if timers.pending_question and timers.pending_question.timer:
            timers.pending_question.timer.cancel()
        timer = self._timer_factory(self._follow_up_seconds, self._handle_question_timeout, (chat_id,))
        timers.pending_question = PendingQuestion(text=text, asked_at=_utcnow(), timer=timer)
        timer.start()

    def _clear_pending_question(self, timers: ChatTimers) -> None:
        if timers.pending_question and timers.pending_question.timer:
            timers.pending_question.timer.cancel()
        timers.pending_question = None

    def _handle_question_timeout(self, chat_id: int) -> None:
        with self._lock:
            timers = self._timers.get(chat_id)
            if not timers or not timers.pending_question:
                return
            question = timers.pending_question
            now = _utcnow()
            if self._rest_service.is_resting(chat_id, now):
                resume = self._rest_service.next_resume_time(chat_id, now)
                delay = (resume - now).total_seconds() if resume else self._follow_up_seconds
                question.timer = self._timer_factory(delay, self._handle_question_timeout, (chat_id,))
                return
            question.asked_at = now
            question.timer = self._timer_factory(self._follow_up_seconds, self._handle_question_timeout, (chat_id,))
            event = {"type": QUESTION_EVENT, "question": question.text}
        if self._event_handler:
            self._event_handler(chat_id, event)

    def _has_active_tracker(self, chat_id: int) -> bool:
        if not self._tracker:
            return False
        return bool(self._tracker.list_active(chat_id))

    def _is_resting(self, chat_id: int, when: Optional[datetime] = None) -> bool:
        if not self._rest_service:
            return False
        return bool(self._rest_service.is_resting(chat_id, when or _utcnow()))
