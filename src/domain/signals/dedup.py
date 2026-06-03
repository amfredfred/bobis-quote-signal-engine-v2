"""
domain/signals/dedup.py — pure deduplication rules for signal emission.

This is the domain half of the old SessionMemory: the rule engine that
decides whether a signal should be emitted. All state lives in plain
Python sets/dicts — no file I/O, no config imports, no _cfg.

I/O (loading/saving dedup state to disk) lives in the infrastructure layer.

Dedup rules (A–E, identical to backtester):
  A — dead_zones:   one signal per BOS zone ever     (htf_ts, direction)
  B — open_dir:     one open position per symbol      (symbol, direction[, htf, ltf])
  C — seen_ltf:     one signal per LTF swing          (ltf_ts, direction)
  D — stale filter: rejection older than stale_hours → skip
  E — seen_rej:     each rejection candle fires once  (rej_ts)

Circuit breaker:
  After MAX_CONSECUTIVE_LOSSES → pause for PAUSE_HOURS.
  Pause derived from closed_at timestamps — no extra persistence needed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

from domain.entities.enums import SignalDirection
from domain.entities.ranges import HtfRange, LtfRange, RejectionCandle
from domain.entities.session import ClosedSignalRecord

logger = logging.getLogger(__name__)

# Type alias for the open-direction lock key
_DirKey = tuple  # (symbol, direction, htf_interval, ltf_interval) or (symbol, direction, "", "")


@dataclass
class DedupState:
    """
    All mutable dedup state for one trading session.

    Designed to be created fresh each session day and populated from
    the infrastructure layer (SQLite / JSON) on startup.
    """

    # Rule A — one signal per zone per session
    dead_zones: set[tuple[int, str]] = field(default_factory=set)

    # Rule B — one open position per direction (key includes TF pair when
    #           multi_tf_independent_positions=True)
    open_dir: dict[_DirKey, Optional[int]] = field(default_factory=dict)

    # Rule C — one signal per LTF swing per session
    seen_ltf: set[tuple[int, str]] = field(default_factory=set)

    # Rule E — each rejection candle fires once
    seen_rej: set[int] = field(default_factory=set)

    def replay(self, records: list[ClosedSignalRecord]) -> None:
        """Rebuild state from a list of closed signal records (startup replay)."""
        for rec in records:
            self.dead_zones.add((rec.htf_ts, rec.direction))
            if rec.ltf_ts:
                self.seen_ltf.add((rec.ltf_ts, rec.direction))
            if rec.rej_ts:
                self.seen_rej.add(rec.rej_ts)

    def register(
        self,
        *,
        symbol:       str,
        direction:    SignalDirection,
        htf_range:    HtfRange,
        ltf_range:    LtfRange,
        rejection:    RejectionCandle,
        htf_interval: str = "",
        ltf_interval: str = "",
        multi_tf_independent: bool = True,
    ) -> None:
        """Lock zone, LTF, and rejection after a signal is emitted."""
        dir_str = direction.value
        self.dead_zones.add((htf_range.timestamp, dir_str))
        self.seen_ltf.add((ltf_range.timestamp, dir_str))
        self.seen_rej.add(rejection.timestamp)

        dir_key: _DirKey = (
            (symbol, dir_str, htf_interval, ltf_interval)
            if multi_tf_independent
            else (symbol, dir_str, "", "")
        )
        self.open_dir[dir_key] = None

        logger.info(
            "registered %s %s  zone=%d  ltf=%d  rej=%d",
            dir_str, symbol,
            htf_range.timestamp, ltf_range.timestamp, rejection.timestamp,
        )

    def release_direction(
        self,
        *,
        symbol:       str,
        direction:    str,
        htf_interval: str = "",
        ltf_interval: str = "",
        multi_tf_independent: bool = True,
    ) -> None:
        """Unlock direction after a trade closes."""
        key: _DirKey = (
            (symbol, direction, htf_interval, ltf_interval)
            if multi_tf_independent
            else (symbol, direction, "", "")
        )
        self.open_dir.pop(key, None)

    def is_direction_open(
        self,
        *,
        symbol:       str,
        direction:    SignalDirection,
        htf_interval: str = "",
        ltf_interval: str = "",
        multi_tf_independent: bool = True,
    ) -> bool:
        dir_str = direction.value
        if multi_tf_independent:
            return (symbol, dir_str, htf_interval, ltf_interval) in self.open_dir
        return (symbol, dir_str, "", "") in self.open_dir


# ── Should-emit evaluator ─────────────────────────────────────────────────────

@dataclass(frozen=True)
class DedupResult:
    allowed: bool
    reason:  str


def should_emit(
    state:        DedupState,
    *,
    htf_range:    HtfRange,
    ltf_range:    LtfRange,
    rejection:    RejectionCandle,
    direction:    SignalDirection,
    symbol:       str,
    current_ts:   int,
    stale_hours:  float,
    htf_interval: str  = "",
    ltf_interval: str  = "",
) -> DedupResult:
    """
    Run rules A–E and return whether this signal should be emitted.

    Rule A — dead zone
    """
    dir_str = direction.value

    # A — zone already fired this session
    if (htf_range.timestamp, dir_str) in state.dead_zones:
        return DedupResult(False, f"A: [{symbol}] dead zone {htf_range.timestamp}")

    # C — LTF swing already used this session
    if (ltf_range.timestamp, dir_str) in state.seen_ltf:
        return DedupResult(False, f"C: [{symbol}] seen ltf {ltf_range.timestamp}")

    # D — rejection candle is stale
    stale_ms = stale_hours * 3_600_000
    if current_ts - rejection.timestamp > stale_ms:
        return DedupResult(
            False,
            f"D: [{symbol}] STALE — rejection is {(current_ts - rejection.timestamp) / 3_600_000:.2f}h old (max {stale_hours}h)",
        )

    # E — rejection candle already used
    if rejection.timestamp in state.seen_rej:
        return DedupResult(False, f"E: [{symbol}] seen rej {rejection.timestamp}")

    return DedupResult(True, "")
