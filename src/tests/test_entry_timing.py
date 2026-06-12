from __future__ import annotations

from domain.entities.candle import Candle
from domain.entities.enums import BosDirection, SignalDirection
from domain.entities.ranges import HtfRange
from domain.market.rejection import CrtDetector
from domain.signals.entry import find_entry


BASE = 1_780_000_000_000
M5 = 5 * 60 * 1000


def test_crt_sell_sweeps_prev_high_and_closes_below() -> None:
    """Candle that wicks above previous candle's high and closes back below triggers CRT SELL."""
    htf_range = HtfRange(110.0, 90.0, BosDirection.BEARISH, BASE)
    prev = Candle(BASE + M5, 100.0, 104.0, 99.0, 103.0)
    trigger = Candle(BASE + 2 * M5, 103.0, 105.0, 101.0, 103.5)

    result = CrtDetector.check(trigger, prev, SignalDirection.SHORT, htf_range)

    assert result is not None
    assert result[0].timestamp == BASE + 2 * M5


def test_crt_sell_no_trigger_when_close_above_level() -> None:
    """Close must come back below prev high — if it stays above, no trigger."""
    htf_range = HtfRange(110.0, 90.0, BosDirection.BEARISH, BASE)
    prev = Candle(BASE + M5, 100.0, 104.0, 99.0, 103.0)
    trigger = Candle(BASE + 2 * M5, 103.0, 106.0, 101.0, 105.0)  # close above prev.high

    result = CrtDetector.check(trigger, prev, SignalDirection.SHORT, htf_range)

    assert result is None


def test_crt_buy_sweeps_prev_low_and_closes_above() -> None:
    """Candle that wicks below previous candle's low and closes back above triggers CRT BUY."""
    htf_range = HtfRange(110.0, 90.0, BosDirection.BULLISH, BASE)
    prev = Candle(BASE + M5, 100.0, 103.0, 98.0, 99.0)
    trigger = Candle(BASE + 2 * M5, 99.0, 101.0, 96.0, 99.5)

    result = CrtDetector.check(trigger, prev, SignalDirection.LONG, htf_range)

    assert result is not None
    assert result[0].timestamp == BASE + 2 * M5


def test_crt_htf_containment_filters_out_of_zone_candle() -> None:
    """Candle entirely outside the HTF range box is rejected even if it sweeps prev level."""
    htf_range = HtfRange(105.0, 95.0, BosDirection.BEARISH, BASE)
    prev = Candle(BASE + M5, 80.0, 84.0, 79.0, 83.0)   # well below HTF zone
    trigger = Candle(BASE + 2 * M5, 83.0, 85.0, 80.0, 82.5)

    result = CrtDetector.check(trigger, prev, SignalDirection.SHORT, htf_range)

    assert result is None


def test_crt_find_most_recent_returns_latest_trigger() -> None:
    htf_range = HtfRange(110.0, 90.0, BosDirection.BEARISH, BASE)
    candles = [
        Candle(BASE + M5, 100.0, 104.0, 99.0, 103.0),
        Candle(BASE + 2 * M5, 103.0, 105.0, 101.0, 103.5),  # trigger
        Candle(BASE + 3 * M5, 103.5, 104.0, 102.0, 103.0),  # no sweep of prev.high=105
    ]

    result = CrtDetector.find_most_recent(candles, SignalDirection.SHORT, htf_range)

    assert result is not None
    assert result[0].timestamp == BASE + 2 * M5


def test_find_entry_delegates_to_crt_detector() -> None:
    htf_range = HtfRange(110.0, 90.0, BosDirection.BEARISH, BASE)
    candles = [
        Candle(BASE, 100.0, 104.0, 99.0, 103.0),
        Candle(BASE + M5, 103.0, 105.0, 101.0, 103.5),
    ]

    result = find_entry(candles, SignalDirection.SHORT, htf_range)

    assert result is not None
    assert result[0].timestamp == BASE + M5
