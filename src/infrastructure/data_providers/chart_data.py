"""
infrastructure/data_providers/chart_data.py — builds chart payloads for TradingView Lightweight Charts.

Ported from data/chart_data.py with updated imports.
"""

from __future__ import annotations

from typing import Optional

from domain.entities.candle import Candle
from domain.entities.enums import SignalDirection
from domain.entities.trade import TradeSignal

_HTF_COLOR = "rgba(245, 124, 0, 0.25)"
_LTF_COLOR = "rgba(59, 130, 246, 0.25)"
_ENTRY_COLOR = "#FBBF24"
_SL_COLOR    = "#EF4444"
_TP1_COLOR   = "#34D399"
_TP2_COLOR   = "#22D3EE"
_REJ_COLOR   = "#F59E0B"


def _candles_to_dicts(candles: list[Candle]) -> list[dict]:
    return [
        {"time": c.timestamp // 1000, "open": c.open, "high": c.high, "low": c.low, "close": c.close}
        for c in candles
    ]


def _idx(candles: list[Candle], ts: int) -> int:
    if not candles:
        return 0
    exact = next((i for i, c in enumerate(candles) if c.timestamp == ts), None)
    if exact is not None:
        return exact
    return min(range(len(candles)), key=lambda i: abs(candles[i].timestamp - ts))


def build_chart_data(
    signal:      TradeSignal,
    ltf_candles: list[Candle],
    htf_candles: list[Candle],
    *,
    ltf_pre:      int = 40,
    ltf_post:     int = 80,
    htf_pre:      int = 35,
    htf_post:     int = 15,
    htf_interval: str = "30min",
    ltf_interval: str = "1min",
) -> dict:
    swg_i     = _idx(ltf_candles, signal.ltf_range.timestamp)
    rej_i     = _idx(ltf_candles, signal.rejection_candle.timestamp)
    ltf_slice = ltf_candles[max(0, swg_i - ltf_pre) : min(len(ltf_candles), rej_i + ltf_post + 1)]

    zone_i    = _idx(htf_candles, signal.htf_range.timestamp)
    htf_slice = htf_candles[max(0, zone_i - htf_pre) : min(len(htf_candles), zone_i + htf_post + 1)]

    is_short  = signal.direction == SignalDirection.SHORT
    rej_ts_s  = signal.rejection_candle.timestamp // 1000

    markers = [{
        "time":     rej_ts_s,
        "position": "aboveBar" if is_short else "belowBar",
        "color":    _REJ_COLOR,
        "shape":    "arrowDown" if is_short else "arrowUp",
        "text":     "Entry",
    }]

    htf_markers = []
    if signal.htf_range.broken_at:
        htf_markers.append({
            "time":     signal.htf_range.broken_at // 1000,
            "position": "aboveBar" if is_short else "belowBar",
            "color":    "#F57C00",
            "shape":    "circle",
            "text":     "BOS",
        })

    price_lines = [
        {"price": signal.entry_price, "color": _ENTRY_COLOR, "label": "Entry", "lineStyle": 0},
        {"price": signal.stop_loss,   "color": _SL_COLOR,    "label": "SL",    "lineStyle": 2},
        {"price": signal.tp1,         "color": _TP1_COLOR,   "label": "TP1",   "lineStyle": 1},
        {"price": signal.tp2,         "color": _TP2_COLOR,   "label": "TP2",   "lineStyle": 0},
    ]

    return {
        "ltf": {"candles": _candles_to_dicts(ltf_slice), "markers": markers, "priceLines": price_lines},
        "htf": {"candles": _candles_to_dicts(htf_slice), "markers": htf_markers},
        "overlays": {
            "htfZone": {
                "top": signal.htf_range.range_high, "bottom": signal.htf_range.range_low,
                "color": _HTF_COLOR, "borderColor": "#F57C00",
                "label": "Supply" if is_short else "Demand",
            },
            "ltfZone": {
                "top": signal.ltf_range.range_high, "bottom": signal.ltf_range.range_low,
                "color": _LTF_COLOR, "borderColor": "#3B82F6",
                "label": "LTF Swing",
            },
            "entry": signal.entry_price, "sl": signal.stop_loss,
            "tp1": signal.tp1, "tp2": signal.tp2,
        },
        "meta": {
            "symbol":       signal.symbol,
            "direction":    signal.direction.value,
            "htfInterval":  htf_interval,
            "ltfInterval":  ltf_interval,
            "rr":           round(signal.risk_reward_ratio, 2),
            "pattern":      signal.rejection_candle.pattern.value,
            "wickRatio":    round(signal.rejection_candle.wick_ratio, 4),
            "rejectionTs":  rej_ts_s,
            "bosDirection": signal.htf_range.bos_direction.value,
        },
    }
