"""
shared/utils/time.py — timezone-agnostic time utilities.

No config imports. All functions take explicit timezone or ms values.
"""

from __future__ import annotations

import datetime
from zoneinfo import ZoneInfo


def ms_to_dt(ts_ms: int, tz: ZoneInfo) -> datetime.datetime:
    """UTC millisecond timestamp → localised datetime."""
    return datetime.datetime.fromtimestamp(ts_ms / 1000, tz=tz)


def ms_to_str(ts_ms: int, tz: ZoneInfo) -> str:
    """UTC millisecond timestamp → 'YYYY-MM-DD HH:MM:SS' string."""
    return ms_to_dt(ts_ms, tz).strftime("%Y-%m-%d %H:%M:%S")


def ms_to_utc_str(ts_ms: int) -> str:
    """UTC millisecond timestamp → UTC 'YYYY-MM-DD HH:MM:SS' string."""
    return ms_to_str(ts_ms, ZoneInfo("UTC"))


def now_ms() -> int:
    """Current UTC time as milliseconds."""
    return int(datetime.datetime.now(tz=ZoneInfo("UTC")).timestamp() * 1000)


def session_date(ts_ms: int, tz: ZoneInfo) -> datetime.date:
    """Return the session date for a timestamp in the given timezone."""
    return ms_to_dt(ts_ms, tz).date()
