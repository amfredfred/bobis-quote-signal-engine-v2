from __future__ import annotations

import pytest

from domain.entities.enums import SignalDirection
from domain.trade_management import (
    breakeven_buffer,
    breakeven_price,
    protected_breakeven_rr,
)


def test_breakeven_price_adds_buffer_for_long() -> None:
    assert breakeven_price(
        direction=SignalDirection.LONG,
        entry_price=100.0,
        risk_pips=10.0,
        spread_price_units=0.1,
        spread_multiplier=1.5,
        max_buffer_pct_of_risk=10.0,
    ) == pytest.approx(100.15)


def test_breakeven_price_subtracts_buffer_for_short() -> None:
    assert breakeven_price(
        direction=SignalDirection.SHORT,
        entry_price=100.0,
        risk_pips=10.0,
        spread_price_units=0.1,
        spread_multiplier=1.5,
        max_buffer_pct_of_risk=10.0,
    ) == pytest.approx(99.85)


def test_protected_breakeven_rr_applies_buffer_to_remaining_position() -> None:
    assert protected_breakeven_rr(
        full_rr=2.0,
        tp1_trigger_pct=50.0,
        tp1_close_pct=25.0,
        risk_pips=10.0,
        spread_price_units=0.5,
        spread_multiplier=1.5,
        max_buffer_pct_of_risk=10.0,
    ) == pytest.approx(0.30625)


def test_breakeven_price_cap_never_reduces_buffer_below_spread() -> None:
    assert breakeven_price(
        direction=SignalDirection.LONG,
        entry_price=100.0,
        risk_pips=10.0,
        spread_price_units=5.0,
        spread_multiplier=2.0,
        max_buffer_pct_of_risk=10.0,
    ) == pytest.approx(105.0)


def test_breakeven_buffer_zero_multiplier_uses_entry_breakeven() -> None:
    assert breakeven_buffer(
        risk_pips=10.0,
        spread_price_units=0.5,
        spread_multiplier=0.0,
        max_buffer_pct_of_risk=10.0,
    ) == 0.0


def test_tight_stop_protected_breakeven_is_not_negative_after_spread() -> None:
    risk = 1.0
    spread = 0.28
    buffer = breakeven_buffer(
        risk_pips=risk,
        spread_price_units=spread,
        spread_multiplier=1.5,
        max_buffer_pct_of_risk=10.0,
    )

    assert (buffer / risk) - (spread / risk) >= 0.0
