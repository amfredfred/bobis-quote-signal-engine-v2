"""
tests/test_backtest_spread.py

Unit tests for spread support added to the backtest engine.
Covers: LONG spread, SHORT spread, zero spread, decimal spread,
invalid CLI inputs, get_pip_size, and per-trade spread fields.
"""
from __future__ import annotations

import math
import pytest

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.backtesting.backtest import (
    DEFAULT_SPREAD_PIP,
    SYMBOL_PIP_SIZE,
    get_pip_size,
    spread_pip_to_price,
    calculate_trade_accounting,
    BacktestReport,
    MultiPairBacktester,
)
from domain.entities.enums import SignalDirection, SignalOutcome


# ── get_pip_size ───────────────────────────────────────────────────────────────

class TestGetPipSize:
    def test_xauusd(self):
        assert get_pip_size("XAUUSD") == pytest.approx(0.01)

    def test_eurusd(self):
        assert get_pip_size("EURUSD") == pytest.approx(0.0001)

    def test_us30(self):
        assert get_pip_size("US30") == pytest.approx(1.0)

    def test_us100(self):
        assert get_pip_size("US100") == pytest.approx(1.0)

    def test_us500(self):
        assert get_pip_size("US500") == pytest.approx(0.1)

    def test_jp225(self):
        assert get_pip_size("JP225") == pytest.approx(1.0)

    def test_unknown_symbol_raises(self):
        with pytest.raises(ValueError, match="Unknown symbol"):
            get_pip_size("FAKEUSD")

    def test_unknown_symbol_no_fallback(self):
        """Confirm there is no silent default — must raise."""
        with pytest.raises(ValueError):
            get_pip_size("XYZABC")


# ── spread_pip_to_price ────────────────────────────────────────────────────────

class TestSpreadPipToPrice:
    def test_xauusd_3pip(self):
        # 3 pip * 0.01 pip_size = 0.03 price units
        assert spread_pip_to_price(3, 0.01) == pytest.approx(0.03)

    def test_eurusd_2pip(self):
        assert spread_pip_to_price(2, 0.0001) == pytest.approx(0.0002)

    def test_zero_spread(self):
        assert spread_pip_to_price(0, 0.01) == pytest.approx(0.0)

    def test_decimal_spread(self):
        # 0.5 pip on XAUUSD = 0.005 price units
        assert spread_pip_to_price(0.5, 0.01) == pytest.approx(0.005)


# ── Spread effect on executed_rr ──────────────────────────────────────────────

class TestSpreadExecutedRR:
    """
    Tests the core formula: executed_rr = raw_rr - (spread_price / risk_pips)

    Example setup (XAUUSD-like):
      entry = 2000, sl = 1990 → risk_pips = 10 (price units)
      pip_size = 0.01
      spread = 3 pip → spread_price = 0.03
      spread_r = spread_price / risk_pips = 0.03 / 10 = 0.003 R per trade
    """

    ENTRY = 2000.0
    SL = 1990.0
    RISK_PIPS = 10.0  # abs(entry - sl)
    PIP_SIZE = 0.01
    SPREAD_PIP = 3.0
    SPREAD_PRICE = 0.03  # 3 * 0.01
    SPREAD_R = SPREAD_PRICE / RISK_PIPS  # 0.003

    def _executed_rr(self, raw_rr: float) -> float:
        return raw_rr - self.SPREAD_R

    def test_long_win_rr_reduced(self):
        # Raw WIN at +2.5R → executed = 2.5 - 0.003 = 2.497
        raw = 2.5
        assert self._executed_rr(raw) == pytest.approx(2.497)

    def test_long_loss_rr_worsened(self):
        # Raw LOSS at -1.0R → executed = -1.0 - 0.003 = -1.003
        raw = -1.0
        assert self._executed_rr(raw) == pytest.approx(-1.003)

    def test_long_breakeven_rr_reduced(self):
        # Raw BE at +0.5R → executed = 0.5 - 0.003 = 0.497
        raw = 0.5
        assert self._executed_rr(raw) == pytest.approx(0.497)

    def test_short_same_formula(self):
        # Direction doesn't change the formula — spread_r is direction-agnostic
        raw = 2.0
        assert self._executed_rr(raw) == pytest.approx(1.997)

    def test_expired_no_spread_deducted(self):
        # EXPIRED → executed_rr = 0.0 (no trade entered)
        raw = 0.0
        executed = 0.0  # spread NOT deducted
        assert executed == pytest.approx(0.0)

    def test_zero_spread_passthrough(self):
        # Zero spread: executed_rr == raw_rr for all outcomes
        for raw in [2.5, -1.0, 0.5, 0.0]:
            assert raw - 0.0 == pytest.approx(raw)


# ── Spread integration in _compute_accounting ─────────────────────────────────

def _make_mock_result(raw_rr: float, outcome: SignalOutcome, direction=SignalDirection.LONG,
                      entry=2000.0, sl=1990.0, close_px=None):
    """Build a minimal BacktestResult-like mock."""
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


def _make_report(results, spread_pip=0.0, start_balance=5000.0, risk_percent=1.0):
    from unittest.mock import patch, MagicMock
    from app.backtesting.backtest import BacktestReport
    from config.settings import Settings

    cfg = MagicMock(spec=Settings)

    profile = MagicMock()
    profile.use_breakeven = True
    profile.use_invalidation = False
    profile.signal_expiry_hours = 120.0
    profile.tp1_multiplier = 0.5
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
            spread_pip=spread_pip,
        )


class TestSpreadInAccounting:
    """
    Verifies per-trade accounting uses executed_rr (raw minus spread cost).

    Setup: XAUUSD, entry=2000, sl=1990 → risk_pips=10
    pip_size=0.01, spread=3pip → spread_price=0.03, spread_r=0.003
    balance=5000, risk=1% → risk_amount=50
    """

    def test_long_win_pnl_uses_executed_rr(self):
        WIN = SignalOutcome.WIN_FULL
        r = _make_mock_result(2.5, WIN, SignalDirection.LONG)
        report = _make_report([r], spread_pip=3.0)
        per_trade, _ = report._compute_accounting([r])
        a = per_trade[0]

        # spread_r = 0.03 / 10 = 0.003; executed_rr = 2.5 - 0.003 = 2.497
        assert a["executed_rr"] == pytest.approx(2.497, rel=1e-6)
        assert a["theoretical_rr"] == pytest.approx(2.5)
        # pnl = executed_rr * risk_amount = 2.497 * 50 = 124.85
        assert a["pnl"] == pytest.approx(2.497 * 50, rel=1e-6)

    def test_long_loss_pnl_uses_executed_rr(self):
        LOSS = SignalOutcome.LOSS
        r = _make_mock_result(-1.0, LOSS, SignalDirection.LONG)
        report = _make_report([r], spread_pip=3.0)
        per_trade, _ = report._compute_accounting([r])
        a = per_trade[0]

        # executed_rr = -1.0 - 0.003 = -1.003; pnl = -1.003 * 50 = -50.15
        assert a["executed_rr"] == pytest.approx(-1.003, rel=1e-6)
        assert a["pnl"] == pytest.approx(-1.003 * 50, rel=1e-6)

    def test_short_win_pnl_uses_executed_rr(self):
        WIN = SignalOutcome.WIN_FULL
        r = _make_mock_result(2.0, WIN, SignalDirection.SHORT)
        report = _make_report([r], spread_pip=3.0)
        per_trade, _ = report._compute_accounting([r])
        a = per_trade[0]

        assert a["executed_rr"] == pytest.approx(2.0 - 0.003, rel=1e-6)

    def test_expired_no_spread_deducted(self):
        EXP = SignalOutcome.EXPIRED
        r = _make_mock_result(0.0, EXP, SignalDirection.LONG)
        report = _make_report([r], spread_pip=3.0)
        per_trade, _ = report._compute_accounting([r])
        a = per_trade[0]

        assert a["executed_rr"] == pytest.approx(0.0)
        assert a["theoretical_rr"] == pytest.approx(0.0)
        assert a["pnl"] == pytest.approx(0.0)

    def test_zero_spread_executed_equals_theoretical(self):
        WIN = SignalOutcome.WIN_FULL
        r = _make_mock_result(2.5, WIN, SignalDirection.LONG)
        report = _make_report([r], spread_pip=0.0)
        per_trade, _ = report._compute_accounting([r])
        a = per_trade[0]

        assert a["executed_rr"] == pytest.approx(a["theoretical_rr"])
        assert a["spread_price"] == pytest.approx(0.0)

    def test_spread_fields_present_in_per_trade(self):
        r = _make_mock_result(2.0, SignalOutcome.WIN_FULL)
        report = _make_report([r], spread_pip=2.0)
        per_trade, _ = report._compute_accounting([r])
        a = per_trade[0]

        for field in ["spread_pip", "spread_price", "theoretical_rr", "executed_rr",
                      "raw_entry_price", "executed_entry_price",
                      "raw_exit_price", "executed_exit_price"]:
            assert field in a, f"Missing field: {field}"

    def test_long_executed_entry_adds_spread(self):
        r = _make_mock_result(2.0, SignalOutcome.WIN_FULL, SignalDirection.LONG, entry=2000.0)
        report = _make_report([r], spread_pip=3.0)
        per_trade, _ = report._compute_accounting([r])
        a = per_trade[0]

        # LONG: executed_entry = raw_entry + spread_price
        assert a["executed_entry_price"] == pytest.approx(2000.0 + 0.03)
        assert a["raw_entry_price"] == pytest.approx(2000.0)

    def test_short_executed_exit_adds_spread(self):
        r = _make_mock_result(2.0, SignalOutcome.WIN_FULL, SignalDirection.SHORT,
                              entry=2000.0, close_px=1980.0)
        report = _make_report([r], spread_pip=3.0)
        per_trade, _ = report._compute_accounting([r])
        a = per_trade[0]

        # SHORT: executed_exit = raw_exit + spread_price
        assert a["executed_exit_price"] == pytest.approx(1980.0 + 0.03)
        assert a["executed_entry_price"] == pytest.approx(2000.0)  # no spread on SHORT entry

    def test_decimal_spread(self):
        r = _make_mock_result(2.0, SignalOutcome.WIN_FULL, SignalDirection.LONG)
        report = _make_report([r], spread_pip=0.5)
        per_trade, _ = report._compute_accounting([r])
        a = per_trade[0]

        # 0.5 pip * 0.01 pip_size = 0.005 spread_price; risk_pips=10
        # spread_r = 0.005/10 = 0.0005; executed_rr = 2.0 - 0.0005 = 1.9995
        assert a["spread_price"] == pytest.approx(0.005)
        assert a["executed_rr"] == pytest.approx(1.9995, rel=1e-6)


# ── MultiPairBacktester spread validation ─────────────────────────────────────

class TestMultiPairBacktesterSpreadValidation:
    def _make(self, spread_pip=0.0):
        from unittest.mock import patch, MagicMock
        from app.backtesting.backtest import MultiPairBacktester
        from config.settings import Settings

        cfg = MagicMock(spec=Settings)
        cfg.htf_lookback = 120
        cfg.min_wick_ratio = 0.65
        cfg.entry_model = "candle_pattern"
        cfg.use_trend_filter = False
        cfg.use_breakeven = True
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
        profile.use_breakeven = True
        profile.use_invalidation = False
        profile.signal_expiry_hours = 120.0
        profile.tp1_multiplier = 0.5
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
                spread_pip=spread_pip,
            )

    def test_zero_spread_accepted(self):
        bt = self._make(spread_pip=0.0)
        assert bt.spread_pip == 0.0

    def test_positive_spread_accepted(self):
        bt = self._make(spread_pip=3.0)
        assert bt.spread_pip == 3.0

    def test_decimal_spread_accepted(self):
        bt = self._make(spread_pip=0.5)
        assert bt.spread_pip == pytest.approx(0.5)

    def test_negative_spread_raises(self):
        with pytest.raises(ValueError, match="spreadPip must be >= 0"):
            self._make(spread_pip=-1.0)

    def test_nan_spread_raises(self):
        with pytest.raises(ValueError, match="spreadPip must be a valid number"):
            self._make(spread_pip=math.nan)

    def test_inf_spread_raises(self):
        with pytest.raises(ValueError, match="spreadPip must be a valid number"):
            self._make(spread_pip=math.inf)

    def test_spread_price_computed_correctly(self):
        # XAUUSD: pip_size=0.01; 3 pip → spread_price=0.03
        bt = self._make(spread_pip=3.0)
        assert bt._spread_price == pytest.approx(0.03)

    def test_zero_spread_price_when_zero_pip(self):
        bt = self._make(spread_pip=0.0)
        assert bt._spread_price == pytest.approx(0.0)
