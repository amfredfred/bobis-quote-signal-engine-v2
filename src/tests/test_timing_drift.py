from __future__ import annotations

import asyncio
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.backtesting.backtest import BacktestResult, MultiPairBacktester
from domain.assets.profiles import AssetProfile
from domain.entities.enums import BosDirection, CandlePattern, SignalDirection, SignalOutcome
from domain.entities.ranges import HtfRange, LtfRange, RejectionCandle
from domain.signals.builder import build_signal
from interfaces.cli.main import SignalEngine


BASE = 1_700_000_000_000
M5 = 5 * 60 * 1000


def test_scheduler_passes_analysis_close_without_double_subtract() -> None:
    cfg = _SchedulerCfg(now=BASE + M5 + 1_000)
    service = _Service()
    fake_engine = type("FakeEngine", (), {"_cfg": cfg, "_service": service})()
    expected_analysis_close = (cfg.now_ms() // M5) * M5

    asyncio.run(SignalEngine._on_candle_close(fake_engine, "XAUUSD"))

    assert service.analyze_calls == [("XAUUSD", expected_analysis_close)]
    assert service.update_calls == ["XAUUSD"]


def test_builder_sets_triggered_at_to_actionable_candle_close() -> None:
    signal = _signal()

    assert signal.setup_candle_open_at == BASE
    assert signal.setup_candle_close_at == BASE + M5
    assert signal.created_at == BASE
    assert signal.triggered_at == BASE + M5
    assert signal.to_dict()["setupCandleCloseAt"] == BASE + M5


def test_backtest_print_separates_setup_actionable_entry_and_never_closed_open(capsys) -> None:
    signal = _signal()
    result = BacktestResult(
        signal=signal,
        outcome=SignalOutcome.EXPIRED,
        realized_rr=0.0,
        close_ts=None,
        close_px=None,
    )
    bt = object.__new__(MultiPairBacktester)
    bt.cfg = _PrintCfg()
    bt.spread_points = 0

    MultiPairBacktester._print_result(bt, result)

    out = re.sub(r"\x1b\[[0-9;]*m", "", capsys.readouterr().out)
    assert "S=2023-11-14 22:13:20" in out
    assert "A=2023-11-14 22:18:20" in out
    assert "E@2023-11-14 22:18:20" in out
    assert "closed OPEN" not in out
    assert "closed EXPIRED" in out


class _SchedulerCfg:
    tf_pairs = (("5min", "5min"),)

    def __init__(self, now: int) -> None:
        self._now = now

    def now_ms(self) -> int:
        return self._now

    def dt_ms(self, ms: int) -> str:
        return str(ms)


class _Service:
    def __init__(self) -> None:
        self.analyze_calls = []
        self.update_calls = []

    async def analyze(self, symbol: str, fired_at: int):
        self.analyze_calls.append((symbol, fired_at))

    async def update_watchlist(self, symbol: str):
        self.update_calls.append(symbol)


class _PrintCfg:
    def dt_ms(self, ms: int) -> str:
        import datetime

        return datetime.datetime.fromtimestamp(
            ms / 1000, tz=datetime.timezone.utc
        ).strftime("%Y-%m-%d %H:%M:%S")


def _signal():
    return build_signal(
        symbol="XAUUSD",
        htf_interval="5min",
        ltf_interval="5min",
        htf_range=HtfRange(
            range_high=101.0,
            range_low=99.0,
            bos_direction=BosDirection.BULLISH,
            timestamp=BASE - M5,
            broken_at=BASE - M5,
            tp_level=103.0,
        ),
        ltf_range=LtfRange(
            range_high=101.0,
            range_low=99.0,
            timestamp=BASE,
            direction=SignalDirection.LONG,
        ),
        rejection=RejectionCandle(
            open=100.0,
            high=101.0,
            low=99.0,
            close=100.0,
            timestamp=BASE,
            wick_ratio=0.5,
            pattern=CandlePattern.CRT_BUY,
        ),
        signal_id="sig",
        profile=AssetProfile(
            min_rr=1.0,
            max_rr=0.0,
            use_session_filter=False,
            sessions={},
            stop_placement="range",
            stop_buffer_pct=0.0,
            max_sl_zone_mult=10.0,
            tp1_multiplier=0.5,
            use_breakeven=True,
            use_invalidation=False,
            signal_expiry_hours=1.0,
            use_trend_filter=False,
            htf_lookback=120,
            multi_tf_independent_positions=False,
        ),
    )
