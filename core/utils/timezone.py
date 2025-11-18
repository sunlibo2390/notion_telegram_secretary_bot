from __future__ import annotations

from datetime import datetime, timedelta, timezone

CURRENT_TZ = timezone(timedelta(hours=8))


def configure_timezone(offset_hours: int) -> None:
    global CURRENT_TZ
    CURRENT_TZ = timezone(timedelta(hours=offset_hours))


def to_local(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=CURRENT_TZ)
    return value.astimezone(CURRENT_TZ)


def format_local(value: datetime, fmt: str = "%Y-%m-%d %H:%M") -> str:
    return to_local(value).strftime(fmt)


def local_now() -> datetime:
    return datetime.now(tz=CURRENT_TZ)


# Backward compatibility aliases
to_beijing = to_local
format_beijing = format_local
beijing_now = local_now
