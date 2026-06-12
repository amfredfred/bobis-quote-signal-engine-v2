"""
tests/test_backtest_accounting.py

Unit tests for the account simulation layer added to the backtest engine.
Covers: calculate_trade_accounting, defaults, validation, compounding,
drawdown, and BacktestReport._compute_accounting.
"""
from __future__ import annotations

import math
import pytest

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.backtesting.backtest import (
    DEFAULT_START_BALANCE,
    DEFAULT_RISK_PERCENT,
    calculate_trade_accounting,
)


# ── calculate_trade_accounting ─────────────────────────────────────────────────

class TestCalculateTradeAccounting:
    def _calc(self, balance_before, result_r, risk_percent=1.0, peak_before=None):
        if peak_before is None:
            peak_before = balance_before
        return calculate_trade_accounting(
            balance_before=balance_before,
            result_r=result_r,
            risk_percent=risk_percent,
            peak_balance_before=peak_before,
        )

    def test_loss_reduces_balance(self):
        a = self._calc(5000, -1.0)
        assert a["risk_amount"] == pytest.approx(50.0)
        assert a["pnl"] == pytest.approx(-50.0)
        assert a["balance_after"] == pytest.approx(4950.0)

    def test_win_increases_balance(self):
        a = self._calc(5000, 2.0)
        assert a["risk_amount"] == pytest.approx(50.0)
        assert a["pnl"] == pytest.approx(100.0)
        assert a["balance_after"] == pytest.approx(5100.0)

    def test_compounding_risk_after_loss(self):
        # Trade 1: balance 5000, -1R
        a1 = self._calc(5000, -1.0)
        assert a1["balance_after"] == pytest.approx(4950.0)
        # Trade 2: balance 4950, +2R — risk based on new balance
        a2 = self._calc(4950, 2.0, peak_before=5000)
        assert a2["risk_amount"] == pytest.approx(49.50)
        assert a2["pnl"] == pytest.approx(99.0)
        assert a2["balance_after"] == pytest.approx(5049.0)

    def test_drawdown_after_loss(self):
        # Peak at 5000, balance drops to 4950
        a = self._calc(5000, -1.0, peak_before=5000)
        assert a["drawdown_after"] == pytest.approx(50.0)
        assert a["drawdown_pct_after"] == pytest.approx(1.0)

    def test_no_drawdown_on_win(self):
        a = self._calc(5000, 2.0, peak_before=5000)
        assert a["drawdown_after"] == pytest.approx(0.0)
        assert a["drawdown_pct_after"] == pytest.approx(0.0)

    def test_peak_updates_on_win(self):
        a = self._calc(5000, 2.0, peak_before=5000)
        assert a["peak_balance_after"] == pytest.approx(5100.0)

    def test_breakeven_zero_pnl(self):
        a = self._calc(5000, 0.0)
        assert a["pnl"] == pytest.approx(0.0)
        assert a["balance_after"] == pytest.approx(5000.0)

    def test_half_percent_risk(self):
        a = self._calc(5000, -1.0, risk_percent=0.5)
        assert a["risk_amount"] == pytest.approx(25.0)
        assert a["pnl"] == pytest.approx(-25.0)
        assert a["balance_after"] == pytest.approx(4975.0)


# ── Defaults ───────────────────────────────────────────────────────────────────

def test_default_start_balance():
    assert DEFAULT_START_BALANCE == 5000.0


def test_default_risk_percent():
    assert DEFAULT_RISK_PERCENT == 1.0


# ── MultiPairBacktester validation ────────────────────────────────────────────

class TestMultiPairBacktesterValidation:
    """Validation in __init__ must reject bad inputs immediately."""

    def _minimal_kwargs(self, **overrides):
        from unittest.mock import MagicMock
        from config.settings import Settings

        cfg = MagicMock(spec=Settings)
        cfg.htf_lookback = 120
        cfg.min_wick_ratio = 0.65
        cfg.entry_model = "crt"
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
        # AssetRegistry lookup
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

        defaults = dict(
            cfg=cfg,
            symbol="XAUUSD",
            pairs=[("1h", "5min")],
            htf_candles={"1h": []},
            ltf_candles={"5min": []},
            start_balance=5000.0,
            risk_percent=1.0,
        )
        defaults.update(overrides)
        return defaults

    def _make(self, **kwargs):
        from app.backtesting.backtest import MultiPairBacktester
        from unittest.mock import patch, MagicMock

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
            return MultiPairBacktester(**self._minimal_kwargs(**kwargs))

    def test_zero_start_balance_raises(self):
        with pytest.raises(ValueError, match="startBalance must be greater than 0"):
            self._make(start_balance=0)

    def test_negative_start_balance_raises(self):
        with pytest.raises(ValueError, match="startBalance must be greater than 0"):
            self._make(start_balance=-100)

    def test_infinite_start_balance_raises(self):
        with pytest.raises(ValueError, match="startBalance must be a valid number"):
            self._make(start_balance=math.inf)

    def test_nan_start_balance_raises(self):
        with pytest.raises(ValueError, match="startBalance must be a valid number"):
            self._make(start_balance=math.nan)

    def test_zero_risk_percent_raises(self):
        with pytest.raises(ValueError, match="riskPercent must be greater than 0"):
            self._make(risk_percent=0)

    def test_negative_risk_percent_raises(self):
        with pytest.raises(ValueError, match="riskPercent must be greater than 0"):
            self._make(risk_percent=-1)

    def test_over_100_risk_percent_raises(self):
        with pytest.raises(ValueError, match="riskPercent must be greater than 0 and less than or equal to 100"):
            self._make(risk_percent=101)

    def test_inf_risk_percent_raises(self):
        with pytest.raises(ValueError, match="riskPercent must be a valid number"):
            self._make(risk_percent=math.inf)

    def test_valid_inputs_accepted(self):
        # Should not raise
        bt = self._make(start_balance=10000, risk_percent=0.5)
        assert bt.start_balance == 10000
        assert bt.risk_percent == 0.5

    def test_default_start_balance_used(self):
        bt = self._make()
        assert bt.start_balance == DEFAULT_START_BALANCE

    def test_default_risk_percent_used(self):
        bt = self._make()
        assert bt.risk_percent == DEFAULT_RISK_PERCENT


# ── Full sequence: expected example from spec ─────────────────────────────────

def test_spec_example_sequence():
    """
    Given: startBalance=5000, riskPercent=1, results=[-1, 2, -1, 3]
    Verify each trade's balance matches the spec exactly.
    """
    balance = 5000.0
    peak = 5000.0
    results_r = [-1.0, 2.0, -1.0, 3.0]
    expected_balances = [4950.0, 5049.0, 4998.51, 5148.47]

    for i, r in enumerate(results_r):
        a = calculate_trade_accounting(
            balance_before=balance,
            result_r=r,
            risk_percent=1.0,
            peak_balance_before=peak,
        )
        assert a["balance_after"] == pytest.approx(expected_balances[i], abs=0.01)
        balance = a["balance_after"]
        peak = a["peak_balance_after"]

    # Final summary
    net_pnl = balance - 5000.0
    assert net_pnl == pytest.approx(148.47, abs=0.01)
    net_pnl_pct = net_pnl / 5000.0 * 100
    assert net_pnl_pct == pytest.approx(2.97, abs=0.01)
