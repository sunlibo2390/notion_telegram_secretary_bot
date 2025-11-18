from __future__ import annotations

from dataclasses import dataclass
from typing import Any, List

from apps.telegram_bot.tracker import TaskTracker
from core.domain import Task


@dataclass
class DummyMessage:
    chat_id: int
    text: str


class DummyClient:
    def __init__(self) -> None:
        self.messages: List[DummyMessage] = []

    def send_message(self, chat_id: int, text: str, **_: Any) -> None:
        self.messages.append(DummyMessage(chat_id, text))


class FakeTimer:
    def __init__(self, delay, callback, args):
        self.delay = delay
        self._callback = callback
        self._args = args
        self.cancelled = False

    def start(self) -> None:
        # Timer is manually triggered from tests
        pass

    def fire(self) -> None:
        if not self.cancelled:
            self._callback(*self._args)

    def cancel(self) -> None:
        self.cancelled = True


def _build_timer_factory(created: List[FakeTimer]):
    def factory(delay, callback, args):
        timer = FakeTimer(delay, callback, args)
        created.append(timer)
        return timer

    return factory


def make_task(task_id: str, name: str) -> Task:
    return Task(
        id=task_id,
        name=name,
        priority="Medium",
        status="Doing",
        content="",
        project_id=None,
        project_name="",
        due_date=None,
        subtask_names=[],
        page_url=f"https://www.notion.so/{task_id}",
    )


def test_can_track_multiple_tasks_and_stop_specific_one():
    client = DummyClient()
    timers: List[FakeTimer] = []
    tracker = TaskTracker(client, timer_factory=_build_timer_factory(timers))
    task_a = make_task("task-a", "任务A")
    task_b = make_task("task-b", "任务B")
    tracker.start_tracking(1, task_a, notify_user=False)
    tracker.start_tracking(1, task_b, notify_user=False)

    entries = tracker.list_active(1)
    assert len(entries) == 2

    # Without hint and multiple entries, stop_tracking returns None
    assert tracker.stop_tracking(1) is None

    removed = tracker.stop_tracking(1, task_hint="task-a")
    assert removed is not None
    assert removed.task_id == "task-a"

    remaining = tracker.list_active(1)
    assert len(remaining) == 1
    assert remaining[0].task_id == "task-b"


def test_consume_reply_matches_waiting_entry_by_name():
    client = DummyClient()
    timers: List[FakeTimer] = []
    tracker = TaskTracker(client, timer_factory=_build_timer_factory(timers))
    task_a = make_task("task-a", "任务A")
    tracker.start_tracking(1, task_a, notify_user=False)
    # Trigger reminder to mark entry as waiting
    timers[0].fire()
    assert tracker.consume_reply(1, "任务A 已完成，继续新的目标。")
    assert tracker.list_active(1) == []


def test_tracker_persistence_across_instances(tmp_path):
    storage = tmp_path / "tracker.json"
    client = DummyClient()
    timers: List[FakeTimer] = []
    tracker = TaskTracker(
        client,
        timer_factory=_build_timer_factory(timers),
        storage_path=storage,
        follow_up_seconds=60,
    )
    task = make_task("task-a", "任务A")
    tracker.start_tracking(1, task, interval_minutes=10, notify_user=False)
    assert storage.exists()

    # Recreate tracker and ensure entries reloaded
    new_timers: List[FakeTimer] = []
    tracker_reloaded = TaskTracker(
        client,
        timer_factory=_build_timer_factory(new_timers),
        storage_path=storage,
        follow_up_seconds=60,
    )
    entries = tracker_reloaded.list_active(1)
    assert len(entries) == 1
    assert entries[0].task_id == "task-a"
