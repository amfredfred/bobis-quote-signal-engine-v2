"""
domain/signals/builder.py — construct a TradeSignal from its structural inputs.

Pure domain logic. All quality gates and asset parameters come in as
explicit arguments via AssetProfile — no config singleton, no _cfg import.
"""

from __future__ import annotations

import logging
from typing import Optional

from domain.assets.profiles import AssetProfile, in_session
from domain.entities.enums import SignalStatus
from domain.entities.ranges import HtfRange, RejectionCandle
from domain.entities.trade import TradeSignal
from domain.trade_management import tp1_level

logger = logging.getLogger(__name__)


def _interval_to_ms(interval: str) -> int:
    s = interval.strip().lower()
    if s.endswith("min"):
        return int(s[:-3]) * 60 * 1000
    if s.endswith("h"):
        return int(s[:-1]) * 60 * 60 * 1000
    if s.endswith("day"):
        return int(s[:-3]) * 24 * 60 * 60 * 1000
    if s.endswith("week"):
        return int(s[:-4]) * 7 * 24 * 60 * 60 * 1000
    if s.endswith("month"):
        return int(s[:-5]) * 30 * 24 * 60 * 60 * 1000
    return 0


def build_signal(
    *,
    symbol:       str,
    htf_interval: str,
    ltf_interval: str,
    htf_range:    HtfRange,
    rejection:    RejectionCandle,
    signal_id:    str,
    profile:      AssetProfile,
    broker:       str = "",
    live_bid:     Optional[float] = None,
    live_ask:     Optional[float] = None,
) -> Optional[TradeSignal]:
    """
    Validate and construct a TradeSignal.

    Returns None (with a debug log) if any quality gate fails.
    All gate thresholds come from `profile`; nothing is hardcoded here.

    Gates (in order)
    ────────────────
    1. SL direction — SL must be beyond entry.
    2. SL distance cap — risk must not exceed max_sl_zone_mult × zone height.
    3. TP2 direction — TP2 must be beyond entry.
    4. RR floor — must meet min_rr.
    5. RR cap — TP2 capped when rr > max_rr (not skipped; tp2 is adjusted).
    6. Session filter — rejection candle must be inside an allowed session.

    Pricing
    ───────
    entry defaults to the LTF rejection candle close (`profile.signal_price_source
    == "candle_close"`). When `profile.signal_price_source == "live_bidask"` and a
    matching live_bid/live_ask is supplied, entry uses the side of the spread that
    matches the trade direction (bid for SHORT, ask for LONG) instead — this is
    the price execution will actually see, closing the gap between signal-time RR
    and fill-time RR. If live_bidask is requested but no tick was supplied (e.g.
    feed unavailable), this silently falls back to candle_close rather than
    blocking signal generation.
    """
    direction = htf_range.signal_direction

    from domain.entities.enums import SignalDirection

    use_live = profile.signal_price_source == "live_bidask"
    if use_live and direction == SignalDirection.SHORT and live_bid is not None:
        entry = live_bid
    elif use_live and direction == SignalDirection.LONG and live_ask is not None:
        entry = live_ask
    else:
        entry = rejection.close

    # ── 1. Stop loss (always wick-based) ──────────────────────────────────────
    sl_level = rejection.wick_tip
    buffer = sl_level * profile.stop_buffer_pct
    sl = sl_level + buffer if direction == SignalDirection.SHORT else sl_level - buffer

    if direction == SignalDirection.SHORT and sl <= entry:
        logger.debug("[%s] SL %.5f not above entry %.5f — skipped", symbol, sl, entry)
        return None
    if direction == SignalDirection.LONG and sl >= entry:
        logger.debug("[%s] SL %.5f not below entry %.5f — skipped", symbol, sl, entry)
        return None

    risk = abs(entry - sl)
    if risk < 1e-8:
        return None

    # ── 2. SL distance cap ────────────────────────────────────────────────────
    zone_h = htf_range.height
    if zone_h > 0 and risk > zone_h * profile.max_sl_zone_mult:
        logger.debug(
            "[%s] SL cap: risk %.5f > %.1f× zone %.5f — skipped",
            symbol, risk, profile.max_sl_zone_mult, zone_h,
        )
        return None

    # ── 3. TP2 direction ──────────────────────────────────────────────────────
    tp2 = htf_range.tp_level
    if direction == SignalDirection.SHORT and tp2 >= entry:
        logger.debug("[%s] TP2 %.5f not below entry %.5f — skipped", symbol, tp2, entry)
        return None
    if direction == SignalDirection.LONG and tp2 <= entry:
        logger.debug("[%s] TP2 %.5f not above entry %.5f — skipped", symbol, tp2, entry)
        return None

    reward = abs(tp2 - entry)
    rr     = reward / risk

    # ── 4. RR floor ───────────────────────────────────────────────────────────
    if rr < profile.min_rr:
        logger.debug("[%s] RR %.2f < min %.1f — skipped", symbol, rr, profile.min_rr)
        return None

    # ── 5. RR cap (adjust TP2; do not skip) ───────────────────────────────────
    if profile.max_rr > 0 and rr > profile.max_rr:
        capped_reward = profile.max_rr * risk
        tp2 = (
            entry + capped_reward
            if direction == SignalDirection.LONG
            else entry - capped_reward
        )
        rr = profile.max_rr

    # ── 6. Session filter ─────────────────────────────────────────────────────
    if not in_session(profile, rejection.timestamp):
        logger.debug(
            "[%s] Rejection at %d outside allowed sessions — skipped",
            symbol, rejection.timestamp,
        )
        return None

    tp1 = tp1_level(
        direction=direction,
        entry_price=entry,
        tp2=tp2,
        tp1_trigger_pct=profile.tp1_trigger_pct,
    )
    setup_candle_open_at = rejection.timestamp
    setup_candle_close_at = rejection.timestamp + _interval_to_ms(ltf_interval)

    price_source_used = "live_bidask" if entry != rejection.close else "candle_close"
    price_drift = (entry - rejection.close) if price_source_used == "live_bidask" else None

    return TradeSignal(
        id               = signal_id,
        symbol           = symbol,
        direction        = direction,
        status           = SignalStatus.TRIGGERED,
        entry_price      = entry,
        stop_loss        = sl,
        tp1              = tp1,
        tp2              = tp2,
        htf_range        = htf_range,
        rejection_candle = rejection,
        risk_reward_ratio = rr,
        risk_pips         = risk,
        htf_interval     = htf_interval,
        ltf_interval     = ltf_interval,
        broker           = broker,
        created_at       = setup_candle_open_at,
        triggered_at     = setup_candle_close_at,
        setup_candle_open_at  = setup_candle_open_at,
        price_source_used   = price_source_used,
        candle_close_price  = rejection.close,
        live_bid_at_signal  = live_bid,
        live_ask_at_signal  = live_ask,
        price_drift         = price_drift,
        setup_candle_close_at = setup_candle_close_at,
    )
