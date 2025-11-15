from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional


def _utcnow() -> datetime:
    return datetime.utcnow().replace(tzinfo=timezone.utc)


@dataclass
class UserState:
    action: str = "unknown"
    mental: str = "unknown"
    action_updated_at: Optional[datetime] = None
    mental_updated_at: Optional[datetime] = None
    action_prompted_at: Optional[datetime] = None
    mental_prompted_at: Optional[datetime] = None


class UserStateService:
    def __init__(self, storage_path: Path):
        self._path = storage_path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        if not self._path.exists():
            self._path.write_text("{}", encoding="utf-8")
        self._states: Dict[str, Dict[str, str]] = self._load()

    def _load(self) -> Dict[str, Dict[str, str]]:
        try:
            return json.loads(self._path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}

    def _save(self) -> None:
        self._path.write_text(json.dumps(self._states, ensure_ascii=False, indent=4), encoding="utf-8")

    def reset_all(self) -> None:
        self._states.clear()
        self._save()

    def get_state(
        self,
        chat_id: int,
        has_active_tracker: Optional[bool] = None,
        is_resting: Optional[bool] = None,
    ) -> UserState:
        raw = self._states.get(str(chat_id), {})
        state = UserState(
            action=raw.get("action", "unknown"),
            mental=raw.get("mental", "unknown"),
            action_updated_at=self._parse_dt(raw.get("action_updated_at")),
            mental_updated_at=self._parse_dt(raw.get("mental_updated_at")),
            action_prompted_at=self._parse_dt(raw.get("action_prompted_at")),
            mental_prompted_at=self._parse_dt(raw.get("mental_prompted_at")),
        )
        if has_active_tracker is not None or is_resting is not None:
            state = self._normalize_action(chat_id, state, has_active_tracker, is_resting)
        return state

    def update_state(
        self,
        chat_id: int,
        *,
        action: Optional[str] = None,
        mental: Optional[str] = None,
        has_active_tracker: Optional[bool] = None,
        is_resting: Optional[bool] = None,
    ) -> UserState:
        state = self.get_state(
            chat_id,
            has_active_tracker=has_active_tracker,
            is_resting=is_resting,
        )
        now = _utcnow()
        changed = False
        if action:
            desired = action
            if is_resting:
                desired = "休息中"
            state.action = desired
            state.action_updated_at = now
            state.action_prompted_at = None
            changed = True
        if mental:
            state.mental = mental
            state.mental_updated_at = now
            state.mental_prompted_at = None
            changed = True
        if has_active_tracker is not None or is_resting is not None:
            state = self._normalize_action(
                chat_id,
                state,
                has_active_tracker,
                is_resting,
            )
        if changed:
            self._persist(chat_id, state)
        return state

    def mark_prompt(self, chat_id: int, *, action: bool = False, mental: bool = False) -> None:
        state = self.get_state(chat_id)
        now = _utcnow()
        if action:
            state.action_prompted_at = now
        if mental:
            state.mental_prompted_at = now
        self._persist(chat_id, state)

    def _persist(self, chat_id: int, state: UserState) -> None:
        self._states[str(chat_id)] = {
            "action": state.action,
            "mental": state.mental,
            "action_updated_at": self._dump_dt(state.action_updated_at),
            "mental_updated_at": self._dump_dt(state.mental_updated_at),
            "action_prompted_at": self._dump_dt(state.action_prompted_at),
            "mental_prompted_at": self._dump_dt(state.mental_prompted_at),
        }
        self._save()

    def _normalize_action(
        self,
        chat_id: int,
        state: UserState,
        has_active_tracker: Optional[bool],
        is_resting: Optional[bool],
    ) -> UserState:
        changed = False
        now = _utcnow()
        if is_resting:
            if state.action != "休息中":
                state.action = "休息中"
                state.action_updated_at = now
                state.action_prompted_at = None
                changed = True
        else:
            if state.action == "休息中":
                if has_active_tracker:
                    state.action = "推进中"
                    state.action_updated_at = now
                    state.action_prompted_at = None
                    changed = True
                else:
                    state.action = "unknown"
                    state.action_updated_at = now
                    state.action_prompted_at = None
                    changed = True
            elif state.action == "推进中" and not has_active_tracker:
                state.action = "unknown"
                state.action_updated_at = None
                state.action_prompted_at = None
                changed = True
            elif has_active_tracker and state.action == "unknown":
                state.action = "推进中"
                state.action_updated_at = now
                state.action_prompted_at = None
                changed = True
        if changed:
            self._persist(chat_id, state)
        return state

    @staticmethod
    def _parse_dt(value: Optional[str]) -> Optional[datetime]:
        if not value:
            return None
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None

    @staticmethod
    def _dump_dt(value: Optional[datetime]) -> Optional[str]:
        return value.isoformat() if value else None
