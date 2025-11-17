from datetime import datetime, timedelta, timezone

from apps.telegram_bot.proactivity import ProactivityService
from apps.telegram_bot.rest import RestScheduleService
from apps.telegram_bot.user_state import UserState
from core.utils.timezone import to_beijing


class DummyStateService:
    def __init__(self, state: UserState):
        self._state = state

    def get_state(self, chat_id: int, **kwargs):
        return self._state


def test_pending_state_due_respects_upcoming_rest(monkeypatch, tmp_path):
    chat_id = 42
    base_time = datetime(2025, 11, 15, 17, 28, tzinfo=timezone.utc)
    rest_start = base_time + timedelta(seconds=60)
    rest_end = rest_start + timedelta(hours=10)
    monkeypatch.setattr("apps.telegram_bot.rest._utcnow", lambda: base_time)
    rest_service = RestScheduleService(tmp_path / "rest_windows.json")
    rest_service.add_window(chat_id, start=rest_start, end=rest_end, note="nap time")

    user_state = UserState(
        action="unknown",
        mental="稳定",
        action_updated_at=base_time - timedelta(hours=2),
        mental_updated_at=base_time - timedelta(hours=5),
        action_prompted_at=None,
        mental_prompted_at=base_time - timedelta(seconds=30),
    )
    state_service = DummyStateService(user_state)
    proactivity = ProactivityService(
        state_service=state_service,
        rest_service=rest_service,
        state_check_seconds=60,
        state_stale_seconds=3600,
        state_prompt_cooldown_seconds=60,
        follow_up_seconds=600,
        state_unknown_retry_seconds=120,
    )
    monkeypatch.setattr("apps.telegram_bot.proactivity._utcnow", lambda: base_time)

    desc = proactivity.describe_next_prompts(chat_id)

    assert desc["mental"]["pending"] is True
    assert desc["mental"]["due_time"] == to_beijing(rest_end).isoformat()
