from __future__ import annotations

import asyncio
import datetime
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.backtesting.backtest import BacktestResult, MultiPairBacktester
from domain.assets.profiles import AssetProfile
from domain.entities.enums import BosDirection, CandlePattern, SignalDirection, SignalOutcome
from domain.entities.ranges import HtfRange, RejectionCandle
from domain.signals.builder import build_signal
from interfaces.cli.main import SignalEngine
from interfaces.ws.scheduler import SignalScheduler


BASE = 1_700_000_000_000
M5 = 5 * 60 * 1000


def test_scheduler_passes_analysis_close_without_double_subtract() -> None:
    cfg = _SchedulerCfg(now=BASE + M5 + 1_000)
    service = _Service()
    md = _MarketDataClock(now=BASE + M5 + 1_000)
    fake_engine = type(
        "FakeEngine",
        (),
        {"_cfg": cfg, "_md": md, "_service": service, "_metrics": _Metrics()},
    )()
    expected_analysis_close = (cfg.now_ms() // M5) * M5

    asyncio.run(SignalEngine._on_candle_close(fake_engine, "XAUUSD"))

    assert service.analyze_calls == [("XAUUSD", expected_analysis_close)]
    assert service.update_calls == ["XAUUSD"]


def test_scheduler_weekend_sleep_delays_until_broker_monday_open() -> None:
    # 2026-06-06 12:00 UTC = Saturday 15:00 broker (UTC+3).
    # Reopen at Monday 01:00 broker = Sydney session open (Sunday 22:00 UTC).
    # Wait: 604800 - (5*86400 + 15*3600) + 3600 = 122400 s = 34 h.
    cfg = _SchedulerCfg(
        now=_utc_ms("2026-06-06 12:00:00"),
        broker_time_offset_ms=3 * 60 * 60 * 1000,
    )
    scheduler = object.__new__(SignalScheduler)
    scheduler._cfg = cfg

    delay_ms = SignalScheduler._weekend_sleep_delay_ms(scheduler)

    assert delay_ms == (34 * 60 * 60 * 1000) + cfg.ws_candle_buffer_ms


def test_scheduler_weekend_sleep_allows_weekday_market_time() -> None:
    cfg = _SchedulerCfg(
        now=_utc_ms("2026-06-03 16:00:00"),
        broker_time_offset_ms=3 * 60 * 60 * 1000,
    )
    scheduler = object.__new__(SignalScheduler)
    scheduler._cfg = cfg

    assert SignalScheduler._weekend_sleep_delay_ms(scheduler) is None


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

    MultiPairBacktester._print_result(bt, result)

    out = re.sub(r"\x1b\[[0-9;]*m", "", capsys.readouterr().out)
    assert "S=2023-11-14 22:13:20" in out
    assert "A=2023-11-14 22:18:20" in out
    assert "E@2023-11-14 22:18:20" in out
    assert "LONG" not in out
    assert "RR=" not in out
    assert "EXP 0.00R" in out
    assert "closed OPEN" not in out
    assert "closed EXPIRED" in out


class _SchedulerCfg:
    tf_pairs = (("5min", "5min"),)
    ws_candle_buffer_ms = 1_500
    weekend_sleep_enabled = True
    weekend_close_weekday = 5
    weekend_close_time = datetime.time(0, 0)
    weekend_reopen_weekday = 0
    weekend_reopen_time = datetime.time(1, 0)  # Monday 01:00 broker = Sydney open

    def __init__(self, now: int, broker_time_offset_ms: int = 0) -> None:
        self._now = now
        self.broker_time_offset_ms = broker_time_offset_ms

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


class _Metrics:
    """No-op metrics stub — absorbs any recorder call _on_candle_close makes."""

    def __getattr__(self, name: str):
        def _noop(*args, **kwargs):
            return None

        return _noop


class _MarketDataClock:
    def __init__(self, now: int) -> None:
        self._now = now

    def now_ms(self, symbol: str) -> int:
        return self._now


class _PrintCfg:
    def dt_ms(self, ms: int) -> str:
        import datetime

        return datetime.datetime.fromtimestamp(
            ms / 1000, tz=datetime.timezone.utc
        ).strftime("%Y-%m-%d %H:%M:%S")


def _utc_ms(value: str) -> int:
    dt = datetime.datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(
        tzinfo=datetime.timezone.utc
    )
    return int(dt.timestamp() * 1000)


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
            stop_placement="wick",
            stop_buffer_pct=0.0,
            max_sl_zone_mult=10.0,
            tp1_trigger_pct=50.0,
            tp1_close_pct=0.0,
            move_sl_to_be_on_tp1=True,
            use_invalidation=False,
            signal_expiry_hours=1.0,
            use_trend_filter=False,
            htf_lookback=120,
            multi_tf_independent_positions=False,
        ),
    )
