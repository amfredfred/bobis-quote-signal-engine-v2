from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app.backtesting.backtest import BacktestReport, BacktestResult
from app.engine.decision_engine import DecisionEngine
from app.engine.market_replay import replay_signal_lifecycle
from app.engine.parity_trace import trace_from_signal
from domain.assets.profiles import AssetProfile
from domain.entities.candle import Candle
from domain.entities.enums import BosDirection, CandlePattern, SignalDirection, SignalOutcome
from domain.entities.ranges import HtfRange, LtfRange, RejectionCandle
from domain.entities.trade import TradeSignal
from tools.parity_check import compare_traces


TS = 1_780_000_000_000


def _profile() -> AssetProfile:
    return AssetProfile(
        min_rr=1.0,
        max_rr=9.0,
        use_session_filter=False,
        sessions={},
        stop_placement="wick",
        stop_buffer_pct=0.0,
        max_sl_zone_mult=10.0,
        tp1_trigger_pct=50.0,
        tp1_close_pct=0.0,
        move_sl_to_be_on_tp1=True,
        use_invalidation=False,
        signal_expiry_hours=24.0,
        use_trend_filter=False,
        htf_lookback=10,
        multi_tf_independent_positions=False,
    )


def _setup(direction: SignalDirection) -> tuple[HtfRange, LtfRange, RejectionCandle]:
    if direction == SignalDirection.SHORT:
        htf = HtfRange(106.0, 94.0, BosDirection.BEARISH, TS - 10_000, tp_level=90.0)
        ltf = LtfRange(105.0, 99.0, TS - 5_000, SignalDirection.SHORT)
        rejection = RejectionCandle(101.0, 105.0, 99.0, 100.0, TS, 0.8, CandlePattern.SHOOTING_STAR)
    else:
        htf = HtfRange(106.0, 94.0, BosDirection.BULLISH, TS - 10_000, tp_level=110.0)
        ltf = LtfRange(101.0, 95.0, TS - 5_000, SignalDirection.LONG)
        rejection = RejectionCandle(99.0, 101.0, 95.0, 100.0, TS, 0.8, CandlePattern.HAMMER)
    return htf, ltf, rejection


def _signal(direction: SignalDirection) -> TradeSignal:
    htf, ltf, rejection = _setup(direction)
    decision = DecisionEngine().evaluate_setup(
        symbol="XAUUSD",
        htf_interval="1h",
        ltf_interval="5min",
        htf_range=htf,
        ltf_range=ltf,
        rejection=rejection,
        signal_id=f"sig-{direction.value}",
        profile=_profile(),
    )
    assert decision.signal is not None
    return decision.signal


def _result(signal: TradeSignal, outcome: SignalOutcome, rr: float) -> BacktestResult:
    return BacktestResult(signal, outcome, rr, TS + 60_000, signal.tp2)


def _report(result: BacktestResult) -> BacktestReport:
    registry = MagicMock()
    registry.get.return_value = _profile()
    with patch("app.backtesting.backtest.AssetRegistry", return_value=registry):
        return BacktestReport(
            symbol="XAUUSD",
            results=[result],
            cfg=MagicMock(),
            start_balance=5_000.0,
            risk_percent=1.0,
        )


def test_live_backtest_same_sell_signal_same_data():
    signal = _signal(SignalDirection.SHORT)
    candles = [Candle(TS + 60_000, 101.0, 102.0, 90.0, 91.0)]

    live = replay_signal_lifecycle(signal, candles, _profile())
    backtest = replay_signal_lifecycle(signal, candles, _profile())

    assert live.direction == backtest.direction == SignalDirection.SHORT
    assert live.outcome == backtest.outcome == SignalOutcome.WIN_FULL
    assert live.entry_price == pytest.approx(backtest.entry_price)
    assert live.stop_loss == pytest.approx(backtest.stop_loss)
    assert live.tp2 == pytest.approx(backtest.tp2)


def test_live_backtest_same_buy_signal_same_data():
    signal = _signal(SignalDirection.LONG)
    candles = [Candle(TS + 60_000, 99.0, 110.0, 98.0, 109.0)]

    live = replay_signal_lifecycle(signal, candles, _profile())
    backtest = replay_signal_lifecycle(signal, candles, _profile())

    assert live.direction == backtest.direction == SignalDirection.LONG
    assert live.outcome == backtest.outcome == SignalOutcome.WIN_FULL
    assert live.entry_price == pytest.approx(backtest.entry_price)
    assert live.stop_loss == pytest.approx(backtest.stop_loss)
    assert live.tp2 == pytest.approx(backtest.tp2)


def test_live_backtest_same_no_trade_decision_same_data():
    decision = DecisionEngine().evaluate_setup(
        symbol="XAUUSD",
        htf_interval="1h",
        ltf_interval="5min",
        htf_range=_setup(SignalDirection.SHORT)[0],
        ltf_range=_setup(SignalDirection.SHORT)[1],
        rejection=_setup(SignalDirection.SHORT)[2],
        signal_id="blocked",
        profile=_profile(),
        blocked_reason="POSITION OPEN on XAUUSD SHORT",
    )

    assert decision.signal is None
    assert decision.decision_reason == "blocked"
    assert decision.blocked_reason == "POSITION OPEN on XAUUSD SHORT"


def test_live_backtest_same_risk_calculation():
    signal = _signal(SignalDirection.LONG)
    live = trace_from_signal(
        mode="live", signal=signal, cfg={"a": 1}, account_balance=5_000.0, risk_percent=1.0
    )
    backtest = trace_from_signal(
        mode="backtest", signal=signal, cfg={"a": 1}, account_balance=5_000.0, risk_percent=1.0
    )

    assert live.risk_amount == pytest.approx(50.0)
    assert live.risk_amount == pytest.approx(backtest.risk_amount)


def test_live_backtest_same_daily_budget_calculation():
    signal = _signal(SignalDirection.SHORT)
    live = trace_from_signal(
        mode="live", signal=signal, cfg={"a": 1}, account_balance=5_000.0, risk_percent=1.0
    )
    backtest = trace_from_signal(
        mode="backtest", signal=signal, cfg={"a": 1}, account_balance=5_000.0, risk_percent=1.0
    )

    assert live.daily_budget == pytest.approx(backtest.daily_budget)


def test_live_backtest_trace_diff_fails_on_discrepancy():
    signal = _signal(SignalDirection.SHORT)
    live = trace_from_signal(mode="live", signal=signal, cfg={"same": True}).__dict__
    backtest = trace_from_signal(mode="backtest", signal=signal, cfg={"same": True}).__dict__
    backtest["signal"] = "NONE"

    diff = compare_traces([live], [backtest])

    assert diff is not None
    assert "PARITY FAILED" in diff
    assert "signal" in diff
