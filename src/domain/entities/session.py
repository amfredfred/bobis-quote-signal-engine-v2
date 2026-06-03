"""
domain/entities/session.py — records that flow through persistence and memory.

ClosedSignalRecord is the single authoritative shape for a completed trade.
It replaces the 25-positional-param signatures in persistence.py and
session_memory.py — both layers accept/return this dataclass instead.

WsMessage is the WebSocket envelope — thin wrapper kept here because it
references SignalEvent and belongs to the domain contract.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict

from domain.entities.enums import SignalEvent


# ── Closed signal record ──────────────────────────────────────────────────────

@dataclass
class ClosedSignalRecord:
    """
    Immutable snapshot of a completed trade.

    Stored in:
      - SQLite closed_signals table (authoritative)
      - session_YYYY-MM-DD.json (human-readable backup)
      - results/live/{SYMBOL}.csv (backtest-compatible history)

    All callers use keyword arguments; the ordering doesn't matter and
    optional fields default to zero/empty so callers only specify what
    they have.
    """

    signal_id:    str
    symbol:       str
    direction:    str           # "LONG" | "SHORT"
    outcome:      str           # SignalOutcome.value
    realized_rr:  float
    closed_at:    int           # UTC ms

    # ── Structural timestamps ─────────────────────────────────────────────────
    htf_ts:   int   = 0         # HTF swing zone timestamp
    ltf_ts:   int   = 0         # LTF range timestamp
    rej_ts:   int   = 0         # rejection candle timestamp
    entry_ts: int   = 0         # triggered_at (entry fill)

    # ── Levels ────────────────────────────────────────────────────────────────
    entry:     float = 0.0
    sl:        float = 0.0
    tp1:       float = 0.0
    tp2:       float = 0.0
    rr:        float = 0.0      # planned R:R at signal time
    wick_ratio: float = 0.0

    # ── Metadata ──────────────────────────────────────────────────────────────
    pattern:      str = ""
    htf_interval: str = ""
    ltf_interval: str = ""

    # ── Zone geometry ─────────────────────────────────────────────────────────
    htf_high:  float = 0.0
    htf_low:   float = 0.0
    tp_level:  float = 0.0
    ltf_high:  float = 0.0
    ltf_low:   float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> ClosedSignalRecord:
        known = {f.name for f in cls.__dataclass_fields__.values()}
        return cls(**{k: v for k, v in d.items() if k in known})


# ── WebSocket message envelope ────────────────────────────────────────────────

@dataclass(slots=True)
class WsMessage:
    event:   SignalEvent
    payload: dict

    def to_dict(self) -> dict:
        return {"event": self.event.value, "payload": self.payload}
