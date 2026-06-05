from __future__ import annotations

from unittest.mock import patch

from domain.entities.candle import Candle
from domain.entities.enums import BosDirection, CandlePattern, SignalDirection
from domain.entities.ranges import HtfRange, LtfRange, RejectionCandle
from domain.market.swings import SwingDetector
from domain.signals.entry import find_entry


BASE = 1_780_000_000_000
M5 = 5 * 60 * 1000


def test_ltf_range_excludes_candle_opening_at_htf_close() -> None:
    htf_range = HtfRange(
        range_high=105.0,
        range_low=95.0,
        bos_direction=BosDirection.BEARISH,
        timestamp=BASE,
        htf_candle_open=BASE,
        htf_candle_close=BASE + M5,
    )
    inside = Candle(BASE, 100.0, 101.0, 99.0, 100.0)
    next_candle = Candle(BASE + M5, 100.0, 110.0, 98.0, 109.0)

    ltf_range = SwingDetector.find_ltf_range(
        [inside, next_candle],
        htf_range,
        [inside, next_candle],
    )

    assert ltf_range is not None
    assert ltf_range.timestamp == inside.timestamp
    assert ltf_range.range_high == inside.high


def test_all_entry_model_selects_most_recent_candidate() -> None:
    htf_range = HtfRange(105.0, 95.0, BosDirection.BEARISH, BASE)
    ltf_range = LtfRange(104.0, 100.0, BASE, SignalDirection.SHORT)
    older = RejectionCandle(
        101.0, 104.0, 100.0, 101.0, BASE + M5, 0.8, CandlePattern.SHOOTING_STAR
    )
    newer = RejectionCandle(
        101.0, 105.0, 100.0, 101.0, BASE + 2 * M5, 0.8, CandlePattern.CRT_SELL
    )

    with (
        patch(
            "domain.signals.entry.RejectionDetector.find_most_recent",
            return_value=(older, object()),
        ),
        patch(
            "domain.signals.entry.CrtDetector.find_most_recent",
            return_value=(newer, object()),
        ),
    ):
        result = find_entry([], ltf_range, htf_range, "all", 0.65)

    assert result is not None
    assert result[0] is newer
