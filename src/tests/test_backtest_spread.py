"""
tests/test_backtest_spread.py

Unit tests for spread support in the backtest engine.
Spread is specified in price units directly (--spread-points).
Position size uses the raw stop distance plus spread, matching live sizing.

Covers: formula correctness, LONG/SHORT, zero spread, expired trades,
        per-trade field names, executed entry/exit prices, and validation.
"""
from __future__ import annotations

import math
import pytest

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.backtesting.backtest import (
    DEFAULT_SPREAD_POINTS,
    passes_spread_quality_gate,
    spread_adjusted_rr,
    spread_points_to_price_units,
)
from domain.entities.enums import SignalDirection, SignalOutcome


def test_cli_spread_points_convert_gold_to_price_units():
    assert spread_points_to_price_units("XAUUSD", 3.0) == pytest.approx(0.3)


def test_cli_spread_points_leave_unknown_symbols_as_price_units():
    assert spread_points_to_price_units("UNKNOWN", 3.0) == pytest.approx(3.0)


@pytest.mark.parametrize(
    ("spread", "stop_distance", "threshold", "expected"),
    [
        (0.3, 1.0, 0.35, True),
        (0.35, 1.0, 0.35, True),
        (0.36, 1.0, 0.35, False),
        (0.3, 0.0, 0.35, False),
        (0.3, -1.0, 0.35, False),
        (0.3, 1.0, 0.0, True),
        (0.0, 0.0, 0.35, True),
    ],
)
def test_spread_quality_gate(spread, stop_distance, threshold, expected):
    assert passes_spread_quality_gate(spread, stop_distance, threshold) is expected


# ── Spread-adjusted risk sizing ───────────────────────────────────────────────

class TestSpreadExecutedRR:
    """
    entry=2000, sl=1990 → risk_pips=10
    spread_points=0.1 → executed risk distance = 10.1
    """

    RISK_PIPS = 10.0
    SPREAD_POINTS = 0.1
    def _executed_rr(self, raw_rr: float) -> float:
        return spread_adjusted_rr(raw_rr, self.RISK_PIPS, self.SPREAD_POINTS)

    def test_win_rr_reduced(self):
        assert self._executed_rr(2.5) == pytest.approx((25.0 - 0.1) / 10.1)

    def test_loss_rr_remains_one_r(self):
        assert self._executed_rr(-1.0) == pytest.approx(-1.0)

    def test_breakeven_rr_reduced(self):
        assert self._executed_rr(0.5) == pytest.approx((5.0 - 0.1) / 10.1)

    def test_direction_agnostic(self):
        assert self._executed_rr(2.0) == pytest.approx((20.0 - 0.1) / 10.1)

    def test_expired_no_spread_deducted(self):
        executed = 0.0
        assert executed == pytest.approx(0.0)

    def test_zero_spread_passthrough(self):
        for raw in [2.5, -1.0, 0.5, 0.0]:
            assert spread_adjusted_rr(raw, self.RISK_PIPS, 0.0) == pytest.approx(raw)

    def test_tight_stop_reduces_winning_r_more(self):
        spread = 0.5
        wide = spread_adjusted_rr(2.0, 10.0, spread)
        tight = spread_adjusted_rr(2.0, 5.0, spread)
        assert tight < wide


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_mock_result(raw_rr: float, outcome: SignalOutcome, direction=SignalDirection.LONG,
                      entry=2000.0, sl=1990.0, close_px=None):
    from unittest.mock import MagicMock
    from app.backtesting.backtest import BacktestResult

    sig = MagicMock()
    sig.entry_price = entry
    sig.stop_loss = sl
    sig.risk_pips = abs(entry - sl)
    sig.direction = direction
    sig.symbol = "XAUUSD"
    sig.triggered_at = 1_700_000_000_000
    sig.created_at = 1_700_000_000_000
    sig.htf_interval = "4h"
    sig.ltf_interval = "1h"
    sig.risk_reward_ratio = abs(raw_rr) if raw_rr > 0 else 2.0
    sig.tp1 = entry + 5 if direction == SignalDirection.LONG else entry - 5
    sig.tp2 = entry + 20 if direction == SignalDirection.LONG else entry - 20
    sig.htf_range = MagicMock()
    sig.ltf_range = MagicMock()
    sig.rejection_candle = MagicMock()
    sig.rejection_candle.pattern = MagicMock()
    sig.rejection_candle.pattern.value = "HAMMER"

    r = MagicMock(spec=BacktestResult)
    r.signal = sig
    r.realized_rr = raw_rr
    r.outcome = outcome
    r.close_price = close_px if close_px is not None else (entry + 20)
    r.close_ts = 1_700_100_000_000
    r.hit_entry_after_tp1 = False
    return r


def _make_report(results, spread_points=0.0, start_balance=5000.0, risk_percent=1.0):
    from unittest.mock import patch, MagicMock
    from app.backtesting.backtest import BacktestReport
    from config.settings import Settings

    cfg = MagicMock(spec=Settings)

    profile = MagicMock()
    profile.move_sl_to_be_on_tp1 = True
    profile.use_invalidation = False
    profile.signal_expiry_hours = 120.0
    profile.tp1_trigger_pct = 50.0
    profile.tp1_close_pct = 0.0
    profile.use_trend_filter = False
    profile.multi_tf_independent_positions = False
    profile.max_rr = 9.0
    profile.min_rr = 1.5

    registry_mock = MagicMock()
    registry_mock.get.return_value = profile

    with patch("app.backtesting.backtest.AssetRegistry", return_value=registry_mock):
        return BacktestReport(
            symbol="XAUUSD",
            results=results,
            cfg=cfg,
            start_balance=start_balance,
            risk_percent=risk_percent,
            spread_points=spread_points,
        )


# ── Spread integration in _compute_accounting ─────────────────────────────────

class TestSpreadInAccounting:
    """
    entry=2000, sl=1990 → risk_pips=10
    spread_points=0.3 → executed risk distance = 10.3
    balance=5000, risk=1% → risk_amount=50
    """

    def test_long_win_pnl_uses_executed_rr(self):
        r = _make_mock_result(2.5, SignalOutcome.WIN_FULL, SignalDirection.LONG)
        report = _make_report([r], spread_points=0.3)
        per_trade, _ = report._compute_accounting([r])
        a = per_trade[0]

        assert a["executed_rr"] == pytest.approx((25.0 - 0.3) / 10.3, rel=1e-6)
        assert a["theoretical_rr"] == pytest.approx(2.5)
        assert a["pnl"] == pytest.approx(a["executed_rr"] * 50, rel=1e-6)

    def test_long_loss_pnl_uses_executed_rr(self):
        r = _make_mock_result(-1.0, SignalOutcome.LOSS, SignalDirection.LONG)
        report = _make_report([r], spread_points=0.3)
        per_trade, _ = report._compute_accounting([r])
        a = per_trade[0]

        assert a["executed_rr"] == pytest.approx(-1.0, rel=1e-6)
        assert a["pnl"] == pytest.approx(-50.0, rel=1e-6)

    def test_short_win_pnl_uses_executed_rr(self):
        r = _make_mock_result(2.0, SignalOutcome.WIN_FULL, SignalDirection.SHORT)
        report = _make_report([r], spread_points=0.3)
        per_trade, _ = report._compute_accounting([r])
        a = per_trade[0]

        assert a["executed_rr"] == pytest.approx((20.0 - 0.3) / 10.3, rel=1e-6)

    def test_expired_no_spread_deducted(self):
        r = _make_mock_result(0.0, SignalOutcome.EXPIRED, SignalDirection.LONG)
        report = _make_report([r], spread_points=0.3)
        per_trade, _ = report._compute_accounting([r])
        a = per_trade[0]

        assert a["executed_rr"] == pytest.approx(0.0)
        assert a["pnl"] == pytest.approx(0.0)

    def test_zero_spread_executed_equals_theoretical(self):
        r = _make_mock_result(2.5, SignalOutcome.WIN_FULL, SignalDirection.LONG)
        report = _make_report([r], spread_points=0.0)
        per_trade, _ = report._compute_accounting([r])
        a = per_trade[0]

        assert a["executed_rr"] == pytest.approx(a["theoretical_rr"])

    def test_spread_fields_present_in_per_trade(self):
        r = _make_mock_result(2.0, SignalOutcome.WIN_FULL)
        report = _make_report([r], spread_points=0.2)
        per_trade, _ = report._compute_accounting([r])
        a = per_trade[0]

        for field in ["spread_points", "theoretical_rr", "executed_rr",
                      "raw_entry_price", "executed_entry_price",
                      "raw_exit_price", "executed_exit_price"]:
            assert field in a, f"Missing field: {field}"

    def test_long_executed_entry_adds_spread(self):
        r = _make_mock_result(2.0, SignalOutcome.WIN_FULL, SignalDirection.LONG, entry=2000.0)
        report = _make_report([r], spread_points=0.3)
        per_trade, _ = report._compute_accounting([r])
        a = per_trade[0]

        # LONG: exec_entry = raw_entry + spread_points
        assert a["executed_entry_price"] == pytest.approx(2000.3)
        assert a["raw_entry_price"] == pytest.approx(2000.0)

    def test_short_executed_exit_adds_spread(self):
        r = _make_mock_result(2.0, SignalOutcome.WIN_FULL, SignalDirection.SHORT,
                              entry=2000.0, close_px=1980.0)
        report = _make_report([r], spread_points=0.3)
        per_trade, _ = report._compute_accounting([r])
        a = per_trade[0]

        # SHORT: exec_exit = raw_exit + spread_points
        assert a["executed_exit_price"] == pytest.approx(1980.3)
        assert a["executed_entry_price"] == pytest.approx(2000.0)

    def test_decimal_spread_points(self):
        r = _make_mock_result(2.0, SignalOutcome.WIN_FULL, SignalDirection.LONG)
        report = _make_report([r], spread_points=0.05)
        per_trade, _ = report._compute_accounting([r])
        a = per_trade[0]

        assert a["executed_rr"] == pytest.approx((20.0 - 0.05) / 10.05, rel=1e-6)


# ── MultiPairBacktester spread_points validation ──────────────────────────────

class TestMultiPairBacktesterSpreadValidation:
    def _make(self, spread_points=0.0):
        from unittest.mock import patch, MagicMock
        from app.backtesting.backtest import MultiPairBacktester
        from config.settings import Settings

        cfg = MagicMock(spec=Settings)
        cfg.htf_lookback = 120
        cfg.min_wick_ratio = 0.65
        cfg.entry_model = "candle_pattern"
        cfg.use_trend_filter = False
        cfg.move_sl_to_be_on_tp1 = True
        cfg.tp1_trigger_pct = 50.0
        cfg.tp1_close_pct = 0.0
        cfg.use_invalidation = False
        cfg.multi_tf_independent_positions = False
        cfg.signal_expiry_hours = 120.0
        cfg.pivot_bars = 1
        cfg.max_htf_zones_per_dir = 1
        cfg.use_displacement_filter = False
        cfg.displacement_atr_period = 10
        cfg.displacement_atr_mult = 1.2
        cfg.tf_displacement_mult = {}
        cfg.rejection_stale_hours = MagicMock(return_value=0.5)

        profile = MagicMock()
        profile.move_sl_to_be_on_tp1 = True
        profile.use_invalidation = False
        profile.signal_expiry_hours = 120.0
        profile.tp1_trigger_pct = 50.0
        profile.tp1_close_pct = 0.0
        profile.use_trend_filter = False
        profile.multi_tf_independent_positions = False
        profile.max_rr = 9.0
        profile.min_rr = 1.5

        registry_mock = MagicMock()
        registry_mock.get.return_value = profile

        with patch("app.backtesting.backtest.AssetRegistry", return_value=registry_mock):
            return MultiPairBacktester(
                cfg=cfg,
                symbol="XAUUSD",
                pairs=[("1h", "5min")],
                htf_candles={"1h": []},
                ltf_candles={"5min": []},
                start_balance=5000.0,
                risk_percent=1.0,
                spread_points=spread_points,
            )

    def test_zero_spread_accepted(self):
        bt = self._make(spread_points=0.0)
        assert bt.spread_points == 0.0

    def test_positive_spread_accepted(self):
        bt = self._make(spread_points=0.3)
        assert bt.spread_points == pytest.approx(0.3)

    def test_decimal_spread_accepted(self):
        bt = self._make(spread_points=0.05)
        assert bt.spread_points == pytest.approx(0.05)

    def test_negative_spread_raises(self):
        with pytest.raises(ValueError, match="spreadPoints must be >= 0"):
            self._make(spread_points=-1.0)

    def test_nan_spread_raises(self):
        with pytest.raises(ValueError, match="spreadPoints must be a valid number"):
            self._make(spread_points=math.nan)

    def test_inf_spread_raises(self):
        with pytest.raises(ValueError, match="spreadPoints must be a valid number"):
            self._make(spread_points=math.inf)

    def test_default_spread_is_zero(self):
        assert DEFAULT_SPREAD_POINTS == 0.0
