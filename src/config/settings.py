"""
config/settings.py — application configuration as a frozen injectable dataclass.

Instantiation
─────────────
  Settings()           → all defaults, no env reads  (safe for unit tests)
  Settings.from_env()  → reads os.environ / .env     (production)

Frozen so instances are safe to share across threads and pass as
dependencies without risk of accidental mutation.

Derived properties (never exposed as env vars):
  stale_rejection_hours — min_ltf_minutes / 30 × 6 (adaptive to TF pair)
  pivot_bars            — always 1 
  tp1_multiplier        — always 0.5
  stop_buffer_pct       — always 0.00001
  ws_candle_buffer_ms   — always 1 500 ms
"""

from __future__ import annotations

import datetime
import os
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv, find_dotenv


# ── Helpers ───────────────────────────────────────────────────────────────────


def _bool_env(key: str, default: bool) -> bool:
    val = os.getenv(key)
    if val is None:
        return default
    return val.strip().lower() not in ("false", "0", "no")


def _parse_tf_max_rr(raw: str) -> dict:
    """Parse TF_MAX_RR env var.

    Accepts JSON  : '{"5/1": 3.0, "60/5": 10.0}'
    or shorthand  : '5/1:3,60/5:10'
    Returns {}    on empty / invalid input (falls through to tier table).
    """
    import json

    raw = raw.strip()
    if not raw:
        return {}
    try:
        return {k: float(v) for k, v in json.loads(raw).items()}
    except (json.JSONDecodeError, TypeError, ValueError):
        pass
    result: dict = {}
    for part in raw.split(","):
        part = part.strip()
        if ":" not in part:
            continue
        pair, cap = part.rsplit(":", 1)
        result[pair.strip()] = float(cap.strip())
    return result


def _set_env(key: str, default: set[int]) -> set[int]:
    val = os.getenv(key)
    if val is None:
        return default
    val = val.strip()
    if not val:
        return set()
    return {int(x.strip()) for x in val.split(",") if x.strip()}


def interval_to_minutes(interval: str) -> int:
    """
    Convert a timeframe string to minutes.

    Supported: "1min" "5min" "15min" "30min" "45min"
               "1h"   "2h"   "4h"
               "1day" "1week"
    """
    s = interval.strip().lower()
    if s.endswith("min"):
        return int(s[:-3])
    if s.endswith("h"):
        return int(s[:-1]) * 60
    if s.endswith("day"):
        return int(s[:-3]) * 60 * 24
    if s.endswith("week"):
        return int(s[:-4]) * 60 * 24 * 7
    raise ValueError(
        f"Unknown interval format {interval!r}. "
        f"Expected e.g. '1min','5min','15min','1h','4h','1day'."
    )


def _default_sessions() -> dict[str, dict]:
    return {
        "TOKYO": {
            "start": datetime.time(0, 0),
            "end": datetime.time(8, 0),
            "enabled": _bool_env("SESSION_TOKYO_ENABLED", False),
            "blocked_hours": _set_env("BLOCKED_HOURS_TOKYO", set()),
        },
        "LONDON": {
            "start": datetime.time(8, 0),
            "end": datetime.time(16, 0),
            "enabled": _bool_env("SESSION_LONDON_ENABLED", True),
            "blocked_hours": _set_env("BLOCKED_HOURS_LONDON", {9}),
        },
        "NEW_YORK": {
            "start": datetime.time(16, 0),
            "end": datetime.time(0, 0),
            "enabled": _bool_env("SESSION_NY_ENABLED", True),
            "blocked_hours": _set_env("BLOCKED_HOURS_NY", {17, 19}),
        },
    }


# ── Fixed constants (never in env) ────────────────────────────────────────────

_STOP_BUFFER_PCT: float = 0.00001  # 1 pip buffer — never needs tuning
_TP1_MULTIPLIER: float = 0.5  # partial close at 50% to TP2
_STOP_PLACEMENT: str = "wick"  # wick placement underperforms // swing is more consistent and easier to explain to users
_WS_CANDLE_BUFFER_MS: int = 1_500  # ms after candle close for MT5 to settle
_PIVOT_BARS: int = 1  # structural pivot strength


# ── Settings ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Settings:

    # ── Paths ─────────────────────────────────────────────────────────────────
    base_dir: Path = field(default_factory=lambda: Path.cwd())
    log_level: str = "DEBUG"
    log_dir: str = "logs"

    @property
    def charts_dir(self) -> Path:
        p = self.base_dir / "charts"
        p.mkdir(exist_ok=True)
        return p

    @property
    def session_dir(self) -> Path:
        return self.base_dir / "sessions"

    @property
    def metric_dir(self) -> Path:
        return self.base_dir / "metrics"

    # ── WebSocket ─────────────────────────────────────────────────────────────
    ws_host: str = "0.0.0.0"
    ws_port: int = 8765
    ws_secret: str = ""
    max_ws_clients: int = 10

    # ── MT5 data server ───────────────────────────────────────────────────────
    local_base_url: str = "http://localhost:8000"

    # ── Timezone ──────────────────────────────────────────────────────────────
    session_timezone: str = "UTC"

    @property
    def session_tz(self) -> ZoneInfo:
        return ZoneInfo(self.session_timezone)

    # ── Timeframes ────────────────────────────────────────────────────────────
    tf_pairs: tuple = (("1h", "5min"),)
    htf_lookback: int = 120
    htf_outputsize: int = 1000

    @property
    def htf_interval(self) -> str:
        return self.tf_pairs[0][0]

    @property
    def ltf_interval(self) -> str:
        return self.tf_pairs[0][1]

    @property
    def htf_minutes(self) -> int:
        return interval_to_minutes(self.htf_interval)

    @property
    def ltf_minutes(self) -> int:
        return interval_to_minutes(self.ltf_interval)

    def interval_to_ms(self, interval: str) -> int:
        return interval_to_minutes(interval) * 60 * 1000

    # ── Derived constants ─────────────────────────────────────────────────────

    @property
    def pivot_bars(self) -> int:
        return _PIVOT_BARS

    @property
    def stop_buffer_pct(self) -> float:
        return _STOP_BUFFER_PCT

    @property
    def stop_placement_method(self) -> str:
        return _STOP_PLACEMENT

    @property
    def tp1_multiplier(self) -> float:
        return _TP1_MULTIPLIER

    @property
    def ws_candle_buffer_ms(self) -> int:
        return _WS_CANDLE_BUFFER_MS

    @property
    def stale_rejection_hours(self) -> float:
        """6 LTF candles back from fired_at — adaptive to the TF pair."""
        min_ltf_min = min(interval_to_minutes(ltf) for _, ltf in self.tf_pairs)
        return round(min_ltf_min * 2 / 60, 4)

    def rejection_stale_hours(self, ltf_interval: str) -> float:
        """3 LTF candles back from fired_at."""
        max_ltf_min = max(interval_to_minutes(ltf_interval) for _, ltf in self.tf_pairs)
        return round(max_ltf_min * 2 / 60, 4)

    # ── Signal quality ────────────────────────────────────────────────────────
    min_wick_ratio: float = 0.65
    max_sl_zone_mult: float = 2.0
    min_rr: float = 1.5
    max_rr: float = 9.0  # 0 = disabled

    # Per-pair RR cap: "HTF_min/LTF_min" → max_rr, e.g. {"5/1": 2.5, "60/5": 8.0}
    # Falls back to max_rr when a pair has no explicit entry.
    tf_max_rr: dict = field(default_factory=dict)

    # ── Signal lifetime ───────────────────────────────────────────────────────
    signal_expiry_hours: float = 120.0

    # ── Zone limits ───────────────────────────────────────────────────────────
    max_htf_zones_per_dir: int = 1

    # ── Feature flags ─────────────────────────────────────────────────────────
    use_trend_filter: bool = True
    use_breakeven: bool = True
    use_invalidation: bool = False
    multi_tf_independent_positions: bool = True
    entry_model: str = "candle_pattern"  # candle_pattern | crt | all

    # ── Circuit breaker ───────────────────────────────────────────────────────
    max_consecutive_losses: int = 10
    pause_after_streak_h: float = 10.0

    # ── Session filter ────────────────────────────────────────────────────────
    use_session_filter: bool = True
    sessions: dict[str, dict] = field(default_factory=_default_sessions)

    # ── Validation ────────────────────────────────────────────────────────────

    def __post_init__(self) -> None:
        for htf, ltf in self.tf_pairs:
            htf_m, ltf_m = interval_to_minutes(htf), interval_to_minutes(ltf)
            if htf_m <= ltf_m:
                raise ValueError(
                    f"tf_pairs: htf ({htf}={htf_m}min) must be larger than "
                    f"ltf ({ltf}={ltf_m}min)."
                )
        valid_models = {"candle_pattern", "crt", "all"}
        if self.entry_model not in valid_models:
            raise ValueError(
                f"entry_model must be one of {valid_models}, got {self.entry_model!r}."
            )

    # ── Factory ───────────────────────────────────────────────────────────────

    @classmethod
    def from_env(cls, env_file: "Path | None" = None) -> "Settings":
        """
        Read all settings from os.environ / .env. Call once at startup.

        .env search order:
          1. explicit env_file argument (testing / Docker bind-mount)
          2. find_dotenv() — walks up from the current file toward the fs root
             so it finds .env at the project root regardless of cwd
          3. silent no-op if not found (pure-env deployments / CI)

        Loading happens here, not at module import time, so the working
        directory is irrelevant when the engine runs as an installed script.
        override=False means existing env vars always win over .env values.
        """
        if env_file is not None:
            load_dotenv(env_file, override=False)
        else:
            dotenv_path = find_dotenv(usecwd=True)
            if dotenv_path:
                load_dotenv(dotenv_path, override=False)

        raw_pairs = os.getenv("TF_PAIRS", "1h:5min")
        tf_pairs: tuple = tuple(
            tuple(p.strip().split(":"))
            for p in raw_pairs.split(",")
            if ":" in p.strip()
        ) or (("1h", "5min"),)

        return cls(
            base_dir=Path.cwd(),
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
            log_dir=os.getenv("LOG_DIR", "logs"),
            ws_host=os.getenv("WS_HOST", "0.0.0.0"),
            ws_port=int(os.getenv("WS_PORT", "8765")),
            ws_secret=os.getenv("WS_SECRET", ""),
            max_ws_clients=int(os.getenv("MAX_WS_CLIENTS", "10")),
            local_base_url=os.getenv("LOCAL_BASE_URL", "http://localhost:8000"),
            session_timezone=os.getenv("SESSION_TIMEZONE", "UTC"),
            tf_pairs=tf_pairs,
            htf_lookback=int(os.getenv("HTF_LOOKBACK", "120")),
            htf_outputsize=int(os.getenv("HTF_OUTPUTSIZE", "1000")),
            min_wick_ratio=float(os.getenv("MIN_WICK_RATIO", "0.65")),
            max_sl_zone_mult=float(os.getenv("MAX_SL_ZONE_MULT", "2.0")),
            min_rr=float(os.getenv("MIN_RR", "1.5")),
            max_rr=float(os.getenv("MAX_RR", "9.0")),
            tf_max_rr=_parse_tf_max_rr(os.getenv("TF_MAX_RR", "")),
            signal_expiry_hours=float(os.getenv("SIGNAL_EXPIRY_HOURS", "120")),
            max_htf_zones_per_dir=int(os.getenv("MAX_HTF_ZONES_PER_DIR", "3")),
            use_trend_filter=_bool_env("USE_TREND_FILTER", True),
            use_breakeven=_bool_env("USE_BREAKEVEN", True),
            use_invalidation=_bool_env("USE_INVALIDATION", False),
            multi_tf_independent_positions=_bool_env(
                "MULTI_TF_INDEPENDENT_POSITIONS", True
            ),
            entry_model=os.getenv("ENTRY_MODEL", "candle_pattern").lower(),
            max_consecutive_losses=int(os.getenv("MAX_CONSECUTIVE_LOSSES", "3")),
            pause_after_streak_h=float(os.getenv("PAUSE_AFTER_STREAK_H", "12")),
            use_session_filter=_bool_env("USE_SESSION_FILTER", True),
            sessions=_default_sessions(),
        )

    # ── Utilities ─────────────────────────────────────────────────────────────

    def now_ms(self) -> int:
        """Current time as a UTC millisecond timestamp."""
        return int(datetime.datetime.now(tz=self.session_tz).timestamp() * 1000)

    def ms_to_str(self, ms: int) -> str:
        return datetime.datetime.fromtimestamp(ms / 1000, tz=self.session_tz).strftime(
            "%Y-%m-%d %H:%M:%S"
        )

    def dt_ms(self, ts_ms: int) -> str:
        return self.ms_to_str(ts_ms)
