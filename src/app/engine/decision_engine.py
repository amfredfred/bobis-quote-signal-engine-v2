"""Canonical signal decision wrapper used by live and backtest callers."""

from __future__ import annotations

from dataclasses import dataclass

from domain.assets.profiles import AssetProfile
from domain.entities.ranges import HtfRange, RejectionCandle
from domain.entities.trade import TradeSignal
from domain.signals.builder import build_signal


@dataclass(frozen=True)
class Decision:
    signal: TradeSignal | None
    decision_reason: str
    blocked_reason: str | None = None


class DecisionEngine:
    """Small facade around canonical domain signal construction."""

    def evaluate_setup(
        self,
        *,
        symbol: str,
        htf_interval: str,
        ltf_interval: str,
        htf_range: HtfRange,
        rejection: RejectionCandle,
        signal_id: str,
        profile: AssetProfile,
        broker: str = "",
        blocked_reason: str | None = None,
    ) -> Decision:
        if blocked_reason:
            return Decision(None, "blocked", blocked_reason)
        signal = build_signal(
            symbol=symbol,
            htf_interval=htf_interval,
            ltf_interval=ltf_interval,
            htf_range=htf_range,
            rejection=rejection,
            signal_id=signal_id,
            profile=profile,
            broker=broker,
        )
        if signal is None:
            return Decision(None, "blocked", "signal_quality_gate")
        return Decision(signal, "valid_signal")
