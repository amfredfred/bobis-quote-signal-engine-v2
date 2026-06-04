"""
tests/test_signal_lifecycle.py

Tests for _evaluate_signal and _simulate_lifecycle in SignalService.

Coverage strategy
─────────────────
Each test encodes a deterministic OHLC sequence and asserts:
  - outcome / status
  - realized_rr
  - close_price  ← main regression target

Both methods are tested with the same scenario to prove they agree,
and both are verified to match the Numba/NumPy backtest kernel's
same-bar conflict priorities.

Conflict priority table (all four paths must agree)
────────────────────────────────────────────────────
  State       Same-bar conflict          Winner
  ──────────  ─────────────────────────  ──────────
  TRIGGERED   SL  vs TP1                SL        (FIX #13)
  TRIGGERED   INV vs SL                 INV       (FIX #22)
  TRIGGERED   TP1+TP2 vs INV            TP2       (FIX #25)  ← new
  TP1_HIT     TP2 vs INV                TP2       (FIX #19/#21)
  TP1_HIT     INV vs SL  (use_be=True)  BREAKEVEN
  TP1_HIT     INV vs SL  (use_be=False) LOSS at candle.close
"""

from __future__ import annotations

import copy
import sys
import os
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from domain.entities.candle import Candle
from domain.entities.enums import (
    BosDirection,
    CandlePattern,
    SignalDirection,
    SignalOutcome,
    SignalStatus,
)
from domain.entities.ranges import HtfRange, LtfRange, RejectionCandle
from domain.entities.trade import TradeSignal


# ── Constants ─────────────────────────────────────────────────────────────────

ENTRY = 1.10000
SL = 1.09000  # 100 pips below entry (LONG)
TP1 = 1.10500  # 50 % to TP2
TP2 = 1.11000  # 100 pips above entry
RISK = ENTRY - SL
RR = (TP2 - ENTRY) / RISK  # 1.0
TP1_TRIGGER_PCT = 50.0
TP1_CLOSE_PCT = 0.0

BASE_TS = 1_700_000_000_000
BAR_MS = 60_000

LTF_RANGE_HIGH = 1.10200
LTF_RANGE_LOW = 1.09800


# ── Domain builders ───────────────────────────────────────────────────────────


def _htf_range() -> HtfRange:
    return HtfRange(
        range_high=1.10500,
        range_low=1.09500,
        bos_direction=BosDirection.BULLISH,
        timestamp=BASE_TS - 3_600_000,
        broken_at=BASE_TS - 1_800_000,
        tp_level=TP2,
    )


def _ltf_range(direction: SignalDirection = SignalDirection.LONG) -> LtfRange:
    return LtfRange(
        range_high=LTF_RANGE_HIGH,
        range_low=LTF_RANGE_LOW,
        timestamp=BASE_TS - 900_000,
        direction=direction,
    )


def _rejection() -> RejectionCandle:
    return RejectionCandle(
        open=1.09900,
        high=1.10250,
        low=1.09850,
        close=ENTRY,
        timestamp=BASE_TS,
        wick_ratio=0.72,
        pattern=CandlePattern.HAMMER,
    )


def make_signal(
    *,
    direction: SignalDirection = SignalDirection.LONG,
    status: SignalStatus = SignalStatus.TRIGGERED,
    entry: float = ENTRY,
    sl: float = SL,
    tp1: float = TP1,
    tp2: float = TP2,
    created_at: int = BASE_TS,
    signal_expiry_hours: float = 24.0,
) -> TradeSignal:
    risk = abs(entry - sl)
    rr = abs(tp2 - entry) / risk
    return TradeSignal(
        id="TEST_SIG",
        symbol="EURUSD",
        direction=direction,
        status=status,
        entry_price=entry,
        stop_loss=sl,
        tp1=tp1,
        tp2=tp2,
        htf_range=_htf_range(),
        ltf_range=_ltf_range(direction),
        rejection_candle=_rejection(),
        risk_reward_ratio=rr,
        risk_pips=risk,
        htf_interval="1h",
        ltf_interval="5min",
        created_at=created_at,
        triggered_at=created_at,
    )


def make_candle(
    *,
    ts: int,
    open: float = ENTRY,
    high: float = ENTRY + 0.00050,
    low: float = ENTRY - 0.00050,
    close: float = ENTRY,
) -> Candle:
    return Candle(timestamp=ts, open=open, high=high, low=low, close=close, volume=0.0)


# ── SignalService harness ─────────────────────────────────────────────────────


def make_service(
    *,
    use_breakeven: bool = True,
    use_invalidation: bool = True,
    signal_expiry_hours: float = 24.0,
):
    from app.services.signal_service import SignalService

    profile = MagicMock()
    profile.move_sl_to_be_on_tp1 = use_breakeven
    profile.use_invalidation = use_invalidation
    profile.signal_expiry_hours = signal_expiry_hours
    profile.tp1_trigger_pct = TP1_TRIGGER_PCT
    profile.tp1_close_pct = TP1_CLOSE_PCT

    registry = MagicMock()
    registry.get.return_value = profile

    settings = MagicMock()
    settings.now_ms.return_value = BASE_TS
    settings.signal_expiry_hours = signal_expiry_hours
    settings.tf_pairs = []

    svc = SignalService(
        market_data=MagicMock(),
        settings=settings,
        asset_registry=registry,
        session=MagicMock(),
        signal_store=MagicMock(),
    )
    return svc, profile


# ── Test helpers ──────────────────────────────────────────────────────────────


def eval_signal(svc, signal: TradeSignal, candle: Candle, now: int) -> None:
    svc._evaluate_signal(signal, candle, now)


def simulate(svc, signal: TradeSignal, candles: list[Candle]) -> TradeSignal:
    probe = copy.deepcopy(signal)
    probe.status = SignalStatus.TRIGGERED
    probe.outcome = None
    probe.realized_rr = None
    probe.closed_at = None
    probe.close_price = None
    probe.expired_at = None
    probe.invalidated_at = None
    probe.invalidation_logged_at = None
    probe.tp1_hit_at = None
    probe.tp2_hit_at = None
    probe.sl_hit_at = None
    svc._simulate_lifecycle(probe, candles)
    return probe


def test_signal_payload_separates_candle_and_emit_times():
    signal = make_signal(created_at=BASE_TS)
    signal.detected_at = BASE_TS + 301_500
    signal.emitted_at = BASE_TS + 301_750

    payload = signal.to_dict()

    assert payload["triggeredAt"] == BASE_TS
    assert payload["rejectionCandle"]["timestamp"] == BASE_TS
    assert payload["rejectionCandle"]["closeAt"] == BASE_TS + 300_000
    assert payload["rejectionCandleCloseAt"] == BASE_TS + 300_000
    assert payload["detectedAt"] == BASE_TS + 301_500
    assert payload["emittedAt"] == BASE_TS + 301_750


def both_agree(svc, signal_factory, candles, *, attr: str = "close_price"):
    """Assert _evaluate_signal and _simulate_lifecycle return the same value."""
    s1 = signal_factory()
    for c in candles:
        eval_signal(svc, s1, c, c.timestamp)
        if s1.status not in (SignalStatus.TRIGGERED, SignalStatus.TP1_HIT):
            break

    s2 = signal_factory()
    probe = simulate(svc, s2, candles)

    v1, v2 = getattr(s1, attr), getattr(probe, attr)
    assert (
        v1 == v2
    ), f"_evaluate_signal={v1!r} != _simulate_lifecycle={v2!r} for {attr!r}"
    return v1, v2


# ═══════════════════════════════════════════════════════════════════════════════
# SL scenarios
# ═══════════════════════════════════════════════════════════════════════════════


class TestSLHit:
    """SL hit while TRIGGERED."""

    def _candle(self) -> Candle:
        return make_candle(
            ts=BASE_TS + BAR_MS,
            high=ENTRY + 0.00010,
            low=SL - 0.00020,
            close=1.09850,  # above LTF_RANGE_LOW → no INV
        )

    def test_evaluate_outcome(self):
        svc, _ = make_service()
        signal = make_signal()
        eval_signal(svc, signal, self._candle(), BASE_TS + BAR_MS)

        assert signal.status == SignalStatus.SL_HIT
        assert signal.outcome == SignalOutcome.LOSS
        assert signal.realized_rr == -1.0
        assert signal.close_price == SL

    def test_simulate_outcome(self):
        svc, _ = make_service()
        probe = simulate(svc, make_signal(), [self._candle()])

        assert probe.status == SignalStatus.SL_HIT
        assert probe.outcome == SignalOutcome.LOSS
        assert probe.realized_rr == -1.0
        assert probe.close_price == SL

    def test_both_paths_agree(self):
        svc, _ = make_service()
        both_agree(svc, make_signal, [self._candle()])


class TestSLHitAfterTP1NoBreakeven:
    """SL after TP1 with use_breakeven=False → LOSS at SL."""

    def _candles(self):
        return (
            make_candle(
                ts=BASE_TS + BAR_MS,
                high=TP1 + 0.00010,
                low=ENTRY - 0.00010,
                close=TP1 - 0.00005,
            ),
            make_candle(
                ts=BASE_TS + 2 * BAR_MS,
                high=ENTRY + 0.00010,
                low=SL - 0.00100,
                close=1.09850,
            ),
        )

    def test_evaluate_outcome(self):
        svc, _ = make_service(use_breakeven=False)
        signal = make_signal()
        tp1_bar, sl_bar = self._candles()
        eval_signal(svc, signal, tp1_bar, tp1_bar.timestamp)
        eval_signal(svc, signal, sl_bar, sl_bar.timestamp)

        assert signal.status == SignalStatus.SL_HIT
        assert signal.outcome == SignalOutcome.LOSS
        assert signal.realized_rr == -1.0
        assert signal.close_price == SL

    def test_simulate_outcome(self):
        svc, _ = make_service(use_breakeven=False)
        probe = simulate(svc, make_signal(), list(self._candles()))

        assert probe.outcome == SignalOutcome.LOSS
        assert probe.close_price == SL

    def test_both_paths_agree(self):
        svc, _ = make_service(use_breakeven=False)
        both_agree(svc, make_signal, list(self._candles()))


class TestSLHitAfterTP1Breakeven:
    """SL after TP1 with use_breakeven=True → BREAKEVEN at entry."""

    def _candles(self):
        return (
            make_candle(
                ts=BASE_TS + BAR_MS,
                high=TP1 + 0.00010,
                low=ENTRY - 0.00010,
                close=TP1 - 0.00005,
            ),
            make_candle(
                ts=BASE_TS + 2 * BAR_MS,
                high=ENTRY + 0.00010,
                low=SL - 0.00100,
                close=1.09850,
            ),
        )

    def test_evaluate_outcome(self):
        svc, _ = make_service(use_breakeven=True)
        signal = make_signal()
        tp1_bar, sl_bar = self._candles()
        eval_signal(svc, signal, tp1_bar, tp1_bar.timestamp)
        eval_signal(svc, signal, sl_bar, sl_bar.timestamp)

        assert signal.outcome == SignalOutcome.BREAKEVEN
        assert signal.close_price == ENTRY
        assert signal.realized_rr == pytest.approx(0.0)

    def test_simulate_outcome(self):
        svc, _ = make_service(use_breakeven=True)
        probe = simulate(svc, make_signal(), list(self._candles()))

        assert probe.outcome == SignalOutcome.BREAKEVEN
        assert probe.close_price == ENTRY

    def test_both_paths_agree(self):
        svc, _ = make_service(use_breakeven=True)
        both_agree(svc, make_signal, list(self._candles()))


# ═══════════════════════════════════════════════════════════════════════════════
# TP2 scenarios
# ═══════════════════════════════════════════════════════════════════════════════


class TestTP2Hit:
    """Standard TP1 → TP2 path."""

    def _candles(self):
        return (
            make_candle(
                ts=BASE_TS + BAR_MS,
                high=TP1 + 0.00010,
                low=ENTRY - 0.00010,
                close=TP1 - 0.00005,
            ),
            make_candle(
                ts=BASE_TS + 2 * BAR_MS,
                high=TP2 + 0.00050,
                low=ENTRY + 0.00010,
                close=TP2 + 0.00020,
            ),
        )

    def test_evaluate_outcome(self):
        svc, _ = make_service()
        signal = make_signal()
        tp1_bar, tp2_bar = self._candles()
        eval_signal(svc, signal, tp1_bar, tp1_bar.timestamp)
        eval_signal(svc, signal, tp2_bar, tp2_bar.timestamp)

        assert signal.status == SignalStatus.TP2_HIT
        assert signal.outcome == SignalOutcome.WIN_FULL
        assert signal.realized_rr == pytest.approx(RR)
        assert signal.close_price == TP2

    def test_simulate_outcome(self):
        svc, _ = make_service()
        probe = simulate(svc, make_signal(), list(self._candles()))

        assert probe.status == SignalStatus.TP2_HIT
        assert probe.close_price == TP2

    def test_both_paths_agree(self):
        svc, _ = make_service()
        both_agree(svc, make_signal, list(self._candles()))


class TestTP2SameBarAsTP1:
    """Same-bar TRIGGERED → TP1 → TP2, no INV. WIN_FULL on both paths."""

    def _candle(self):
        return make_candle(
            ts=BASE_TS + BAR_MS,
            high=TP2 + 0.00100,  # clears TP1 and TP2
            low=ENTRY - 0.00010,
            close=TP2 + 0.00050,
        )

    def test_evaluate_outcome(self):
        svc, _ = make_service()
        signal = make_signal()
        eval_signal(svc, signal, self._candle(), BASE_TS + BAR_MS)

        assert signal.status == SignalStatus.TP2_HIT
        assert signal.outcome == SignalOutcome.WIN_FULL
        assert signal.close_price == TP2

    def test_simulate_outcome(self):
        svc, _ = make_service()
        probe = simulate(svc, make_signal(), [self._candle()])

        assert probe.status == SignalStatus.TP2_HIT
        assert probe.close_price == TP2

    def test_both_paths_agree(self):
        svc, _ = make_service()
        both_agree(svc, make_signal, [self._candle()])


class TestTP2EarlyCheck:
    """TP2 + INV same bar while TP1_HIT (carried) — TP2 wins (FIX #19/#21)."""

    def _candles(self):
        return (
            make_candle(
                ts=BASE_TS + BAR_MS,
                high=TP1 + 0.00010,
                low=ENTRY - 0.00010,
                close=TP1 - 0.00005,
            ),
            # high clears TP2; close breaks LTF range_low → INV for LONG
            make_candle(
                ts=BASE_TS + 2 * BAR_MS,
                high=TP2 + 0.00050,
                low=ENTRY - 0.00010,
                close=LTF_RANGE_LOW - 0.00100,
            ),
        )

    def test_evaluate_tp2_wins_over_inv(self):
        svc, _ = make_service(use_invalidation=True)
        signal = make_signal()
        tp1_bar, inv_tp2_bar = self._candles()
        eval_signal(svc, signal, tp1_bar, tp1_bar.timestamp)
        eval_signal(svc, signal, inv_tp2_bar, inv_tp2_bar.timestamp)

        assert signal.outcome == SignalOutcome.WIN_FULL
        assert signal.close_price == TP2

    def test_simulate_tp2_wins_over_inv(self):
        svc, _ = make_service(use_invalidation=True)
        probe = simulate(svc, make_signal(), list(self._candles()))

        assert probe.outcome == SignalOutcome.WIN_FULL
        assert probe.close_price == TP2

    def test_both_paths_agree(self):
        svc, _ = make_service(use_invalidation=True)
        both_agree(svc, make_signal, list(self._candles()))


# ═══════════════════════════════════════════════════════════════════════════════
# FIX #25 — same-bar TRIGGERED + TP1 + TP2 + INV
# ═══════════════════════════════════════════════════════════════════════════════


class TestTP2TriggeredSameBarINV:
    """
    FIX #25 regression suite.

    Scenario: while TRIGGERED, one large-range candle simultaneously:
      • high >= TP2  (clears both TP1 and TP2)
      • close < LTF range_low  (INV boundary for LONG)

    Pre-fix:  INV block fires first → LOSS  (diverges from Numba kernel)
    Post-fix: TP2 pre-check fires first → WIN_FULL  (matches Numba kernel)

    Numba kernel priority:
      (tp1_prev OR tp1_now) AND tp2_now → WIN_FULL, before INV is evaluated.
    """

    @staticmethod
    def _long_candle() -> Candle:
        return make_candle(
            ts=BASE_TS + BAR_MS,
            high=TP2 + 0.00500,  # clears TP1 and TP2
            low=SL + 0.00100,  # above SL
            close=LTF_RANGE_LOW - 0.00200,  # closes through range_low → INV
        )

    # ── LONG ──────────────────────────────────────────────────────────────────

    def test_long_evaluate_tp2_wins(self):
        svc, _ = make_service(use_invalidation=True, use_breakeven=True)
        signal = make_signal()
        eval_signal(svc, signal, self._long_candle(), BASE_TS + BAR_MS)

        assert signal.outcome == SignalOutcome.WIN_FULL, (
            f"FIX #25 LONG: expected WIN_FULL, got {signal.outcome}. "
            "TP2 must beat same-bar INV when TRIGGERED."
        )
        assert signal.status == SignalStatus.TP2_HIT
        assert signal.close_price == TP2
        assert signal.realized_rr == pytest.approx(RR)

    def test_long_simulate_tp2_wins(self):
        svc, _ = make_service(use_invalidation=True, use_breakeven=True)
        probe = simulate(svc, make_signal(), [self._long_candle()])

        assert (
            probe.outcome == SignalOutcome.WIN_FULL
        ), f"FIX #25 LONG (_simulate_lifecycle): got {probe.outcome}"
        assert probe.status == SignalStatus.TP2_HIT
        assert probe.close_price == TP2
        assert probe.realized_rr == pytest.approx(RR)

    def test_long_both_paths_agree_outcome(self):
        svc, _ = make_service(use_invalidation=True, use_breakeven=True)
        both_agree(svc, make_signal, [self._long_candle()], attr="outcome")

    def test_long_both_paths_agree_close_price(self):
        svc, _ = make_service(use_invalidation=True, use_breakeven=True)
        both_agree(svc, make_signal, [self._long_candle()], attr="close_price")

    def test_long_use_breakeven_false_still_wins(self):
        svc, _ = make_service(use_invalidation=True, use_breakeven=False)
        signal = make_signal()
        eval_signal(svc, signal, self._long_candle(), BASE_TS + BAR_MS)
        assert signal.outcome == SignalOutcome.WIN_FULL

    def test_long_use_invalidation_false_still_wins(self):
        """INV disabled — TP2 fires normally, WIN_FULL."""
        svc, _ = make_service(use_invalidation=False)
        signal = make_signal()
        eval_signal(svc, signal, self._long_candle(), BASE_TS + BAR_MS)
        assert signal.outcome == SignalOutcome.WIN_FULL

    # ── SHORT ─────────────────────────────────────────────────────────────────

    SHORT_ENTRY = 1.10000
    SHORT_SL = 1.11000
    SHORT_TP1 = 1.09500
    SHORT_TP2 = 1.09000
    SHORT_RR = (SHORT_ENTRY - SHORT_TP2) / (SHORT_SL - SHORT_ENTRY)  # 1.0

    def _make_short_signal(self) -> TradeSignal:
        risk = self.SHORT_SL - self.SHORT_ENTRY
        return TradeSignal(
            id="TEST_SHORT_25",
            symbol="EURUSD",
            direction=SignalDirection.SHORT,
            status=SignalStatus.TRIGGERED,
            entry_price=self.SHORT_ENTRY,
            stop_loss=self.SHORT_SL,
            tp1=self.SHORT_TP1,
            tp2=self.SHORT_TP2,
            htf_range=HtfRange(
                range_high=1.10500,
                range_low=1.09500,
                bos_direction=BosDirection.BEARISH,
                timestamp=BASE_TS - 3_600_000,
                broken_at=BASE_TS - 1_800_000,
                tp_level=self.SHORT_TP2,
            ),
            ltf_range=LtfRange(
                range_high=LTF_RANGE_HIGH,
                range_low=LTF_RANGE_LOW,
                timestamp=BASE_TS - 900_000,
                direction=SignalDirection.SHORT,
            ),
            rejection_candle=_rejection(),
            risk_reward_ratio=self.SHORT_RR,
            risk_pips=risk,
            htf_interval="1h",
            ltf_interval="5min",
            created_at=BASE_TS,
            triggered_at=BASE_TS,
        )

    def _short_candle(self) -> Candle:
        """
        SHORT: INV fires when close > ltf_range.range_high.
        TP2   fires when low <= tp2.
        This candle does both.
        """
        return make_candle(
            ts=BASE_TS + BAR_MS,
            high=LTF_RANGE_HIGH + 0.00100,  # above range_high → INV
            low=self.SHORT_TP2 - 0.00500,  # clears TP1 and TP2
            close=LTF_RANGE_HIGH + 0.00050,  # closes through INV
        )

    def test_short_evaluate_tp2_wins(self):
        svc, _ = make_service(use_invalidation=True, use_breakeven=True)
        signal = self._make_short_signal()
        eval_signal(svc, signal, self._short_candle(), BASE_TS + BAR_MS)

        assert (
            signal.outcome == SignalOutcome.WIN_FULL
        ), f"FIX #25 SHORT: expected WIN_FULL, got {signal.outcome}"
        assert signal.close_price == self.SHORT_TP2

    def test_short_simulate_tp2_wins(self):
        svc, _ = make_service(use_invalidation=True, use_breakeven=True)
        probe = simulate(svc, self._make_short_signal(), [self._short_candle()])

        assert probe.outcome == SignalOutcome.WIN_FULL
        assert probe.close_price == self.SHORT_TP2

    def test_short_both_paths_agree(self):
        svc, _ = make_service(use_invalidation=True, use_breakeven=True)
        both_agree(svc, self._make_short_signal, [self._short_candle()], attr="outcome")

    # ── Boundary: TP1 only (not TP2) fires on INV bar → LOSS ─────────────────

    def test_tp1_only_no_tp2_inv_bar_loses(self):
        """
        TP1 fires same bar as INV, but TP2 does NOT.
        Fix is precise — only the TP1+TP2 combination overrides INV.
        """
        candle = make_candle(
            ts=BASE_TS + BAR_MS,
            high=TP1 + 0.00050,  # reaches TP1 but NOT TP2
            low=SL + 0.00100,  # above SL
            close=LTF_RANGE_LOW - 0.00200,  # INV for LONG
        )
        svc, _ = make_service(use_invalidation=True)
        signal = make_signal()
        eval_signal(svc, signal, candle, BASE_TS + BAR_MS)

        assert (
            signal.outcome == SignalOutcome.LOSS
        ), f"TP1+INV (no TP2) must still be LOSS, got {signal.outcome}"

    def test_tp1_only_simulate_agrees(self):
        candle = make_candle(
            ts=BASE_TS + BAR_MS,
            high=TP1 + 0.00050,
            low=SL + 0.00100,
            close=LTF_RANGE_LOW - 0.00200,
        )
        svc, _ = make_service(use_invalidation=True)
        probe = simulate(svc, make_signal(), [candle])
        assert probe.outcome == SignalOutcome.LOSS

    def test_both_paths_agree_tp1_only_boundary(self):
        candle = make_candle(
            ts=BASE_TS + BAR_MS,
            high=TP1 + 0.00050,
            low=SL + 0.00100,
            close=LTF_RANGE_LOW - 0.00200,
        )
        svc, _ = make_service(use_invalidation=True)
        both_agree(svc, make_signal, [candle], attr="outcome")


# ═══════════════════════════════════════════════════════════════════════════════
# Expiry
# ═══════════════════════════════════════════════════════════════════════════════


class TestExpiry:
    """Signal expires without hitting any level."""

    _EXPIRY_HOURS = 0.001

    def _candle(self):
        expiry_ms = int(self._EXPIRY_HOURS * 3_600_000)
        return make_candle(
            ts=BASE_TS + expiry_ms + 1,
            high=ENTRY + 0.00010,
            low=ENTRY - 0.00010,
            close=ENTRY,
        )

    def test_evaluate_outcome(self):
        svc, _ = make_service(signal_expiry_hours=self._EXPIRY_HOURS)
        signal = make_signal(signal_expiry_hours=self._EXPIRY_HOURS)
        candle = self._candle()
        eval_signal(svc, signal, candle, candle.timestamp)

        assert signal.status == SignalStatus.EXPIRED
        assert signal.outcome == SignalOutcome.EXPIRED
        assert signal.realized_rr == 0.0
        assert signal.close_price == candle.close

    def test_simulate_outcome(self):
        svc, _ = make_service(signal_expiry_hours=self._EXPIRY_HOURS)
        probe = simulate(
            svc,
            make_signal(signal_expiry_hours=self._EXPIRY_HOURS),
            [self._candle()],
        )
        assert probe.status == SignalStatus.EXPIRED
        assert probe.outcome == SignalOutcome.EXPIRED
        assert probe.realized_rr == 0.0

    def test_both_paths_agree(self):
        svc, _ = make_service(signal_expiry_hours=self._EXPIRY_HOURS)
        factory = lambda: make_signal(signal_expiry_hours=self._EXPIRY_HOURS)
        both_agree(svc, factory, [self._candle()], attr="outcome")


class TestExpiryAfterTP1Breakeven:
    """Expires after TP1 with use_breakeven=True → BREAKEVEN at entry."""

    _EXPIRY_HOURS = 0.001

    def _candles(self):
        expiry_ms = int(self._EXPIRY_HOURS * 3_600_000)
        return (
            make_candle(
                ts=BASE_TS + 1,
                high=TP1 + 0.00010,
                low=ENTRY - 0.00010,
                close=TP1 - 0.00005,
            ),
            make_candle(
                ts=BASE_TS + expiry_ms + 1,
                high=ENTRY + 0.00010,
                low=ENTRY - 0.00010,
                close=ENTRY + 0.00005,
            ),
        )

    def test_evaluate_breakeven(self):
        svc, _ = make_service(
            use_breakeven=True, signal_expiry_hours=self._EXPIRY_HOURS
        )
        signal = make_signal(signal_expiry_hours=self._EXPIRY_HOURS)
        for c in self._candles():
            eval_signal(svc, signal, c, c.timestamp)

        assert signal.outcome == SignalOutcome.BREAKEVEN
        assert signal.close_price == ENTRY
        assert signal.realized_rr == pytest.approx(0.0)

    def test_simulate_breakeven(self):
        svc, _ = make_service(
            use_breakeven=True, signal_expiry_hours=self._EXPIRY_HOURS
        )
        probe = simulate(
            svc,
            make_signal(signal_expiry_hours=self._EXPIRY_HOURS),
            list(self._candles()),
        )
        assert probe.outcome == SignalOutcome.BREAKEVEN
        assert probe.close_price == ENTRY
        assert probe.realized_rr == pytest.approx(0.0)

    def test_no_breakeven_returns_expired(self):
        """use_breakeven=False: expiry after TP1 → EXPIRED 0.0R."""
        svc, _ = make_service(
            use_breakeven=False, signal_expiry_hours=self._EXPIRY_HOURS
        )
        probe = simulate(
            svc,
            make_signal(signal_expiry_hours=self._EXPIRY_HOURS),
            list(self._candles()),
        )
        assert probe.outcome == SignalOutcome.EXPIRED
        assert probe.realized_rr == 0.0


# ═══════════════════════════════════════════════════════════════════════════════
# Invalidation
# ═══════════════════════════════════════════════════════════════════════════════


class TestInvalidation:
    """INV while TRIGGERED — closes at candle.close."""

    def _candle(self):
        return make_candle(
            ts=BASE_TS + BAR_MS,
            high=ENTRY + 0.00010,
            low=LTF_RANGE_LOW - 0.00100,
            close=LTF_RANGE_LOW - 0.00050,
        )

    def test_evaluate_outcome(self):
        svc, _ = make_service(use_invalidation=True)
        signal = make_signal()
        candle = self._candle()
        eval_signal(svc, signal, candle, BASE_TS + BAR_MS)

        assert signal.status == SignalStatus.INVALIDATED
        assert signal.outcome == SignalOutcome.LOSS
        assert signal.close_price == candle.close
        assert signal.realized_rr < 0.0

    def test_simulate_outcome(self):
        svc, _ = make_service(use_invalidation=True)
        probe = simulate(svc, make_signal(), [self._candle()])

        assert probe.status == SignalStatus.INVALIDATED
        assert probe.close_price == self._candle().close

    def test_both_paths_agree(self):
        svc, _ = make_service(use_invalidation=True)
        both_agree(svc, make_signal, [self._candle()])

    def test_inv_breakeven_after_tp1(self):
        """INV while TP1_HIT + use_breakeven=True → BREAKEVEN at entry."""
        svc, _ = make_service(use_invalidation=True, use_breakeven=True)
        signal = make_signal()
        tp1_bar = make_candle(
            ts=BASE_TS + BAR_MS,
            high=TP1 + 0.00010,
            low=ENTRY - 0.00010,
            close=TP1 - 0.00005,
        )
        inv_bar = make_candle(
            ts=BASE_TS + 2 * BAR_MS,
            high=ENTRY + 0.00010,
            low=LTF_RANGE_LOW - 0.00100,
            close=LTF_RANGE_LOW - 0.00050,
        )
        eval_signal(svc, signal, tp1_bar, tp1_bar.timestamp)
        eval_signal(svc, signal, inv_bar, inv_bar.timestamp)

        assert signal.outcome == SignalOutcome.BREAKEVEN
        assert signal.close_price == ENTRY

    def test_inv_loss_after_tp1_no_breakeven(self):
        """INV while TP1_HIT + use_breakeven=False → LOSS at candle.close."""
        svc, _ = make_service(use_invalidation=True, use_breakeven=False)
        signal = make_signal()
        tp1_bar = make_candle(
            ts=BASE_TS + BAR_MS,
            high=TP1 + 0.00010,
            low=ENTRY - 0.00010,
            close=TP1 - 0.00005,
        )
        inv_bar = make_candle(
            ts=BASE_TS + 2 * BAR_MS,
            high=ENTRY + 0.00010,
            low=LTF_RANGE_LOW - 0.00100,
            close=LTF_RANGE_LOW - 0.00050,
        )
        eval_signal(svc, signal, tp1_bar, tp1_bar.timestamp)
        eval_signal(svc, signal, inv_bar, inv_bar.timestamp)

        assert signal.outcome == SignalOutcome.LOSS
        assert signal.close_price == inv_bar.close


class TestInvalidationDisabled:
    """use_invalidation=False — INV detected but trade stays open."""

    def _candles(self):
        return (
            make_candle(
                ts=BASE_TS + BAR_MS,
                high=ENTRY + 0.00010,
                low=LTF_RANGE_LOW - 0.00100,
                close=LTF_RANGE_LOW - 0.00050,
            ),
            make_candle(
                ts=BASE_TS + 2 * BAR_MS,
                high=ENTRY + 0.00010,
                low=SL - 0.00020,
                close=SL + 0.00010,
            ),
        )

    def test_trade_stays_open_after_inv(self):
        svc, _ = make_service(use_invalidation=False)
        signal = make_signal()
        inv_bar, _ = self._candles()
        eval_signal(svc, signal, inv_bar, inv_bar.timestamp)

        assert signal.status in (SignalStatus.TRIGGERED, SignalStatus.TP1_HIT)
        assert signal.invalidation_logged_at == inv_bar.timestamp

    def test_sl_closes_after_inv_disabled(self):
        svc, _ = make_service(use_invalidation=False)
        signal = make_signal()
        for c in self._candles():
            eval_signal(svc, signal, c, c.timestamp)

        assert signal.status == SignalStatus.SL_HIT
        assert signal.close_price == SL

    def test_simulate_inv_disabled_then_sl(self):
        svc, _ = make_service(use_invalidation=False)
        probe = simulate(svc, make_signal(), list(self._candles()))

        assert probe.status == SignalStatus.SL_HIT
        assert probe.close_price == SL


# ═══════════════════════════════════════════════════════════════════════════════
# Same-bar conflict: SL vs TP1 while TRIGGERED — SL wins (FIX #13)
# ═══════════════════════════════════════════════════════════════════════════════


class TestSLBeatsTP1SameBar:
    """SL and TP1 on the same bar while TRIGGERED → SL wins."""

    def _candle(self):
        return make_candle(
            ts=BASE_TS + BAR_MS,
            high=TP1 + 0.00010,  # high >= TP1
            low=SL - 0.00010,  # low <= SL
            close=1.09850,  # above LTF_RANGE_LOW → no INV
        )

    def test_evaluate_sl_wins(self):
        svc, _ = make_service()
        signal = make_signal()
        eval_signal(svc, signal, self._candle(), BASE_TS + BAR_MS)

        assert signal.outcome == SignalOutcome.LOSS
        assert signal.status == SignalStatus.SL_HIT
        assert signal.realized_rr == -1.0
        assert signal.close_price == SL

    def test_simulate_sl_wins(self):
        svc, _ = make_service()
        probe = simulate(svc, make_signal(), [self._candle()])

        assert probe.outcome == SignalOutcome.LOSS
        assert probe.status == SignalStatus.SL_HIT
        assert probe.close_price == SL

    def test_both_paths_agree(self):
        svc, _ = make_service()
        both_agree(svc, make_signal, [self._candle()], attr="outcome")


# ═══════════════════════════════════════════════════════════════════════════════
# SHORT mirrors
# ═══════════════════════════════════════════════════════════════════════════════

_SHORT_ENTRY = 1.10000
_SHORT_SL = 1.11000
_SHORT_TP1 = 1.09500
_SHORT_TP2 = 1.09000


def make_short_signal() -> TradeSignal:
    risk = _SHORT_SL - _SHORT_ENTRY
    rr = (_SHORT_ENTRY - _SHORT_TP2) / risk
    return TradeSignal(
        id="TEST_SHORT",
        symbol="EURUSD",
        direction=SignalDirection.SHORT,
        status=SignalStatus.TRIGGERED,
        entry_price=_SHORT_ENTRY,
        stop_loss=_SHORT_SL,
        tp1=_SHORT_TP1,
        tp2=_SHORT_TP2,
        htf_range=HtfRange(
            range_high=1.10500,
            range_low=1.09500,
            bos_direction=BosDirection.BEARISH,
            timestamp=BASE_TS - 3_600_000,
            broken_at=BASE_TS - 1_800_000,
            tp_level=_SHORT_TP2,
        ),
        ltf_range=LtfRange(
            range_high=LTF_RANGE_HIGH,
            range_low=LTF_RANGE_LOW,
            timestamp=BASE_TS - 900_000,
            direction=SignalDirection.SHORT,
        ),
        rejection_candle=_rejection(),
        risk_reward_ratio=rr,
        risk_pips=risk,
        htf_interval="1h",
        ltf_interval="5min",
        created_at=BASE_TS,
        triggered_at=BASE_TS,
    )


class TestShortSLHit:
    def test_evaluate_close_price(self):
        svc, _ = make_service()
        signal = make_short_signal()
        candle = make_candle(
            ts=BASE_TS + BAR_MS,
            high=_SHORT_SL + 0.00050,  # high >= SL for SHORT
            low=_SHORT_ENTRY - 0.00010,
            close=1.10100,  # below range_high → no INV
        )
        eval_signal(svc, signal, candle, BASE_TS + BAR_MS)

        assert signal.status == SignalStatus.SL_HIT
        assert signal.close_price == _SHORT_SL

    def test_simulate_close_price(self):
        svc, _ = make_service()
        candle = make_candle(
            ts=BASE_TS + BAR_MS,
            high=_SHORT_SL + 0.00050,
            low=_SHORT_ENTRY - 0.00010,
            close=1.10100,
        )
        probe = simulate(svc, make_short_signal(), [candle])

        assert probe.status == SignalStatus.SL_HIT
        assert probe.close_price == _SHORT_SL


class TestShortTP2Hit:
    def _candles(self):
        return (
            make_candle(
                ts=BASE_TS + BAR_MS,
                high=_SHORT_ENTRY + 0.00010,
                low=_SHORT_TP1 - 0.00010,  # low <= tp1 for SHORT
                close=_SHORT_TP1 + 0.00005,
            ),
            make_candle(
                ts=BASE_TS + 2 * BAR_MS,
                high=_SHORT_ENTRY + 0.00010,
                low=_SHORT_TP2 - 0.00100,
                close=_SHORT_TP2 - 0.00050,
            ),
        )

    def test_evaluate_close_price(self):
        svc, _ = make_service()
        signal = make_short_signal()
        for c in self._candles():
            eval_signal(svc, signal, c, c.timestamp)

        assert signal.status == SignalStatus.TP2_HIT
        assert signal.close_price == _SHORT_TP2

    def test_simulate_close_price(self):
        svc, _ = make_service()
        probe = simulate(svc, make_short_signal(), list(self._candles()))

        assert probe.status == SignalStatus.TP2_HIT
        assert probe.close_price == _SHORT_TP2


# ═══════════════════════════════════════════════════════════════════════════════
# Neutral candles
# ═══════════════════════════════════════════════════════════════════════════════


class TestNeutralCandles:
    """Candles that touch nothing — signal stays TRIGGERED."""

    def test_neutral_bar_keeps_signal_open(self):
        svc, _ = make_service()
        signal = make_signal()
        eval_signal(
            svc,
            signal,
            make_candle(
                ts=BASE_TS + BAR_MS,
                high=ENTRY + 0.00010,
                low=ENTRY - 0.00010,
                close=ENTRY,
            ),
            BASE_TS + BAR_MS,
        )

        assert signal.status == SignalStatus.TRIGGERED
        assert signal.outcome is None

    def test_simulate_multiple_neutral_then_sl(self):
        svc, _ = make_service()
        neutrals = [
            make_candle(
                ts=BASE_TS + i * BAR_MS,
                high=ENTRY + 0.00010,
                low=ENTRY - 0.00010,
                close=ENTRY,
            )
            for i in range(1, 5)
        ]
        sl_bar = make_candle(
            ts=BASE_TS + 5 * BAR_MS,
            high=ENTRY + 0.00010,
            low=SL - 0.00020,
            close=1.09850,
        )
        probe = simulate(svc, make_signal(), neutrals + [sl_bar])

        assert probe.status == SignalStatus.SL_HIT
        assert probe.close_price == SL

    def test_simulate_empty_candles_stays_triggered(self):
        svc, _ = make_service()
        probe = simulate(svc, make_signal(), [])

        assert probe.status == SignalStatus.TRIGGERED
        assert probe.outcome is None
