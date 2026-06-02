"""Market-data normalization and deterministic signal replay."""

from __future__ import annotations

import copy
import datetime as dt
import math
from dataclasses import dataclass

from domain.assets.profiles import AssetProfile
from domain.entities.candle import Candle
from domain.entities.enums import SignalDirection, SignalOutcome, SignalStatus
from domain.entities.trade import TradeSignal


@dataclass(frozen=True)
class StepOutcome:
    terminal: bool
    emit_tp1: bool = False
    emit_inv_log: bool = False


def normalize_candle(candle: Candle) -> Candle:
    """Return a validated UTC-ms OHLCV candle used by every engine mode."""
    if not isinstance(candle.timestamp, int):
        raise TypeError("candle.timestamp must be a UTC millisecond integer")
    instant = dt.datetime.fromtimestamp(
        candle.timestamp / 1000, tz=dt.timezone.utc
    )
    if instant.tzinfo is None or instant.utcoffset() != dt.timedelta(0):
        raise ValueError("candle.timestamp must normalize to UTC")

    values = {
        "open": candle.open,
        "high": candle.high,
        "low": candle.low,
        "close": candle.close,
        "volume": candle.volume,
    }
    for name, value in values.items():
        if value is None or not math.isfinite(float(value)):
            raise ValueError(f"candle.{name} must be finite")
    if candle.high < candle.low:
        raise ValueError("candle.high must be >= candle.low")

    return Candle(
        timestamp=candle.timestamp,
        open=float(candle.open),
        high=float(candle.high),
        low=float(candle.low),
        close=float(candle.close),
        volume=float(candle.volume),
    )


def step_signal_state(
    signal: TradeSignal, candle: Candle, profile: AssetProfile, now: int
) -> StepOutcome:
    """Single live/backtest lifecycle policy for SL, TP, invalidation and expiry."""
    candle = normalize_candle(candle)
    is_short = signal.direction == SignalDirection.SHORT
    expiry_ms = int(profile.signal_expiry_hours * 3_600_000)

    if now - signal.created_at > expiry_ms:
        if signal.status == SignalStatus.TRIGGERED:
            signal.outcome = SignalOutcome.EXPIRED
            signal.realized_rr = 0.0
            signal.close_price = candle.close
        elif signal.status == SignalStatus.TP1_HIT:
            if profile.use_breakeven:
                signal.outcome = SignalOutcome.BREAKEVEN
                signal.realized_rr = signal.risk_reward_ratio * profile.tp1_multiplier
                signal.close_price = signal.entry_price
            else:
                signal.outcome = SignalOutcome.EXPIRED
                signal.realized_rr = 0.0
                signal.close_price = candle.close
        signal.status = SignalStatus.EXPIRED
        signal.expired_at = now
        signal.closed_at = now
        return StepOutcome(terminal=True)

    sl_hit = (
        candle.high >= signal.stop_loss
        if is_short
        else candle.low <= signal.stop_loss
    )
    tp1_chk = candle.low <= signal.tp1 if is_short else candle.high >= signal.tp1
    tp2_hit = candle.low <= signal.tp2 if is_short else candle.high >= signal.tp2
    inv_now = (
        candle.close > signal.ltf_range.range_high
        if is_short
        else candle.close < signal.ltf_range.range_low
    )

    emit_inv_log = False
    emit_tp1 = False

    if signal.status == SignalStatus.TRIGGERED:
        if tp1_chk and tp2_hit:
            signal.status = SignalStatus.TP2_HIT
            signal.outcome = SignalOutcome.WIN_FULL
            signal.realized_rr = signal.risk_reward_ratio
            signal.tp1_hit_at = now
            signal.tp2_hit_at = now
            signal.closed_at = now
            signal.close_price = signal.tp2
            return StepOutcome(terminal=True)

        if inv_now and profile.use_invalidation:
            signal.invalidated_at = now
            signal.status = SignalStatus.INVALIDATED
            signal.outcome = SignalOutcome.LOSS
            signal.realized_rr = -(abs(signal.entry_price - candle.close) / signal.risk_pips)
            signal.closed_at = now
            signal.close_price = candle.close
            return StepOutcome(terminal=True)

        if inv_now and not profile.use_invalidation and signal.invalidation_logged_at is None:
            signal.invalidation_logged_at = now
            emit_inv_log = True

        if sl_hit:
            signal.status = SignalStatus.SL_HIT
            signal.outcome = SignalOutcome.LOSS
            signal.realized_rr = -1.0
            signal.sl_hit_at = now
            signal.closed_at = now
            signal.close_price = signal.stop_loss
            return StepOutcome(terminal=True, emit_inv_log=emit_inv_log)

        if tp1_chk:
            signal.status = SignalStatus.TP1_HIT
            signal.tp1_hit_at = now
            emit_tp1 = True

    if signal.status == SignalStatus.TP1_HIT:
        if tp2_hit:
            signal.status = SignalStatus.TP2_HIT
            signal.outcome = SignalOutcome.WIN_FULL
            signal.realized_rr = signal.risk_reward_ratio
            signal.tp2_hit_at = now
            signal.closed_at = now
            signal.close_price = signal.tp2
            return StepOutcome(terminal=True, emit_tp1=emit_tp1)

        if inv_now and profile.use_invalidation:
            signal.invalidated_at = now
            signal.status = SignalStatus.INVALIDATED
            signal.closed_at = now
            if profile.use_breakeven:
                signal.outcome = SignalOutcome.BREAKEVEN
                signal.realized_rr = signal.risk_reward_ratio * profile.tp1_multiplier
                signal.close_price = signal.entry_price
            else:
                signal.outcome = SignalOutcome.LOSS
                signal.realized_rr = -(abs(signal.entry_price - candle.close) / signal.risk_pips)
                signal.close_price = candle.close
            return StepOutcome(terminal=True, emit_tp1=emit_tp1)

        if inv_now and not profile.use_invalidation and signal.invalidation_logged_at is None:
            signal.invalidation_logged_at = now
            emit_inv_log = True

        if sl_hit:
            signal.sl_hit_at = now
            signal.closed_at = now
            signal.status = SignalStatus.SL_HIT
            if profile.use_breakeven:
                signal.outcome = SignalOutcome.BREAKEVEN
                signal.realized_rr = signal.risk_reward_ratio * profile.tp1_multiplier
                signal.close_price = signal.entry_price
            else:
                signal.outcome = SignalOutcome.LOSS
                signal.realized_rr = -1.0
                signal.close_price = signal.stop_loss
            return StepOutcome(terminal=True, emit_tp1=emit_tp1, emit_inv_log=emit_inv_log)

    return StepOutcome(terminal=False, emit_tp1=emit_tp1, emit_inv_log=emit_inv_log)


def replay_signal_lifecycle(
    signal: TradeSignal, candles: list[Candle], profile: AssetProfile
) -> TradeSignal:
    """Replay a signal through the exact same lifecycle path live uses."""
    probe = copy.deepcopy(signal)
    for candle in candles:
        step = step_signal_state(probe, candle, profile, candle.timestamp)
        if step.terminal:
            break
    return probe

