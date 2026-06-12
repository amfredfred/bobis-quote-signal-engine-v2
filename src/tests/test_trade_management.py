from __future__ import annotations

import pytest

from domain.entities.enums import SignalDirection
from domain.trade_management import (
    breakeven_price,
    protected_breakeven_rr,
)


def test_breakeven_price_long_returns_entry() -> None:
    assert breakeven_price(
        direction=SignalDirection.LONG,
        entry_price=100.0,
    ) == pytest.approx(100.0)


def test_breakeven_price_short_returns_entry() -> None:
    assert breakeven_price(
        direction=SignalDirection.SHORT,
        entry_price=99.5,
    ) == pytest.approx(99.5)


def test_protected_breakeven_rr_matches_tp1_booked() -> None:
    rr = protected_breakeven_rr(
        full_rr=2.0,
        tp1_trigger_pct=50.0,
        tp1_close_pct=25.0,
    )
    # tp1_booked_rr(full_rr=2.0, tp1_trigger_pct=50.0, tp1_close_pct=25.0)
    # partial_close = 25% of position closed at 1R (50% of 2R full)
    # remaining 75% continues → realized contribution = 0.25 * 1.0 = 0.25
    # remaining (1 - 0.25) at BE = 0R contribution
    # protected_rr = 0.25
    assert rr == pytest.approx(0.25)
