"""
infrastructure/persistence/signal_store.py — SQLite-backed signal persistence.

Replaces data/persistence.py. All methods accept/return ClosedSignalRecord
dataclasses instead of 25 positional parameters.

Two tables:
  open_signals   — active signals (TRIGGERED / TP1_HIT), rebuilt into the
                   in-memory watchlist on startup.
  closed_signals — completed trades; permanent record for reporting and
                   session-memory replay.

Design choices:
  - WAL + NORMAL synchronous: crash-safe without fsync overhead.
  - Thread-local connections + a single write lock: many readers, one writer.
  - INSERT OR IGNORE on closed_signals: idempotent — safe to call twice.
  - All writes accept ClosedSignalRecord; callers never manage raw tuples.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from pathlib import Path
from typing import Optional

from domain.entities.session import ClosedSignalRecord
from domain.entities.trade import TradeSignal

logger = logging.getLogger(__name__)

# ── Schema ────────────────────────────────────────────────────────────────────

_DDL = """
PRAGMA journal_mode = WAL;
PRAGMA synchronous  = NORMAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS open_signals (
    signal_id    TEXT    PRIMARY KEY,
    symbol       TEXT    NOT NULL,
    direction    TEXT    NOT NULL,
    htf_interval TEXT    NOT NULL,
    ltf_interval TEXT    NOT NULL,
    status       TEXT    NOT NULL,
    created_at   INTEGER NOT NULL,
    triggered_at INTEGER,
    tp1_hit_at   INTEGER,
    raw_json     TEXT    NOT NULL,
    updated_at   INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS closed_signals (
    signal_id    TEXT    PRIMARY KEY,
    symbol       TEXT    NOT NULL,
    direction    TEXT    NOT NULL,
    htf_interval TEXT    NOT NULL DEFAULT '',
    ltf_interval TEXT    NOT NULL DEFAULT '',
    outcome      TEXT    NOT NULL,
    realized_rr  REAL    NOT NULL,
    entry        REAL    NOT NULL DEFAULT 0,
    sl           REAL    NOT NULL DEFAULT 0,
    tp1          REAL    NOT NULL DEFAULT 0,
    tp2          REAL    NOT NULL DEFAULT 0,
    rr           REAL    NOT NULL DEFAULT 0,
    wick_ratio   REAL    NOT NULL DEFAULT 0,
    pattern      TEXT    NOT NULL DEFAULT '',
    htf_ts       INTEGER NOT NULL,
    ltf_ts       INTEGER NOT NULL DEFAULT 0,
    rej_ts       INTEGER NOT NULL DEFAULT 0,
    entry_ts     INTEGER NOT NULL DEFAULT 0,
    closed_at    INTEGER NOT NULL,
    session_day  TEXT    NOT NULL,
    htf_high     REAL    NOT NULL DEFAULT 0,
    htf_low      REAL    NOT NULL DEFAULT 0,
    tp_level     REAL    NOT NULL DEFAULT 0,
    ltf_high     REAL    NOT NULL DEFAULT 0,
    ltf_low      REAL    NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_open_symbol  ON open_signals  (symbol);
CREATE INDEX IF NOT EXISTS idx_closed_day   ON closed_signals (session_day);
CREATE INDEX IF NOT EXISTS idx_closed_sym   ON closed_signals (symbol);
"""


# ── Connection pool ───────────────────────────────────────────────────────────

_local      = threading.local()
_write_lock = threading.Lock()


class SignalStore:
    """
    SQLite persistence for open and closed signals.

    Instantiate once per application; inject wherever persistence is needed.
    The db_path can be any Path — makes testing with :memory: trivial.
    """

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    # ── Setup ─────────────────────────────────────────────────────────────────

    def _conn(self) -> sqlite3.Connection:
        if not hasattr(_local, "conn") or _local.conn is None:
            c = sqlite3.connect(
                str(self._db_path), check_same_thread=False, timeout=10
            )
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA journal_mode = WAL")
            c.execute("PRAGMA synchronous  = NORMAL")
            _local.conn = c
        return _local.conn

    def _init_db(self) -> None:
        with _write_lock:
            self._conn().executescript(_DDL)
            self._conn().commit()
        logger.info("SignalStore ready: %s", self._db_path)

    # ── Open signals ──────────────────────────────────────────────────────────

    def upsert_open(self, signal: TradeSignal, now_ms: int) -> None:
        """Insert or update an active signal. Called on every state change."""
        d = signal.to_dict()
        with _write_lock:
            self._conn().execute(
                """
                INSERT INTO open_signals
                    (signal_id, symbol, direction, htf_interval, ltf_interval,
                     status, created_at, triggered_at, tp1_hit_at, raw_json, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(signal_id) DO UPDATE SET
                    status       = excluded.status,
                    tp1_hit_at   = excluded.tp1_hit_at,
                    raw_json     = excluded.raw_json,
                    updated_at   = excluded.updated_at
                """,
                (
                    d["id"], d["symbol"], d["direction"],
                    d.get("htfInterval", ""), d.get("ltfInterval", ""),
                    d["status"],
                    d.get("createdAt", 0), d.get("triggeredAt"),
                    d.get("tp1HitAt"), json.dumps(d), now_ms,
                ),
            )
            self._conn().commit()

    def delete_open(self, signal_id: str) -> None:
        """Remove a signal from the open table when it closes. Safe if absent."""
        with _write_lock:
            self._conn().execute(
                "DELETE FROM open_signals WHERE signal_id = ?", (signal_id,)
            )
            self._conn().commit()

    def load_open_signals(self) -> list[dict]:
        """Load all open signals as raw dicts (from raw_json). Called at startup."""
        rows = (
            self._conn()
            .execute("SELECT raw_json FROM open_signals ORDER BY created_at")
            .fetchall()
        )
        results = []
        for row in rows:
            try:
                results.append(json.loads(row["raw_json"]))
            except Exception as exc:
                logger.warning("Skipping corrupt open_signal row: %s", exc)
        return results

    # ── Closed signals ────────────────────────────────────────────────────────

    def insert_closed(self, rec: ClosedSignalRecord, session_day: str) -> None:
        """Write a closed signal record. Idempotent — INSERT OR IGNORE."""
        with _write_lock:
            self._conn().execute(
                """
                INSERT OR IGNORE INTO closed_signals (
                    signal_id, symbol, direction, htf_interval, ltf_interval,
                    outcome, realized_rr, entry, sl, tp1, tp2, rr, wick_ratio,
                    pattern, htf_ts, ltf_ts, rej_ts, entry_ts, closed_at,
                    session_day, htf_high, htf_low, tp_level, ltf_high, ltf_low
                ) VALUES (
                    ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?
                )
                """,
                (
                    rec.signal_id, rec.symbol, rec.direction,
                    rec.htf_interval, rec.ltf_interval,
                    rec.outcome, rec.realized_rr,
                    rec.entry, rec.sl, rec.tp1, rec.tp2, rec.rr, rec.wick_ratio,
                    rec.pattern, rec.htf_ts, rec.ltf_ts, rec.rej_ts, rec.entry_ts,
                    rec.closed_at, session_day,
                    rec.htf_high, rec.htf_low, rec.tp_level,
                    rec.ltf_high, rec.ltf_low,
                ),
            )
            self._conn().commit()

    def load_closed_for_session(self, session_day: str) -> list[ClosedSignalRecord]:
        """Return all closed signals for a session day. Used for startup replay."""
        rows = (
            self._conn()
            .execute(
                """
                SELECT signal_id, symbol, direction, htf_interval, ltf_interval,
                       outcome, realized_rr, entry, sl, tp1, tp2, rr, wick_ratio,
                       pattern, htf_ts, ltf_ts, rej_ts, entry_ts, closed_at,
                       htf_high, htf_low, tp_level, ltf_high, ltf_low
                FROM   closed_signals
                WHERE  session_day = ?
                ORDER BY closed_at
                """,
                (session_day,),
            )
            .fetchall()
        )
        records = []
        for row in rows:
            try:
                records.append(ClosedSignalRecord(**dict(row)))
            except Exception as exc:
                logger.warning("Skipping malformed closed_signal row: %s", exc)
        return records

    def load_closed_for_dedup(self) -> list[ClosedSignalRecord]:
        """Return structural keys for all closed signals to rebuild zone limits."""
        rows = (
            self._conn()
            .execute(
                """
                SELECT signal_id, symbol, direction, htf_interval, ltf_interval,
                       outcome, realized_rr, htf_ts, ltf_ts, rej_ts, closed_at
                FROM closed_signals
                ORDER BY closed_at
                """
            )
            .fetchall()
        )
        records = []
        for row in rows:
            try:
                records.append(ClosedSignalRecord(**dict(row)))
            except Exception as exc:
                logger.warning("Skipping malformed dedup record: %s", exc)
        return records

    def get_open(self, signal_id: str) -> Optional[dict]:
        """Fetch a single open signal by ID. Returns None if not found."""
        row = (
            self._conn()
            .execute(
                "SELECT raw_json FROM open_signals WHERE signal_id = ?", (signal_id,)
            )
            .fetchone()
        )
        if row is None:
            return None
        try:
            return json.loads(row["raw_json"])
        except Exception:
            return None
