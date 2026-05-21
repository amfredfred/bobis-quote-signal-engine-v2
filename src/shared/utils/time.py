"""shared/utils/time.py - UTC millisecond helpers."""

from __future__ import annotations

import datetime


def ms_to_dt(ts_ms: int) -> datetime.datetime:
    return datetime.datetime.fromtimestamp(ts_ms / 1000, tz=datetime.timezone.utc)


def ms_to_str(ts_ms: int) -> str:
    return ms_to_dt(ts_ms).strftime("%Y-%m-%d %H:%M:%S")


def now_ms() -> int:
    return int(datetime.datetime.now(tz=datetime.timezone.utc).timestamp() * 1000)


def session_date(ts_ms: int) -> datetime.date:
    return ms_to_dt(ts_ms).date()
