from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, List, Optional
from uuid import uuid4


def _utcnow() -> datetime:
    return datetime.utcnow().replace(tzinfo=timezone.utc)


def _parse_dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _dump_dt(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


@dataclass
class RestWindow:
    id: str
    chat_id: int
    start: datetime
    end: datetime
    status: str  # pending | approved | cancelled
    note: str
    created_at: datetime
    session_type: str = "rest"
    task_id: Optional[str] = None
    task_name: Optional[str] = None


class RestScheduleService:
    def __init__(self, storage_path: Path):
        self._path = storage_path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        if not self._path.exists():
            self._path.write_text("{}", encoding="utf-8")
        self._data: Dict[str, Dict] = self._load()
        self._recent_cancelled: Dict[int, datetime] = {}
        self._rest_types = {"rest"}

    def _load(self) -> Dict[str, Dict]:
        try:
            return json.loads(self._path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}

    def _save(self) -> None:
        self._path.write_text(json.dumps(self._data, ensure_ascii=False, indent=4), encoding="utf-8")

    def _prune_expired(self) -> None:
        now = _utcnow()
        removed = False
        for window_id in list(self._data.keys()):
            payload = self._data[window_id]
            end = _parse_dt(payload["end"])
            if end <= now and payload.get("session_type", "rest") in self._rest_types:
                self._data.pop(window_id)
                removed = True
        if removed:
            self._save()
        cutoff = now - timedelta(minutes=30)
        for chat_id in list(self._recent_cancelled.keys()):
            if self._recent_cancelled[chat_id] < cutoff:
                self._recent_cancelled.pop(chat_id, None)

    def list_windows(self, chat_id: int, include_past: bool = False) -> List[RestWindow]:
        self._prune_expired()
        now = _utcnow()
        result: List[RestWindow] = []
        for payload in self._data.values():
            if payload["chat_id"] != chat_id:
                continue
            if payload.get("status") in {"cancelled", "rejected"}:
                continue
            window = self._hydrate(payload)
            if include_past or window.end >= now:
                result.append(window)
        result.sort(key=lambda item: item.start)
        return result

    def iter_windows(self, include_past: bool = False) -> List[RestWindow]:
        self._prune_expired()
        now = _utcnow()
        windows: List[RestWindow] = []
        for payload in self._data.values():
            window = self._hydrate(payload)
            if include_past or window.end >= now:
                windows.append(window)
        windows.sort(key=lambda item: (item.chat_id, item.start))
        return windows

    def add_window(
        self,
        chat_id: int,
        start: datetime,
        end: datetime,
        note: str = "",
        status: str = "approved",
        session_type: str = "rest",
        task_id: Optional[str] = None,
        task_name: Optional[str] = None,
    ) -> RestWindow:
        self._prune_expired()
        if end <= start:
            raise ValueError("end must be after start")
        notes = [note.strip()] if note.strip() else []
        normalized_type = session_type or "rest"
        if normalized_type == "rest":
            # Merge overlapping rest windows to keep a single continuous block.
            for window_id, payload in list(self._data.items()):
                if payload["chat_id"] != chat_id:
                    continue
                if payload.get("status") in {"cancelled", "rejected"}:
                    continue
                if payload.get("session_type", "rest") != "rest":
                    continue
                existing_start = _parse_dt(payload["start"])
                existing_end = _parse_dt(payload["end"])
                overlaps = not (end <= existing_start or start >= existing_end)
                touches = end == existing_start or start == existing_end
                if overlaps or touches:
                    start = min(start, existing_start)
                    end = max(end, existing_end)
                    existing_note = payload.get("note", "").strip()
                    if existing_note:
                        notes.append(existing_note)
                    self._data.pop(window_id, None)
        window_id = str(uuid4())
        combined_note = "；".join(dict.fromkeys(notes)) if notes else ""
        payload = {
            "id": window_id,
            "chat_id": chat_id,
            "start": _dump_dt(start),
            "end": _dump_dt(end),
            "status": status,
            "note": combined_note,
            "created_at": _dump_dt(_utcnow()),
            "session_type": normalized_type,
            "task_id": task_id,
            "task_name": task_name,
        }
        self._data[window_id] = payload
        self._save()
        return self._hydrate(payload)

    def cancel_window(self, window_id: str) -> bool:
        self._prune_expired()
        payload = self._data.get(window_id)
        if not payload:
            return False
        now = _utcnow()
        start = _parse_dt(payload["start"])
        end = _parse_dt(payload["end"])
        active = payload.get("status") == "approved" and start <= now <= end
        self._data.pop(window_id, None)
        self._save()
        if active:
            self._recent_cancelled[payload["chat_id"]] = now
        return True

    def delete_window(self, window_id: str) -> bool:
        self._prune_expired()
        if window_id in self._data:
            self._data.pop(window_id)
            self._save()
            return True
        return False

    def get_window(self, window_id: str) -> Optional[RestWindow]:
        self._prune_expired()
        payload = self._data.get(window_id)
        return self._hydrate(payload) if payload else None

    def is_resting(self, chat_id: int, when: Optional[datetime] = None) -> bool:
        window = self.current_window(chat_id, when, session_type="rest")
        return window is not None

    def has_active_task_block(self, chat_id: int, when: Optional[datetime] = None) -> bool:
        window = self.current_window(chat_id, when, session_type="task")
        return window is not None

    def current_window(
        self,
        chat_id: int,
        when: Optional[datetime] = None,
        session_type: str = "rest",
    ) -> Optional[RestWindow]:
        self._prune_expired()
        when = when or _utcnow()
        for payload in self._data.values():
            if payload["chat_id"] != chat_id:
                continue
            if payload.get("status") != "approved":
                continue
            if payload.get("session_type", "rest") != session_type:
                continue
            start = _parse_dt(payload["start"])
            end = _parse_dt(payload["end"])
            if start <= when <= end:
                return RestWindow(
                    id=payload["id"],
                    chat_id=chat_id,
                    start=start,
                    end=end,
                    status=payload["status"],
                    note=payload.get("note", ""),
                    created_at=_parse_dt(payload["created_at"]),
                    session_type=payload.get("session_type", "rest"),
                    task_id=payload.get("task_id"),
                    task_name=payload.get("task_name"),
                )
        return None

    def next_resume_time(self, chat_id: int, when: Optional[datetime] = None) -> Optional[datetime]:
        self._prune_expired()
        when = when or _utcnow()
        future_ends = [
            _parse_dt(payload["end"])
            for payload in self._data.values()
            if payload["chat_id"] == chat_id
            and payload.get("status") == "approved"
            and payload.get("session_type", "rest") == "rest"
            and _parse_dt(payload["end"]) > when
        ]
        return min(future_ends) if future_ends else None

    def next_window(self, chat_id: int) -> Optional[RestWindow]:
        windows = [
            window
            for window in self.list_windows(chat_id, include_past=False)
            if window.session_type == "rest"
        ]
        return windows[0] if windows else None

    def recent_cancelled_at(self, chat_id: int) -> Optional[datetime]:
        self._prune_expired()
        return self._recent_cancelled.get(chat_id)

    def _hydrate(self, payload: Dict) -> RestWindow:
        return RestWindow(
            id=payload["id"],
            chat_id=payload["chat_id"],
            start=_parse_dt(payload["start"]),
            end=_parse_dt(payload["end"]),
            status=payload.get("status", "approved"),
            note=payload.get("note", ""),
            created_at=_parse_dt(payload["created_at"]),
            session_type=payload.get("session_type", "rest"),
            task_id=payload.get("task_id"),
            task_name=payload.get("task_name"),
        )
