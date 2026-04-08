"""
app/session/coordinator.py — session coordinator.

Owns the mutable session state that lives between signal emissions:
  - DedupState     (dead zones, open positions, seen LTF/rejection)
  - Trade history  (for win-rate, consecutive-loss tracking)
  - Circuit breaker (pause after loss streak)
  - Session rollover at midnight

Separates the rule engine (domain/signals/dedup.py) from persistence
(infrastructure/persistence/) and from the application lifecycle.

The coordinator is constructed with its dependencies injected:
  - SignalStore  — authoritative SQLite source for startup replay
  - SessionStore — JSON backup + CSV writer
  - Settings     — config values (expiry, circuit breaker, TZ)
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from domain.entities.enums import (
    SignalDirection, SignalOutcome,
    CLOSED_OUTCOMES, WIN_OUTCOMES, VOID_OUTCOMES,
)
from domain.entities.ranges import HtfRange, LtfRange, RejectionCandle
from domain.entities.session import ClosedSignalRecord
from domain.signals.dedup import DedupState, DedupResult, should_emit

logger = logging.getLogger(__name__)


class SessionCoordinator:
    """
    Application-layer owner of per-session trading state.

    One instance per running engine. Thread-safe for reads; writes are
    serialised by the asyncio event loop (all callers are coroutines).
    """

    def __init__(
        self,
        signal_store,       # SignalStore
        session_store,      # SessionStore
        settings,           # Settings
    ) -> None:
        self._signal_store  = signal_store
        self._session_store = session_store
        self._settings      = settings

        tz = settings.session_tz
        now_ms = settings.now_ms()

        self._tz:               ZoneInfo                    = tz
        self._session_day:      date                        = self._to_date(now_ms)
        self._session_start_ms: int                         = self._day_start_ms(self._session_day)
        self._state:            DedupState                  = DedupState()
        self._history:          deque[ClosedSignalRecord]   = deque(maxlen=500)
        self._paused_until:     Optional[int]               = None

        self._load_session()
        logger.info(
            "SessionCoordinator ready  day=%s  tz=%s  zones=%d  history=%d  streak=%d",
            self._session_day, settings.session_timezone,
            len(self._state.dead_zones), len(self._history), self.consecutive_losses,
        )

    # ── Public: dedup gate ────────────────────────────────────────────────────

    def should_emit(
        self,
        *,
        htf_range:    HtfRange,
        ltf_range:    LtfRange,
        rejection:    RejectionCandle,
        direction:    SignalDirection,
        symbol:       str,
        current_ts:   int,
        htf_interval: str = "",
        ltf_interval: str = "",
    ) -> tuple[bool, str]:
        now = current_ts
        self._maybe_rollover(now)
        self._release_expired_directions(now)

        if self._paused_until and now < self._paused_until:
            mins = (self._paused_until - now) // 60_000
            return False, f"PAUSED after loss streak — {mins}min remaining"

        # Position lock (Rule B) — checked here because it involves app state
        multi = self._settings.multi_tf_independent_positions
        if self._state.is_direction_open(
            symbol=symbol, direction=direction,
            htf_interval=htf_interval, ltf_interval=ltf_interval,
            multi_tf_independent=multi,
        ):
            return False, f"POSITION OPEN on {symbol} {direction.value}"

        result: DedupResult = should_emit(
            self._state,
            htf_range=htf_range,
            ltf_range=ltf_range,
            rejection=rejection,
            direction=direction,
            symbol=symbol,
            current_ts=current_ts,
            stale_hours=self._settings.rejection_stale_hours(ltf_interval),
            htf_interval=htf_interval,
            ltf_interval=ltf_interval,
        )
        return result.allowed, result.reason

    def register_signal(
        self,
        *,
        signal_id:    str,
        symbol:       str,
        direction:    SignalDirection,
        htf_range:    HtfRange,
        ltf_range:    LtfRange,
        rejection:    RejectionCandle,
        htf_interval: str = "",
        ltf_interval: str = "",
    ) -> None:
        multi = self._settings.multi_tf_independent_positions
        self._state.register(
            symbol       = symbol,
            direction    = direction,
            htf_range    = htf_range,
            ltf_range    = ltf_range,
            rejection    = rejection,
            htf_interval = htf_interval,
            ltf_interval = ltf_interval,
            multi_tf_independent = multi,
        )

    def record_outcome(self, rec: ClosedSignalRecord) -> None:
        """Called after a signal closes. Updates state, persists, writes CSV."""
        multi = self._settings.multi_tf_independent_positions
        self._state.release_direction(
            symbol       = rec.symbol,
            direction    = rec.direction,
            htf_interval = rec.htf_interval,
            ltf_interval = rec.ltf_interval,
            multi_tf_independent = multi,
        )
        self._history.append(rec)

        # Persist
        session_day_str = self._session_day.isoformat()
        try:
            self._signal_store.insert_closed(rec, session_day_str)
        except Exception as exc:
            logger.error("Failed to insert closed signal %s to DB: %s", rec.signal_id, exc)

        self._session_store.append_record(rec, self._session_day, self._session_start_ms)
        self._session_store.append_csv(rec)
        self._check_streak()

        logger.info(
            "closed %s %s %s → %s  %.2fR  | %s",
            rec.symbol, rec.direction, rec.signal_id,
            rec.outcome, rec.realized_rr, self.status_line(),
        )

    # ── Public: queries ───────────────────────────────────────────────────────

    @property
    def session_day(self) -> date:
        return self._session_day

    @property
    def session_r(self) -> float:
        return sum(r.realized_rr for r in self._history if r.outcome in CLOSED_OUTCOMES)

    @property
    def consecutive_losses(self) -> int:
        count = 0
        for rec in reversed(list(self._history)):
            if rec.outcome == SignalOutcome.LOSS.value:
                count += 1
            elif rec.outcome in WIN_OUTCOMES:
                break
        return count

    @property
    def win_rate(self) -> Optional[float]:
        closed = [r for r in self._history if r.outcome in CLOSED_OUTCOMES]
        if not closed:
            return None
        return sum(1 for r in closed if r.outcome in WIN_OUTCOMES) / len(closed)

    def stats(self) -> dict:
        closed = [r for r in self._history if r.outcome in CLOSED_OUTCOMES]
        wins   = [r for r in closed if r.outcome in WIN_OUTCOMES]
        losses = [r for r in closed if r.outcome == SignalOutcome.LOSS.value]
        voids  = [r for r in self._history if r.outcome in VOID_OUTCOMES]
        now    = self._settings.now_ms()
        paused = bool(self._paused_until and now < self._paused_until)
        return {
            "session_day":         self._session_day.isoformat(),
            "session_timezone":    self._settings.session_timezone,
            "session_r":           round(self.session_r, 2),
            "total_signals":       len(self._history),
            "wins":                len(wins),
            "losses":              len(losses),
            "voids":               len(voids),
            "win_rate":            round(self.win_rate * 100, 1) if self.win_rate is not None else None,
            "consecutive_losses":  self.consecutive_losses,
            "paused":              paused,
            "paused_until":        (
                datetime.fromtimestamp(self._paused_until / 1000, tz=self._tz).isoformat()
                if paused else None
            ),
            "dead_zones":          len(self._state.dead_zones),
            "open_positions":      [
                f"{s}:{d}[{h}/{l}]" for s, d, h, l in self._state.open_dir
            ],
        }

    def status_line(self) -> str:
        now    = self._settings.now_ms()
        wr     = f"{self.win_rate * 100:.0f}%" if self.win_rate is not None else "—"
        cl     = self.consecutive_losses
        streak = f"  ⚠ {cl} consecutive losses" if cl >= 2 else ""
        paused = "  🔴 PAUSED" if (self._paused_until and now < self._paused_until) else ""
        open_pos = [f"{s}:{d}[{h}/{l}]" for s, d, h, l in self._state.open_dir] or ["∅"]
        return (
            f"[{self._session_day} {self._settings.session_timezone}]  "
            f"{self.session_r:+.2f}R  WR {wr}  "
            f"zones={len(self._state.dead_zones)}  open={open_pos}"
            f"{streak}{paused}"
        )

    # ── Internals ─────────────────────────────────────────────────────────────

    def _to_date(self, ts_ms: int) -> date:
        return datetime.fromtimestamp(ts_ms / 1000, tz=self._tz).date()

    def _day_start_ms(self, day: date) -> int:
        dt = datetime(day.year, day.month, day.day, 0, 0, 0, tzinfo=self._tz)
        return int(dt.timestamp() * 1000)

    def _load_session(self) -> None:
        session_day_str = self._session_day.isoformat()
        records: list[ClosedSignalRecord] = []

        # Primary: SQLite
        try:
            records = self._signal_store.load_closed_for_session(session_day_str)
            logger.info("Loaded %d closed record(s) from SQLite", len(records))
        except Exception as exc:
            logger.warning("SQLite session load failed (%s) — falling back to JSON", exc)

        # Fallback: session JSON
        if not records:
            records = self._session_store.load_records_from_json(self._session_day)
            if records:
                logger.info("Loaded %d record(s) from JSON fallback", len(records))

        if not records:
            logger.info("No session data for %s — fresh start", session_day_str)
            return

        self._state.replay(records)
        for rec in records:
            self._history.append(rec)

        self._check_streak()
        logger.info(
            "Replayed %d record(s)  zones=%d  streak=%d",
            len(records), len(self._state.dead_zones), self.consecutive_losses,
        )

    def _maybe_rollover(self, now: int) -> None:
        new_day = self._to_date(now)
        if new_day == self._session_day:
            return
        logger.info(
            "Session rollover: %s → %s  (closing R: %.2f)",
            self._session_day, new_day, self.session_r,
        )
        self._session_day      = new_day
        self._session_start_ms = self._day_start_ms(new_day)
        self._state            = DedupState()
        self._history.clear()
        self._paused_until     = None
        self._load_session()

    def _release_expired_directions(self, now: int) -> None:
        expired = [
            k for k, ts in self._state.open_dir.items()
            if ts is not None and ts <= now
        ]
        for k in expired:
            del self._state.open_dir[k]
            logger.debug("Released direction lock: %s", k)

    def _check_streak(self) -> None:
        cl = self.consecutive_losses
        max_losses   = self._settings.max_consecutive_losses
        pause_hours  = self._settings.pause_after_streak_h

        if cl < max_losses:
            self._paused_until = None
            return

        streak_records = []
        for rec in reversed(list(self._history)):
            if rec.outcome == SignalOutcome.LOSS.value:
                streak_records.append(rec)
            elif rec.outcome in WIN_OUTCOMES:
                break

        last_loss_ts  = streak_records[0].closed_at
        pause_until   = last_loss_ts + int(pause_hours * 3_600_000)
        now           = self._settings.now_ms()

        if pause_until > now:
            if self._paused_until != pause_until:
                logger.warning(
                    "⚠  %d consecutive losses — engine PAUSED for %.1f hours (until %s)",
                    cl, pause_hours,
                    datetime.fromtimestamp(pause_until / 1000, tz=self._tz).isoformat(),
                )
            self._paused_until = pause_until
        else:
            self._paused_until = None
