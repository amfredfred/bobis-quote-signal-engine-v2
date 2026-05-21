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
  stop_buffer_pct       — always was 0.00001, now 0.00002 (2 pips) to reduce rejections and improve backtest realism
  ws_candle_buffer_ms   — always 1 500 ms
"""

from __future__ import annotations

import datetime
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dotenv import load_dotenv, find_dotenv


# ── Helpers ───────────────────────────────────────────────────────────────────


def _bool_env(key: str, default: bool) -> bool:
    val = os.getenv(key)
    if val is None:
        return default
    return val.strip().lower() not in ("false", "0", "no")


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() not in ("false", "0", "no", "off")


def _parse_pair_map(raw: Any) -> dict:
    """Parse per-timeframe mapping values.

    Accepts JSON  : '{"5/1": 3.0, "60/5": 10.0}'
    or shorthand  : '5/1:3,60/5:10'
    or YAML dict : {"5/1": 3.0, "60/5": 10.0}
    """
    import json

    if raw is None:
        return {}
    if isinstance(raw, dict):
        return {str(k): float(v) for k, v in raw.items()}
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


def _int_set(value: Any, default: set[int] | None = None) -> set[int]:
    if value is None:
        return default or set()
    if value == "":
        return set()
    if isinstance(value, (list, tuple, set)):
        return {int(x) for x in value}
    return {int(x.strip()) for x in str(value).split(",") if x.strip()}


def _load_yaml(path: Path) -> dict:
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")
    try:
        import yaml
    except ImportError as exc:
        raise RuntimeError("PyYAML is required to load YAML config files.") from exc
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"Config file root must be a mapping: {path}")
    return data


def _config_path_from_env() -> Path | None:
    raw = os.getenv("USE_CONFIG") or os.getenv("APEX_CONFIG")
    if raw:
        return Path(raw).expanduser()
    default = Path("config.yaml")
    return default if default.exists() else None


def _get(data: dict, key: str, default: Any = None) -> Any:
    cur: Any = data
    for part in key.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def _parse_tf_pairs(value: Any) -> tuple:
    if value is None:
        return (("1h", "5min"),)
    if isinstance(value, str):
        return tuple(
            tuple(p.strip().split(":"))
            for p in value.split(",")
            if ":" in p.strip()
        ) or (("1h", "5min"),)
    pairs = []
    for item in value:
        if isinstance(item, str) and ":" in item:
            pairs.append(tuple(item.strip().split(":")))
        elif isinstance(item, dict):
            pairs.append((str(item["htf"]), str(item["ltf"])))
        elif isinstance(item, (list, tuple)) and len(item) == 2:
            pairs.append((str(item[0]), str(item[1])))
    return tuple(pairs) or (("1h", "5min"),)


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
            "enabled": False,
            "blocked_hours": set(),
        },
        "LONDON": {
            "start": datetime.time(8, 0),
            "end": datetime.time(16, 0),
            "enabled": True,
            "blocked_hours": {9},
        },
        "NEW_YORK": {
            "start": datetime.time(16, 0),
            "end": datetime.time(0, 0),
            "enabled": True,
            "blocked_hours": {17, 19},
        },
    }


def _parse_time(value: Any, default: datetime.time) -> datetime.time:
    if value is None:
        return default
    if isinstance(value, datetime.time):
        return value
    text = str(value).strip()
    hour, _, minute = text.partition(":")
    return datetime.time(int(hour), int(minute or 0))


def _sessions_from_config(raw: Any) -> dict[str, dict]:
    sessions = _default_sessions()
    if not isinstance(raw, dict):
        return sessions
    for name, overrides in raw.items():
        key = str(name).upper()
        base = sessions.get(
            key,
            {
                "start": datetime.time(0, 0),
                "end": datetime.time(0, 0),
                "enabled": False,
                "blocked_hours": set(),
            },
        )
        overrides = overrides or {}
        sessions[key] = {
            "start": _parse_time(overrides.get("start"), base["start"]),
            "end": _parse_time(overrides.get("end"), base["end"]),
            "enabled": _as_bool(overrides.get("enabled"), base["enabled"]),
            "blocked_hours": _int_set(
                overrides.get("blocked_hours"), base.get("blocked_hours", set())
            ),
        }
    return sessions


# ── Fixed constants (never in env) ────────────────────────────────────────────

_STOP_BUFFER_PCT: float = 0.00001
# 1 pip buffer — never needs tuning
_TP1_MULTIPLIER: float = 0.5
# partial close at 50% to TP2
_STOP_PLACEMENT: str = "wick"
# wick placement = High RRR // swing is more consistent and easier to explain to users
_WS_CANDLE_BUFFER_MS: int = 1_500
# ms after candle close for MT5 to settle
_PIVOT_BARS: int = 1
# structural pivot strength


# ── Settings ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Settings:

    # ── Paths ─────────────────────────────────────────────────────────────────
    base_dir: Path = field(default_factory=lambda: Path.cwd())
    log_level: str = "INFO"
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

    # ── MT5 terminal ──────────────────────────────────────────────────────────
    mt5_terminal_path: str = ""
    mt5_login: int = 0
    mt5_password: str = ""
    mt5_server: str = ""
    mt5_timeout_ms: int = 60_000
    mt5_portable: bool = False

    # ── Deployment / trading guardrails ───────────────────────────────────────
    apex_env: str = "paper"
    apex_live_confirm: str = ""
    apex_disable_trading: bool = False

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

    def rejection_stale_hours(self, ltf_interval: str) -> float:
        """2 LTF candles back from fired_at."""
        return round(interval_to_minutes(ltf_interval) * 2 / 60, 4)

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

    # ── Displacement filter ───────────────────────────────────────────────────
    # Guards against ranging markets by requiring the BOS candle to show a
    # strong impulsive body.  The BOS candle body must be >= displacement_atr_mult
    # × the average body size of the prior displacement_atr_period candles.
    # Set use_displacement_filter=False to disable entirely.
    use_displacement_filter: bool = True
    displacement_atr_period: int   = 10   # lookback candles for avg body size
    displacement_atr_mult:   float = 1.2  # global fallback multiplier
    # Per-pair overrides — same format as TF_MAX_RR.
    # e.g. "60/1:0.8,60/5:0.8,30/1:1.3,30/5:1.3"
    # Falls back to displacement_atr_mult for any pair not listed.
    tf_displacement_mult: dict = field(default_factory=dict)

    def displacement_mult_for(self, htf_interval: str, ltf_interval: str) -> float:
        """Return the displacement multiplier for a specific TF pair."""
        key = f"{interval_to_minutes(htf_interval)}/{interval_to_minutes(ltf_interval)}"
        return float(self.tf_displacement_mult.get(key, self.displacement_atr_mult))

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
        if self.apex_env not in {"paper", "live"}:
            raise ValueError("apex_env must be 'paper' or 'live'.")
        if self.apex_env == "live" and self.apex_live_confirm != "YES_I_ACCEPT_RISK":
            raise ValueError(
                "APEX_ENV=live requires APEX_LIVE_CONFIRM=YES_I_ACCEPT_RISK."
            )
        if not self.ws_secret:
            import logging as _logging

            _logging.getLogger(__name__).warning(
                "WS_SECRET is not set — WebSocket server is unauthenticated. "
                "Set the WS_SECRET environment variable before deploying to production."
            )

    # ── Factory ───────────────────────────────────────────────────────────────

    @classmethod
    def from_env(cls, env_file: "Path | None" = None) -> "Settings":
        """
        Read secrets/runtime flags from os.environ / .env and engine settings
        from YAML. Call once at startup.

        .env search order:
          1. explicit env_file argument (testing / Docker bind-mount)
          2. find_dotenv() — walks up from the current file toward the fs root
             so it finds .env at the project root regardless of cwd
          3. silent no-op if not found (pure-env deployments / CI)

        Set USE_CONFIG=config.yaml or APEX_CONFIG=config.yaml to choose the YAML
        file. If neither is set, config.yaml is used when present; otherwise
        dataclass defaults are used.
        """
        if env_file is not None:
            load_dotenv(env_file, override=False)
        else:
            dotenv_path = find_dotenv(usecwd=True)
            if dotenv_path:
                load_dotenv(dotenv_path, override=False)

        config_path = _config_path_from_env()
        cfg = _load_yaml(config_path) if config_path else {}

        return cls(
            base_dir=Path.cwd(),
            log_level=str(_get(cfg, "logging.level", "INFO")).upper(),
            log_dir=str(_get(cfg, "logging.dir", "logs")),
            ws_host=str(_get(cfg, "websocket.host", "0.0.0.0")),
            ws_port=int(_get(cfg, "websocket.port", 8765)),
            ws_secret=os.getenv("WS_SECRET", ""),
            max_ws_clients=int(_get(cfg, "websocket.max_clients", 10)),
            mt5_terminal_path=str(_get(cfg, "mt5.terminal_path", "")),
            mt5_login=int(os.getenv("MT5_LOGIN", "0") or "0"),
            mt5_password=os.getenv("MT5_PASSWORD", ""),
            mt5_server=os.getenv("MT5_SERVER", ""),
            mt5_timeout_ms=int(_get(cfg, "mt5.timeout_ms", 60_000)),
            mt5_portable=_as_bool(_get(cfg, "mt5.portable", False)),
            apex_env=os.getenv("APEX_ENV", "paper").strip().lower(),
            apex_live_confirm=os.getenv("APEX_LIVE_CONFIRM", ""),
            apex_disable_trading=_bool_env("APEX_DISABLE_TRADING", False),
            tf_pairs=_parse_tf_pairs(_get(cfg, "timeframes.pairs")),
            htf_lookback=int(_get(cfg, "timeframes.htf_lookback", 120)),
            htf_outputsize=int(_get(cfg, "timeframes.htf_outputsize", 1000)),
            min_wick_ratio=float(_get(cfg, "signal_quality.min_wick_ratio", 0.65)),
            max_sl_zone_mult=float(_get(cfg, "signal_quality.max_sl_zone_mult", 2.0)),
            min_rr=float(_get(cfg, "signal_quality.min_rr", 1.5)),
            max_rr=float(_get(cfg, "signal_quality.max_rr", 9.0)),
            tf_max_rr=_parse_pair_map(_get(cfg, "signal_quality.tf_max_rr")),
            signal_expiry_hours=float(_get(cfg, "signal_lifetime.expiry_hours", 120)),
            max_htf_zones_per_dir=int(_get(cfg, "zones.max_htf_zones_per_dir", 3)),
            use_trend_filter=_as_bool(_get(cfg, "features.use_trend_filter", True)),
            use_breakeven=_as_bool(_get(cfg, "features.use_breakeven", True)),
            use_invalidation=_as_bool(_get(cfg, "features.use_invalidation", False)),
            multi_tf_independent_positions=_as_bool(
                _get(cfg, "features.multi_tf_independent_positions", True)
            ),
            entry_model=str(_get(cfg, "features.entry_model", "candle_pattern")).lower(),
            use_displacement_filter=_as_bool(
                _get(cfg, "displacement_filter.enabled", True)
            ),
            displacement_atr_period=int(_get(cfg, "displacement_filter.atr_period", 10)),
            displacement_atr_mult=float(_get(cfg, "displacement_filter.atr_mult", 1.2)),
            tf_displacement_mult=_parse_pair_map(
                _get(cfg, "displacement_filter.tf_mult")
            ),
            max_consecutive_losses=int(_get(cfg, "circuit_breaker.max_consecutive_losses", 3)),
            pause_after_streak_h=float(_get(cfg, "circuit_breaker.pause_after_streak_h", 12)),
            use_session_filter=_as_bool(_get(cfg, "session_filter.enabled", True)),
            sessions=_sessions_from_config(_get(cfg, "session_filter.sessions")),
        )

    # ── Utilities ─────────────────────────────────────────────────────────────

    def now_ms(self) -> int:
        """Current time as a UTC millisecond timestamp."""
        return int(datetime.datetime.now(tz=datetime.timezone.utc).timestamp() * 1000)

    def ms_to_str(self, ms: int) -> str:
        return datetime.datetime.fromtimestamp(ms / 1000, tz=datetime.timezone.utc).strftime(
            "%Y-%m-%d %H:%M:%S"
        )

    def dt_ms(self, ts_ms: int) -> str:
        return self.ms_to_str(ts_ms)
