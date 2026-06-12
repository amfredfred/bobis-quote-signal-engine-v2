"""
domain/signals/dedup.py — pure deduplication rules for signal emission.

This is the domain half of the old SessionMemory: the rule engine that
decides whether a signal should be emitted. All state lives in plain
Python sets/dicts — no file I/O, no config imports, no _cfg.

I/O (loading/saving dedup state to disk) lives in the infrastructure layer.

Dedup rules (A, B, D, E):
  A — zone count:   up to max_signal_count emissions per BOS zone
  B — open_dir:     one open position per symbol      (symbol, direction[, htf, ltf])
  D — stale filter: rejection older than stale_hours → skip
  E — seen_rej:     each rejection candle fires once  (symbol, htf, ltf, rej_ts)

Circuit breaker:
  After MAX_CONSECUTIVE_LOSSES → pause for PAUSE_HOURS.
  Pause derived from closed_at timestamps — no extra persistence needed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

from domain.entities.enums import SignalDirection
from domain.entities.ranges import HtfRange, RejectionCandle
from domain.entities.session import ClosedSignalRecord

logger = logging.getLogger(__name__)

# Type alias for the open-direction lock key
_DirKey = tuple  # (symbol, direction, htf_interval, ltf_interval) or (symbol, direction, "", "")


@dataclass
class DedupState:
    """
    Mutable dedup state. Zone counts persist for the lifetime of each zone;
    direction locks and trade history are managed by the session coordinator.

    Populated from the infrastructure layer on startup and retained across
    session-day rollovers.
    """

    # Rule A — zones that reached their configured signal limit
    dead_zones: set[tuple] = field(default_factory=set)
    zone_signal_counts: dict[tuple, int] = field(default_factory=dict)

    # Rule B — one open position per direction (key includes TF pair when
    #           multi_tf_independent_positions=True)
    open_dir: dict[_DirKey, Optional[int]] = field(default_factory=dict)

    # Rule E — each rejection candle fires once
    seen_rej: set[tuple] = field(default_factory=set)

    def replay(self, records: list[ClosedSignalRecord], max_signal_count: int = 1) -> None:
        """Rebuild state from a list of closed signal records (startup replay)."""
        for rec in records:
            zone_key, rej_key = _dedup_keys(
                rec.symbol,
                rec.htf_interval,
                rec.ltf_interval,
                rec.htf_ts,
                rec.rej_ts,
                rec.direction,
            )
            count = self.zone_signal_counts.get(zone_key, 0) + 1
            self.zone_signal_counts[zone_key] = count
            if count >= max_signal_count:
                self.dead_zones.add(zone_key)
            if rec.rej_ts:
                self.seen_rej.add(rej_key)

    def register(
        self,
        *,
        symbol:       str,
        direction:    SignalDirection,
        htf_range:    HtfRange,
        rejection:    RejectionCandle,
        htf_interval: str = "",
        ltf_interval: str = "",
        multi_tf_independent: bool = True,
        max_signal_count: int = 1,
    ) -> int:
        """Register an emitted signal and return its one-based zone attempt."""
        dir_str = direction.value
        zone_key, rej_key = _dedup_keys(
            symbol,
            htf_interval,
            ltf_interval,
            htf_range.timestamp,
            rejection.timestamp,
            dir_str,
        )
        attempt = self.zone_signal_counts.get(zone_key, 0) + 1
        self.zone_signal_counts[zone_key] = attempt
        if attempt >= max_signal_count:
            self.dead_zones.add(zone_key)
        self.seen_rej.add(rej_key)

        dir_key: _DirKey = (
            (symbol, dir_str, htf_interval, ltf_interval)
            if multi_tf_independent
            else (symbol, dir_str, "", "")
        )
        self.open_dir[dir_key] = None

        logger.info(
            "registered %s %s  zone=%d attempt=%d/%d  rej=%d",
            dir_str, symbol,
            htf_range.timestamp, attempt, max_signal_count,
            rejection.timestamp,
        )
        return attempt

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
    rejection:    RejectionCandle,
    direction:    SignalDirection,
    symbol:       str,
    current_ts:   int,
    stale_hours:  float,
    htf_interval: str  = "",
    ltf_interval: str  = "",
    max_signal_count: int = 1,
) -> DedupResult:
    """Run rules A, D, E and return whether this signal should be emitted."""
    dir_str = direction.value

    zone_key, rej_key = _dedup_keys(
        symbol,
        htf_interval,
        ltf_interval,
        htf_range.timestamp,
        rejection.timestamp,
        dir_str,
    )

    # A — zone reached its configured emission limit
    zone_count = state.zone_signal_counts.get(zone_key, 0)
    if zone_key in state.dead_zones or zone_count >= max_signal_count:
        return DedupResult(
            False,
            f"A: [{symbol}] zone signal limit {zone_count}/{max_signal_count} "
            f"for {htf_range.timestamp}",
        )

    # D — rejection candle is stale
    stale_ms = stale_hours * 3_600_000
    if current_ts - rejection.timestamp > stale_ms:
        return DedupResult(
            False,
            f"D: [{symbol}] STALE — rejection is {(current_ts - rejection.timestamp) / 3_600_000:.2f}h old (max {stale_hours}h)",
        )

    # E — rejection candle already used
    if rej_key in state.seen_rej:
        return DedupResult(False, f"E: [{symbol}] seen rej {rejection.timestamp}")

    return DedupResult(True, "")


def _dedup_keys(
    symbol: str,
    htf_interval: str,
    ltf_interval: str,
    htf_ts: int,
    rej_ts: int,
    direction: str,
) -> tuple[tuple, tuple]:
    prefix = (symbol, htf_interval or "", ltf_interval or "")
    return (
        (*prefix, htf_ts, direction),
        (*prefix, rej_ts),
    )
