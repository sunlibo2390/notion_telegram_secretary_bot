from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

from apps.telegram_bot.handlers.commands import CommandRouter
from apps.telegram_bot.history.history_store import HistoryStore
from core.domain import Task


class DummyClient:
    def __init__(self):
        self.messages: List[dict] = []

    def send_message(self, chat_id: int, text: str, parse_mode: Optional[str] = None) -> None:
        self.messages.append({"chat_id": chat_id, "text": text, "parse_mode": parse_mode})


class FakeTaskRepo:
    def __init__(self, tasks: List[Task], custom_ids: List[str]):
        self.tasks = list(tasks)
        self.custom_ids = set(custom_ids)

    def list_active_tasks(self) -> List[Task]:
        return list(self.tasks)

    def get_task(self, task_id: str) -> Optional[Task]:
        return next((task for task in self.tasks if task.id == task_id), None)

    def update_custom_task(self, task_id: str, **updates) -> Optional[Task]:
        if task_id not in self.custom_ids:
            return None
        task = self.get_task(task_id)
        if not task:
            return None
        for field, value in updates.items():
            if value is None and field == "due_date":
                setattr(task, field, None)
            elif value is not None:
                setattr(task, field, value)
        return task

    def delete_custom_task(self, task_id: str) -> bool:
        if task_id not in self.custom_ids:
            return False
        for idx, task in enumerate(self.tasks):
            if task.id == task_id:
                self.tasks.pop(idx)
                self.custom_ids.discard(task_id)
                return True
        return False

    def is_custom_task(self, task_id: str) -> bool:
        return task_id in self.custom_ids


@dataclass
class DummyTrackingEntry:
    task_id: str
    task_name: str
    task_url: str = "https://www.notion.so/test"
    waiting: bool = False


class DummyTracker:
    def __init__(self, entries: Optional[List[DummyTrackingEntry]] = None):
        self.entries = entries or []
        self.last_stop: Optional[str] = None

    def list_active(self, chat_id: int) -> List[DummyTrackingEntry]:
        return list(self.entries)

    def stop_tracking(self, chat_id: int, task_hint: Optional[str] = None):
        if not self.entries:
            return None
        if task_hint:
            lowered = task_hint.lower()
            for idx, entry in enumerate(self.entries):
                if entry.task_id == task_hint or lowered in entry.task_name.lower():
                    self.last_stop = entry.task_id
                    return self.entries.pop(idx)
            return None
        if len(self.entries) == 1:
            entry = self.entries.pop(0)
            self.last_stop = entry.task_id
            return entry
        return None

    def clear(self, chat_id: int) -> None:
        self.entries.clear()


def _build_router(tmp_path, tracker=None):
    client = DummyClient()
    history = HistoryStore(root_dir=tmp_path / "history")
    tasks = [
        Task(
            id="custom-1",
            name="自建任务",
            priority="High",
            status="Todo",
            content="",
            project_id=None,
            project_name="实验",
            due_date="2099-01-01T00:00:00",
        ),
        Task(
            id="notion-1",
            name="Notion 任务",
            priority="Medium",
            status="Doing",
            content="",
            project_id="proj",
            project_name="主项目",
            due_date="2099-02-01T00:00:00",
            page_url="https://www.notion.so/test",
        ),
    ]
    repo = FakeTaskRepo(tasks=tasks, custom_ids=["custom-1"])
    router = CommandRouter(
        client=client,
        history_store=history,
        task_repo=repo,
        tracker=tracker,
    )
    return router, client, repo


def test_tasks_command_outputs_ordered_list(tmp_path):
    router, client, _ = _build_router(tmp_path)
    router._handle_tasks(chat_id=1, text="/tasks 2")
    sent = client.messages[-1]
    assert sent["parse_mode"] == "Markdown"
    assert sent["text"].splitlines()[0].startswith("1.")
    assert "提示：/tasks update" in sent["text"]
    assert router._task_snapshot[1]


def test_tasks_update_custom_by_index(tmp_path):
    router, client, repo = _build_router(tmp_path)
    router._handle_tasks(chat_id=1, text="/tasks")
    router._handle_tasks(chat_id=1, text="/tasks update 1 status=进行中 priority=Low")
    sent = client.messages[-1]
    assert "任务已更新" in sent["text"]
    updated = repo.get_task("custom-1")
    assert updated.status == "进行中"
    assert updated.priority == "Low"


def test_tasks_delete_custom_by_index(tmp_path):
    router, client, repo = _build_router(tmp_path)
    router._handle_tasks(chat_id=1, text="/tasks")
    router._handle_tasks(chat_id=1, text="/tasks delete 1")
    sent = client.messages[-1]
    assert "已删除" in sent["text"]
    assert repo.get_task("custom-1") is None


def test_tasks_grouped_by_project(tmp_path):
    router, client, _ = _build_router(tmp_path)
    router._handle_tasks(chat_id=1, text="/tasks projects 1")
    sent = client.messages[-1]
    text = sent["text"]
    assert "*按项目分组任务*" in text
    assert "实验 ｜任务:1" in text
    assert "主项目 ｜任务:1" in text
    assert text.count("  - ") == 2


def test_trackings_outputs_enumerated_list(tmp_path):
    tracker = DummyTracker(
        [
            DummyTrackingEntry(task_id="task-a", task_name="任务A"),
            DummyTrackingEntry(task_id="task-b", task_name="任务B", waiting=True),
        ]
    )
    router, client, _ = _build_router(tmp_path, tracker=tracker)
    router._handle_list_trackings(chat_id=1)
    text = client.messages[-1]["text"]
    assert "1." in text and "2." in text
    assert "等待反馈" in text
    assert router._tracking_snapshot[1] == ["task-a", "task-b"]


def test_untrack_accepts_index_from_snapshot(tmp_path):
    tracker = DummyTracker(
        [
            DummyTrackingEntry(task_id="task-a", task_name="任务A"),
            DummyTrackingEntry(task_id="task-b", task_name="任务B"),
        ]
    )
    router, client, _ = _build_router(tmp_path, tracker=tracker)
    router._handle_list_trackings(chat_id=1)
    router._handle_untrack(chat_id=1, text="/untrack 2")
    assert tracker.last_stop == "task-b"
    assert "已取消跟踪" in client.messages[-1]["text"]


def test_untrack_without_hint_prompts_when_multiple(tmp_path):
    tracker = DummyTracker(
        [
            DummyTrackingEntry(task_id="task-a", task_name="任务A"),
            DummyTrackingEntry(task_id="task-b", task_name="任务B"),
        ]
    )
    router, client, _ = _build_router(tmp_path, tracker=tracker)
    router._handle_untrack(chat_id=1, text="/untrack")
    assert "请先执行 /trackings" in client.messages[-1]["text"]
