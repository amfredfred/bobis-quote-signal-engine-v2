"""
domain/assets/profiles.py — per-symbol signal quality profiles.

Architecture
────────────
  AssetProfile    — frozen dataclass (replaces the TypedDict).
                    All callers get type safety and dot-access.
  AssetRegistry   — builds a profile from a Config + class/symbol overrides.
                    Injected into signal builders; nothing imports _cfg here.

Adding a new symbol:  one line in ASSET_CLASS_MAP.
Tuning a class:       edit _CLASS_OVERRIDES.
Tuning one symbol:    add to SYMBOL_OVERRIDES with only the keys to change.

Symbol normalisation
────────────────────
  All public functions accept any of: "EURUSD" "EUR/USD" "eurusd" "eur/usd"
  _normalize() strips "/" and uppercases, then maps to the canonical form.

Session definitions
────────────────────
  Hardcoded per asset class — backed by backtest evidence (May 2025–Mar 2026).
  Global Config.sessions is the fallback for symbols not in ASSET_CLASS_MAP.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from typing import Any, Optional, Protocol


# ── Config protocol (breaks the circular import) ──────────────────────────────
# domain/ must not import config/. We declare the minimal interface we need.


class _ConfigProtocol(Protocol):
    min_rr: float
    max_rr: float
    use_session_filter: bool
    sessions: dict
    stop_placement_method: str
    stop_buffer_pct: float
    max_sl_zone_mult: float
    tp1_trigger_pct: float
    tp1_close_pct: float
    move_sl_to_be_on_tp1: bool
    breakeven_spread_multiplier: float
    breakeven_max_buffer_pct_of_risk: float
    trade_management_tf_overrides: dict
    use_invalidation: bool
    signal_expiry_hours: float
    use_trend_filter: bool
    htf_lookback: int
    multi_tf_independent_positions: bool
    tf_max_rr: dict


# ── Asset profile ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class AssetProfile:
    """
    Fully resolved signal-quality parameters for one symbol.

    Frozen so it can be cached and passed across threads safely.
    Dot-access replaces the previous TypedDict["key"] pattern.
    """

    # Signal quality gates
    min_rr: float
    max_rr: float
    use_session_filter: bool
    sessions: dict

    # SL / TP
    stop_placement: str
    stop_buffer_pct: float
    max_sl_zone_mult: float
    tp1_trigger_pct: float
    tp1_close_pct: float

    # Trade management
    move_sl_to_be_on_tp1: bool
    use_invalidation: bool
    signal_expiry_hours: float

    # Structure detection
    use_trend_filter: bool
    htf_lookback: int

    # Multi-TF
    multi_tf_independent_positions: bool

    # Protected breakeven. Spread is injected by backtests; live signal
    # lifecycle falls back to entry breakeven because candles have no quote spread.
    breakeven_spread_price_units: float = 0.0
    breakeven_spread_multiplier: float = 1.5
    breakeven_max_buffer_pct_of_risk: float = 10.0


# ── Symbol → asset class ──────────────────────────────────────────────────────

ASSET_CLASS_MAP: dict[str, str] = {
    "XAUUSD": "COMMODITY",
    "US100": "INDICES",
    "US500": "INDICES",
}
SUPPORTED_SYMBOLS = frozenset(ASSET_CLASS_MAP)

# Built once at import — maps stripped form back to canonical:
#   "EURUSD" → "EUR/USD",  "XAUUSD" → "XAU/USD",  "US500" → "US500"
_STRIP_MAP: dict[str, str] = {k.replace("/", ""): k for k in ASSET_CLASS_MAP}


def normalize_symbol(symbol: str) -> str:
    """
    Accept any reasonable symbol format and return the canonical form.

    "EURUSD" | "EUR/USD" | "eurusd" | "eur/usd"  →  "EUR/USD"
    "XAUUSD" | "XAU/USD"                          →  "XAU/USD"
    "US500"                                         →  "US500"
    """
    return _STRIP_MAP.get(symbol.upper().replace("/", ""), symbol.upper())


def get_asset_class(symbol: str) -> str:
    return ASSET_CLASS_MAP.get(normalize_symbol(symbol), "FOREX")


# ── Session definitions per asset class ───────────────────────────────────────

_SESSIONS_FOREX: dict[str, dict] = {
    "LONDON": {
        "start": datetime.time(8, 0),
        "end": datetime.time(16, 0),
        "enabled": True,
        "blocked_hours": {9},  # 70.4% loss rate in backtest
    },
    "NEW_YORK": {
        "start": datetime.time(16, 0),
        "end": datetime.time(0, 0),
        "enabled": True,
        "blocked_hours": {17, 19},  # NY open chop + evening dead zone
    },
}

_SESSIONS_COMMODITY: dict[str, dict] = {
    "TOKYO": {
        "start": datetime.time(0, 0),
        "end": datetime.time(8, 0),
        "enabled": True,  # gold active in Asian hours
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

_SESSIONS_INDICES: dict[str, dict] = {
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

_CLASS_SESSIONS: dict[str, dict] = {
    "FOREX": _SESSIONS_FOREX,
    "COMMODITY": _SESSIONS_COMMODITY,
    "INDICES": _SESSIONS_INDICES,
}

# Per-class and per-symbol overrides (only keys that differ from Config defaults)
_CLASS_OVERRIDES: dict[str, dict] = {
    "FOREX": {
        "max_rr": 2.5,
    },
    "COMMODITY": {
        "max_rr": 8.0,
    },
    "INDICES": {
        "max_rr": 8.0,
    },
    "CRYPTO": {
        "max_rr": 8.0,
        "stop_buffer_pct": 0.02,
    },
}
SYMBOL_OVERRIDES: dict[str, dict] = {}


# ── Session filter ────────────────────────────────────────────────────────────


def in_session(profile: AssetProfile, ts_ms: int) -> bool:
    """
    Return True if ts_ms falls inside an enabled, non-blocked session window.

    Rules (in order):
      1. use_session_filter == False  → always True
      2. For each enabled session:
           a. Skip if UTC hour is in blocked_hours
           b. Accept if hour falls within start/end window
      3. No session accepted → False
    """
    if not profile.use_session_filter:
        return True

    import datetime as _dt

    dt = _dt.datetime.fromtimestamp(ts_ms / 1000, tz=_dt.timezone.utc)
    hour = dt.hour

    for _name, s in profile.sessions.items():
        if not s["enabled"]:
            continue
        if hour in s.get("blocked_hours", set()):
            continue
        sh = s["start"].hour
        eh = s["end"].hour
        # Sessions ending at midnight: end==00:00 → treat as 24:00
        in_window = (hour >= sh) if eh == 0 else (sh <= hour < eh)
        if in_window:
            return True

    return False


# ── Registry ──────────────────────────────────────────────────────────────────


class AssetRegistry:
    """
    Builds AssetProfile instances from a Config and optional overrides.

    Instantiate once with the application Config and inject into any
    component that needs per-symbol parameters — no module-level _cfg.

    Resolution order:
      1. Config instance  — all global defaults
      2. _CLASS_OVERRIDES — class-level adjustments
      3. SYMBOL_OVERRIDES — symbol-level fine-tuning
      4. Class sessions   — replace Config.sessions with asset-class sessions
    """

    def __init__(self, cfg: _ConfigProtocol) -> None:
        self._cfg = cfg

    def get(
        self,
        symbol: str,
        htf_interval: Optional[str] = None,
        ltf_interval: Optional[str] = None,
    ) -> AssetProfile:
        symbol = normalize_symbol(symbol)
        cfg = self._cfg

        # 1. Base from Config
        base: dict[str, Any] = {
            "min_rr": cfg.min_rr,
            "max_rr": cfg.max_rr,
            "use_session_filter": cfg.use_session_filter,
            "sessions": cfg.sessions,
            "stop_placement": cfg.stop_placement_method,
            "stop_buffer_pct": cfg.stop_buffer_pct,
            "max_sl_zone_mult": cfg.max_sl_zone_mult,
            "tp1_trigger_pct": cfg.tp1_trigger_pct,
            "tp1_close_pct": cfg.tp1_close_pct,
            "move_sl_to_be_on_tp1": cfg.move_sl_to_be_on_tp1,
            "breakeven_spread_multiplier": cfg.breakeven_spread_multiplier,
            "breakeven_max_buffer_pct_of_risk": cfg.breakeven_max_buffer_pct_of_risk,
            "use_invalidation": cfg.use_invalidation,
            "signal_expiry_hours": cfg.signal_expiry_hours,
            "use_trend_filter": cfg.use_trend_filter,
            "htf_lookback": cfg.htf_lookback,
            "multi_tf_independent_positions": cfg.multi_tf_independent_positions,
        }

        # 2 + 3. Class / symbol overrides
        class_key = ASSET_CLASS_MAP.get(symbol, "FOREX")
        base.update(_CLASS_OVERRIDES.get(class_key, {}))
        base.update(SYMBOL_OVERRIDES.get(symbol, {}))

        # 4. Replace generic sessions with asset-class specific sessions
        base["sessions"] = _CLASS_SESSIONS.get(class_key, cfg.sessions)

        # 5. Timeframe-aware max_rr and TP management.
        if htf_interval and ltf_interval:
            base["max_rr"] = self._resolve_tf_max_rr(
                htf_interval, ltf_interval, base["max_rr"]
            )
            base.update(
                self._resolve_tf_trade_management(htf_interval, ltf_interval)
            )

        return AssetProfile(**base)

    def _resolve_tf_max_rr(
        self, htf_interval: str, ltf_interval: str, fallback: float
    ) -> float:
        from config.settings import interval_to_minutes

        key = f"{interval_to_minutes(htf_interval)}/{interval_to_minutes(ltf_interval)}"
        return float(self._cfg.tf_max_rr.get(key, fallback))

    def _resolve_tf_trade_management(
        self, htf_interval: str, ltf_interval: str
    ) -> dict[str, Any]:
        from config.settings import interval_to_minutes

        key = f"{interval_to_minutes(htf_interval)}/{interval_to_minutes(ltf_interval)}"
        return dict(self._cfg.trade_management_tf_overrides.get(key, {}))
