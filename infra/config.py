from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Tuple

try:  # pragma: no cover - shim for Python < 3.11
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib  # type: ignore[no-redef]


@dataclass(frozen=True)
class TelegramSettings:
    token: str
    poll_timeout: int
    admin_ids: Tuple[int, ...]


@dataclass(frozen=True)
class PathsSettings:
    data_dir: Path
    raw_json_dir: Path
    processed_dir: Path
    history_dir: Path
    database_ids_path: Path


@dataclass(frozen=True)
class NotionSettings:
    api_key: str
    database_ids: Dict[str, str]
    sync_interval: int
    force_update: bool


@dataclass(frozen=True)
class LLMSettings:
    provider: str
    base_url: str
    model: str
    api_key: str | None
    temperature: float
    enabled: bool


@dataclass(frozen=True)
class WeComSettings:
    webhook_url: str


@dataclass(frozen=True)
class ProactivitySettings:
    state_check_seconds: int
    state_stale_seconds: int
    state_prompt_cooldown_seconds: int
    question_follow_up_seconds: int
    state_unknown_retry_seconds: int


@dataclass(frozen=True)
class Settings:
    telegram: TelegramSettings | None
    paths: PathsSettings
    notion: NotionSettings
    llm: LLMSettings | None
    wecom: WeComSettings | None
    tracker_interval: int
    tracker_follow_up: int
    proactivity: ProactivitySettings


def _default_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _load_toml(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    with open(path, "rb") as fp:
        return tomllib.load(fp)


def _ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def _load_database_ids(path: Path) -> Dict[str, str]:
    if path.exists():
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def load_settings(
    force_update: bool | None = None,
    require_telegram: bool = True,
    config_path: Path | None = None,
) -> Settings:
    root = _default_root()
    config_file = (
        config_path
        or Path(os.getenv("SECRETARY_CONFIG", root / "config" / "settings.toml"))
    ).resolve()
    config = _load_toml(config_file)

    paths_cfg = config.get("paths", {})
    data_dir = Path(
        paths_cfg.get("data_dir")
        or os.getenv("DATA_DIR")
        or root / "databases"
    ).resolve()
    raw_dir = _ensure_dir(data_dir / "raw_json")
    processed_dir = _ensure_dir(data_dir / "json")
    history_dir = _ensure_dir(data_dir / "telegram_history")
    database_ids_path = Path(
        paths_cfg.get("database_ids_path")
        or os.getenv("NOTION_DATABASE_IDS_PATH")
        or root / "database_ids.json"
    ).resolve()

    telegram_cfg = config.get("telegram", {})
    telegram_token = (
        telegram_cfg.get("token") or os.getenv("TELEGRAM_BOT_TOKEN", "")
    ).strip()
    admin_ids_value = telegram_cfg.get("admin_ids")
    if isinstance(admin_ids_value, str):
        admin_ids_value = [admin_ids_value]
    if admin_ids_value is None:
        env_admin_ids = os.getenv("TELEGRAM_ADMIN_IDS", "")
        admin_ids_value = [
            token.strip() for token in env_admin_ids.split(",") if token.strip()
        ]
    admin_ids: Tuple[int, ...] = tuple(
        int(token) for token in admin_ids_value if str(token).isdigit()
    )
    poll_timeout = int(
        telegram_cfg.get("poll_timeout")
        or os.getenv("TELEGRAM_POLL_TIMEOUT", "25")
    )
    telegram_settings: TelegramSettings | None = None
    if telegram_token:
        telegram_settings = TelegramSettings(
            token=telegram_token,
            poll_timeout=poll_timeout,
            admin_ids=admin_ids,
        )
    elif require_telegram:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not configured.")

    notion_cfg = config.get("notion", {})
    notion_api_key = (
        notion_cfg.get("api_key") or os.getenv("NOTION_API_KEY", "")
    ).strip()
    if not notion_api_key:
        raise RuntimeError("NOTION_API_KEY is not configured.")
    database_ids = _load_database_ids(database_ids_path)
    sync_interval = int(
        notion_cfg.get("sync_interval")
        or os.getenv("NOTION_SYNC_INTERVAL", "1800")
    )
    force_flag = (
        notion_cfg.get("force_update")
        if notion_cfg.get("force_update") is not None
        else bool(force_update)
    )

    llm_cfg = config.get("llm", {})
    provider = llm_cfg.get("provider") or os.getenv("LLM_PROVIDER", "openai")
    base_url = llm_cfg.get("base_url") or os.getenv(
        "LLM_BASE_URL", "https://api.openai.com/v1"
    )
    model = llm_cfg.get("model") or os.getenv("LLM_MODEL", "gpt-4o-mini")
    api_key = (
        llm_cfg.get("api_key")
        or os.getenv("LLM_API_KEY")
        or os.getenv("OPENAI_API_KEY")
    )
    temperature = float(
        llm_cfg.get("temperature")
        or os.getenv("LLM_TEMPERATURE", "0.3")
    )
    llm_settings = LLMSettings(
        provider=provider,
        base_url=base_url,
        model=model,
        api_key=api_key.strip() if isinstance(api_key, str) else None,
        temperature=temperature,
        enabled=bool(api_key),
    )

    tracker_cfg = config.get("tracker", {})
    tracker_interval = int(
        tracker_cfg.get("interval_seconds")
        or os.getenv("TRACKER_INTERVAL_SECONDS", "1500")
    )
    tracker_follow_up = int(
        tracker_cfg.get("follow_up_seconds")
        or os.getenv("TRACKER_FOLLOW_UP_SECONDS", "600")
    )

    wecom_cfg = config.get("wecom", {})
    webhook_url = (
        wecom_cfg.get("webhook_url") or os.getenv("WECOM_WEBHOOK_URL", "")
    ).strip()
    wecom_settings = WeComSettings(webhook_url=webhook_url) if webhook_url else None

    proactivity_cfg = config.get("proactivity", {})
    proactivity_settings = ProactivitySettings(
        state_check_seconds=int(
            proactivity_cfg.get("state_check_seconds")
            or os.getenv("PROACTIVITY_STATE_CHECK_SECONDS", "300")
        ),
        state_stale_seconds=int(
            proactivity_cfg.get("state_stale_seconds")
            or os.getenv("PROACTIVITY_STATE_STALE_SECONDS", "3600")
        ),
        state_prompt_cooldown_seconds=int(
            proactivity_cfg.get("state_prompt_cooldown_seconds")
            or os.getenv("PROACTIVITY_STATE_PROMPT_COOLDOWN_SECONDS", "600")
        ),
        question_follow_up_seconds=int(
            proactivity_cfg.get("question_follow_up_seconds")
            or os.getenv("PROACTIVITY_QUESTION_FOLLOW_UP_SECONDS", "600")
        ),
        state_unknown_retry_seconds=int(
            proactivity_cfg.get("state_unknown_retry_seconds")
            or os.getenv("PROACTIVITY_STATE_UNKNOWN_RETRY_SECONDS", "120")
        ),
    )

    settings = Settings(
        telegram=telegram_settings,
        paths=PathsSettings(
            data_dir=data_dir,
            raw_json_dir=raw_dir,
            processed_dir=processed_dir,
            history_dir=history_dir,
            database_ids_path=database_ids_path,
        ),
        notion=NotionSettings(
            api_key=notion_api_key,
            database_ids=database_ids,
            sync_interval=sync_interval,
            force_update=bool(force_flag),
        ),
        llm=llm_settings if llm_settings.api_key else None,
        wecom=wecom_settings,
        tracker_interval=tracker_interval,
        tracker_follow_up=tracker_follow_up,
        proactivity=proactivity_settings,
    )
    return settings
