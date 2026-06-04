from __future__ import annotations

import math
from types import SimpleNamespace

import pytest

from app.backtesting.backtest import (
    DEFAULT_TRAILING_GIVEBACK_PCT,
    MultiPairBacktester,
)
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


def test_default_trailing_giveback_pct_is_disabled() -> None:
    assert DEFAULT_TRAILING_GIVEBACK_PCT == 0.0


def test_long_giveback_trailing_locks_profit_after_tp1() -> None:
    bt = _backtester(50.0)
    signal = _signal(SignalDirection.LONG)
    result = bt._simulate_with_giveback_trailing(
        signal,
        [
            _candle(1, high=130.0, low=101.0, close=129.0),
            _candle(2, high=150.0, low=128.0, close=146.0),
            _candle(3, high=142.0, low=124.0, close=126.0),
        ],
        _profile(),
    )

    assert result.outcome == SignalOutcome.BREAKEVEN
    assert result.close_price == pytest.approx(125.0)
    assert result.realized_rr == pytest.approx(2.5)
    assert result.trail_mfe_price == pytest.approx(150.0)
    assert result.trailed_sl == pytest.approx(125.0)


def test_short_giveback_trailing_locks_profit_after_tp1() -> None:
    bt = _backtester(50.0)
    signal = _signal(SignalDirection.SHORT)
    result = bt._simulate_with_giveback_trailing(
        signal,
        [
            _candle(1, high=199.0, low=170.0, close=171.0),
            _candle(2, high=172.0, low=150.0, close=154.0),
            _candle(3, high=176.0, low=158.0, close=174.0),
        ],
        _profile(),
    )

    assert result.outcome == SignalOutcome.BREAKEVEN
    assert result.close_price == pytest.approx(175.0)
    assert result.realized_rr == pytest.approx(2.5)
    assert result.trail_mfe_price == pytest.approx(150.0)
    assert result.trailed_sl == pytest.approx(175.0)


def test_disabled_backtester_uses_existing_replay_path() -> None:
    bt = _backtester(0.0)
    signal = _signal(SignalDirection.LONG)
    future_np = bt._candles_to_np(
        [
            _candle(1, high=120.0, low=101.0, close=119.0),
            _candle(2, high=140.0, low=118.0, close=136.0),
            _candle(3, high=132.0, low=90.0, close=91.0),
        ]
    )

    result = bt._simulate(signal, future_np)

    assert result.trailing_giveback_pct == 0.0
    assert result.trail_mfe_price is None
    assert result.trailed_sl is None


@pytest.mark.parametrize("pct", [-1.0, 100.0, math.inf, math.nan])
def test_constructor_rejects_invalid_trailing_giveback_pct(pct: float) -> None:
    with pytest.raises(ValueError, match="trailingGivebackPct"):
        MultiPairBacktester(
            cfg=_cfg(),
            symbol="XAUUSD",
            pairs=[("1h", "5min")],
            htf_candles={"1h": []},
            ltf_candles={"5min": []},
            trailing_giveback_pct=pct,
        )


def _backtester(trailing_giveback_pct: float) -> MultiPairBacktester:
    bt = object.__new__(MultiPairBacktester)
    bt.trailing_giveback_pct = trailing_giveback_pct
    bt._registry = SimpleNamespace(get=lambda *_args: _profile())
    return bt


def _profile() -> SimpleNamespace:
    return SimpleNamespace(
        move_sl_to_be_on_tp1=True,
        use_invalidation=False,
        signal_expiry_hours=120.0,
        tp1_trigger_pct=50.0,
        tp1_close_pct=0.0,
    )


def _cfg() -> SimpleNamespace:
    return SimpleNamespace(
        htf_lookback=120,
        entry_model="candle_pattern",
        rejection_stale_hours=lambda _ltf: 0.5,
        pivot_bars=1,
        max_htf_zones_per_dir=1,
        use_displacement_filter=False,
        displacement_atr_period=10,
        displacement_atr_mult=1.2,
        tf_displacement_mult={},
    )


def _signal(direction: SignalDirection) -> TradeSignal:
    is_short = direction == SignalDirection.SHORT
    entry = 200.0 if is_short else 100.0
    stop = 210.0 if is_short else 90.0
    tp1 = 170.0 if is_short else 130.0
    tp2 = 140.0 if is_short else 160.0
    ltf_range = LtfRange(
        range_high=230.0 if is_short else 130.0,
        range_low=170.0 if is_short else 70.0,
        timestamp=0,
        direction=direction,
    )
    return TradeSignal(
        id="sig",
        symbol="XAUUSD",
        direction=direction,
        status=SignalStatus.TRIGGERED,
        entry_price=entry,
        stop_loss=stop,
        tp1=tp1,
        tp2=tp2,
        htf_range=HtfRange(
            range_high=230.0,
            range_low=70.0,
            bos_direction=BosDirection.BEARISH if is_short else BosDirection.BULLISH,
            timestamp=0,
            tp_level=tp2,
        ),
        ltf_range=ltf_range,
        rejection_candle=RejectionCandle(
            open=entry,
            high=entry + 1,
            low=entry - 1,
            close=entry,
            timestamp=0,
            wick_ratio=0.8,
            pattern=CandlePattern.SHOOTING_STAR if is_short else CandlePattern.HAMMER,
        ),
        risk_reward_ratio=6.0,
        risk_pips=10.0,
        created_at=0,
        triggered_at=0,
        setup_candle_open_at=0,
        setup_candle_close_at=0,
    )


def _candle(ts: int, high: float, low: float, close: float) -> Candle:
    return Candle(
        timestamp=ts,
        open=close,
        high=high,
        low=low,
        close=close,
        volume=1.0,
    )
