from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List, Optional
from uuid import uuid4

from dataclasses import asdict

from core.domain import Task
from data_pipeline.storage import paths


class TaskRepository:
    def __init__(
        self,
        processed_path: Path | None = None,
        custom_path: Path | None = None,
    ):
        self._primary_path = processed_path or paths.processed_json_path("processed_tasks")
        self._custom_path = custom_path or paths.processed_json_path("agent_tasks")
        self._primary_cache: Dict[str, Task] = {}
        self._custom_cache: Dict[str, Task] = {}
        self._primary_loaded = False
        self._custom_loaded = False

    @staticmethod
    def _read_json(path: Path) -> Dict[str, Dict]:
        try:
            with path.open("r", encoding="utf-8") as file:
                return json.load(file)
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _load_primary(self) -> None:
        if self._primary_loaded:
            return
        self._primary_cache = {}
        if self._primary_path.exists():
            raw = self._read_json(self._primary_path)
            for task_id, payload in raw.items():
                payload = self._normalize_payload(task_id, payload, is_custom=False)
                self._primary_cache[task_id] = Task(id=task_id, **payload)
        self._primary_loaded = True

    def _load_custom(self) -> None:
        if self._custom_loaded:
            return
        self._custom_cache = {}
        if self._custom_path.exists():
            raw = self._read_json(self._custom_path)
            for task_id, payload in raw.items():
                payload = self._normalize_payload(task_id, payload, is_custom=True)
                self._custom_cache[task_id] = Task(id=task_id, **payload)
        else:
            self._custom_path.parent.mkdir(parents=True, exist_ok=True)
            self._custom_path.write_text("{}", encoding="utf-8")
        self._custom_loaded = True

    def _save_custom(self) -> None:
        if not self._custom_loaded:
            return
        with open(self._custom_path, "w", encoding="utf-8") as file:
            payload = {}
            for task in self._custom_cache.values():
                data = asdict(task)
                data.pop("id", None)
                payload[task.id] = data
            json.dump(
                payload,
                file,
                ensure_ascii=False,
                indent=4,
            )

    @staticmethod
    def _normalize_payload(task_id: str, payload: dict, is_custom: bool) -> dict:
        payload = dict(payload)
        if "due_data" in payload and "due_date" not in payload:
            payload["due_date"] = payload.pop("due_data")
        payload.setdefault("subtask_names", [])
        payload.setdefault("content", "")
        payload.setdefault("priority", "Medium")
        payload.setdefault("status", "Undecomposed")
        payload.setdefault("project_name", "")
        payload.setdefault("project_id", None)
        payload.setdefault("due_date", None)
        if not payload.get("page_url") and not is_custom:
            payload["page_url"] = f"https://www.notion.so/{task_id.replace('-', '')}"
        elif is_custom and "page_url" not in payload:
            payload["page_url"] = None
        return payload

    def refresh(self) -> None:
        self._primary_loaded = False
        self._custom_loaded = False
        self._primary_cache.clear()
        self._custom_cache.clear()

    def list_active_tasks(self) -> List[Task]:
        self._load_primary()
        self._load_custom()
        return list(self._primary_cache.values()) + list(self._custom_cache.values())

    def get_task(self, task_id: str) -> Optional[Task]:
        self._load_primary()
        if task_id in self._primary_cache:
            return self._primary_cache[task_id]
        self._load_custom()
        return self._custom_cache.get(task_id)

    def find_by_name(self, name: str) -> Optional[Task]:
        if not name:
            return None
        lowered = name.lower()
        for cache_loader, cache in (
            (self._load_primary, self._primary_cache),
            (self._load_custom, self._custom_cache),
        ):
            cache_loader()
            exact = next((task for task in cache.values() if task.name.lower() == lowered), None)
            if exact:
                return exact
            contains = next((task for task in cache.values() if lowered in task.name.lower()), None)
            if contains:
                return contains
        return None

    def ensure_task(self, name: str, content: str = "") -> Task:
        existing = self.find_by_name(name)
        if existing:
            return existing
        return self.create_custom_task(name=name, content=content)

    def create_custom_task(
        self,
        name: str,
        content: str = "",
        priority: str = "Medium",
        status: str = "Undecomposed",
        project_name: str = "",
        due_date: Optional[str] = None,
    ) -> Task:
        self._load_custom()
        task_id = str(uuid4())
        payload = {
            "name": name,
            "priority": priority,
            "status": status,
            "content": content,
            "project_id": None,
            "project_name": project_name,
            "due_date": due_date,
            "subtask_names": [],
            "page_url": None,
        }
        task = Task(id=task_id, **payload)
        self._custom_cache[task_id] = task
        self._save_custom()
        return task

    def update_custom_task(
        self,
        task_id: str,
        *,
        name: Optional[str] = None,
        content: Optional[str] = None,
        status: Optional[str] = None,
        priority: Optional[str] = None,
        due_date: Optional[str] = None,
        project_name: Optional[str] = None,
    ) -> Optional[Task]:
        self._load_custom()
        task = self._custom_cache.get(task_id)
        if not task:
            return None
        if name:
            task.name = name
        if content is not None:
            task.content = content
        if status:
            task.status = status
        if priority:
            task.priority = priority
        if due_date is not None:
            task.due_date = due_date
        if project_name is not None:
            task.project_name = project_name
        self._custom_cache[task_id] = task
        self._save_custom()
        return task

    def delete_custom_task(self, task_id: str) -> bool:
        self._load_custom()
        if task_id not in self._custom_cache:
            return False
        self._custom_cache.pop(task_id, None)
        self._save_custom()
        return True

    def is_custom_task(self, task_id: str) -> bool:
        self._load_custom()
        return task_id in self._custom_cache
