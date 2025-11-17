from __future__ import annotations

import os
from pathlib import Path
from typing import Optional, Tuple


_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_BASE = Path(os.getenv("DATA_DIR", _PROJECT_ROOT / "databases")).resolve()


def _prepare_directories(base_dir: Path) -> Tuple[Path, Path, Path, Path]:
    raw_dir = base_dir / "raw_json"
    processed_dir = base_dir / "json"
    history_dir = base_dir / "telegram_history"
    for directory in (raw_dir, processed_dir, history_dir):
        directory.mkdir(parents=True, exist_ok=True)
    return base_dir, raw_dir, processed_dir, history_dir


DATA_DIR, RAW_JSON_DIR, PROCESSED_DIR, TELEGRAM_HISTORY_DIR = _prepare_directories(
    _DEFAULT_BASE
)


def configure(base_dir: Path | str) -> None:
    """
    Override the base data directory at runtime so repositories and processors
    share the same location regardless of import order.
    """
    global DATA_DIR, RAW_JSON_DIR, PROCESSED_DIR, TELEGRAM_HISTORY_DIR
    base = Path(base_dir).resolve()
    DATA_DIR, RAW_JSON_DIR, PROCESSED_DIR, TELEGRAM_HISTORY_DIR = _prepare_directories(
        base
    )


def raw_json_path(name: str, suffix: str = ".json") -> Path:
    filename = f"{name}{suffix}" if not name.endswith(suffix) else name
    return RAW_JSON_DIR / filename


def processed_json_path(name: str, suffix: str = ".json") -> Path:
    filename = f"{name}{suffix}" if not name.endswith(suffix) else name
    return PROCESSED_DIR / filename


def history_path(chat_id: Optional[int] = None) -> Path:
    if chat_id is None:
        return TELEGRAM_HISTORY_DIR
    return TELEGRAM_HISTORY_DIR / f"{chat_id}.jsonl"


def metadata_path() -> Path:
    return TELEGRAM_HISTORY_DIR / "metadata.json"
