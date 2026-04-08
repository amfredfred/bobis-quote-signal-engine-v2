"""
infrastructure/persistence/session_store.py — session-day JSON backup and live CSV writer.

This is the I/O half of the old SessionMemory:
  - JSON backup per session day  (human-readable, not authoritative)
  - results/live/{SYMBOL}.csv    (backtest-compatible trade history)

The authoritative source for session replay is SignalStore.load_closed_for_session().
These files are written for observability only — the engine doesn't read them on startup
unless the SQLite load fails.
"""

from __future__ import annotations

import csv
import json
import logging
from datetime import date, datetime, timezone
from pathlib import Path

from domain.entities.session import ClosedSignalRecord

logger = logging.getLogger(__name__)

_HISTORY_COLUMNS = [
    "id", "symbol", "direction", "entry_dt", "close_dt",
    "entry", "sl", "tp1", "tp2", "rr",
    "outcome", "realized_rr",
    "htf_interval", "ltf_interval",
    "pattern", "wick_ratio",
    "htf_high", "htf_low", "tp_level", "ltf_high", "ltf_low",
    "chart",
]


def _ms_to_utc_str(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


class SessionStore:
    """
    Writes session backup JSON and per-symbol live CSV files.

    session_dir  — e.g. Path("sessions")
    live_dir     — e.g. Path("results/live")
    session_tz   — ZoneInfo used for the JSON metadata only
    """

    def __init__(
        self,
        session_dir: Path,
        live_dir: Path,
        session_tz,
    ) -> None:
        self._session_dir = session_dir
        self._live_dir    = live_dir
        self._session_tz  = session_tz
        session_dir.mkdir(parents=True, exist_ok=True)
        live_dir.mkdir(parents=True, exist_ok=True)

    # ── JSON backup ───────────────────────────────────────────────────────────

    def session_file(self, day: date) -> Path:
        return self._session_dir / f"session_{day.isoformat()}.json"

    def append_record(
        self,
        rec: ClosedSignalRecord,
        day: date,
        session_start_ms: int,
    ) -> None:
        """Append one ClosedSignalRecord to the session JSON backup."""
        path = self.session_file(day)
        try:
            if path.exists():
                data = json.loads(path.read_text())
            else:
                data = {
                    "session_day": day.isoformat(),
                    "timezone":    str(self._session_tz),
                    "session_start": datetime.fromtimestamp(
                        session_start_ms / 1000, tz=self._session_tz
                    ).isoformat(),
                    "records": [],
                }
            data["records"].append(rec.to_dict())
            data["last_updated"] = datetime.fromtimestamp(
                rec.closed_at / 1000, tz=self._session_tz
            ).isoformat()
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=2))
            tmp.replace(path)
        except Exception as exc:
            logger.error("Failed to persist session record %s: %s", rec.signal_id, exc)

    def load_records_from_json(self, day: date) -> list[ClosedSignalRecord]:
        """Fallback JSON load used when SQLite is unavailable."""
        path = self.session_file(day)
        if not path.exists():
            return []
        try:
            data = json.loads(path.read_text())
            records = []
            for raw in data.get("records", []):
                try:
                    records.append(ClosedSignalRecord.from_dict(raw))
                except Exception as exc:
                    logger.warning("Skipping malformed JSON record: %s", exc)
            return records
        except Exception as exc:
            logger.error("Failed to read session file %s: %s", path, exc)
            return []

    # ── Live CSV ──────────────────────────────────────────────────────────────

    def append_csv(self, rec: ClosedSignalRecord) -> None:
        """
        Append one row to results/live/{SYMBOL}.csv.

        Schema matches BacktestResult.to_dict() exactly so summarize_result.py
        can consume results/live/ directly without transformation.
        """
        path = self._live_dir / f"{rec.symbol.replace('/', '')}.csv"
        try:
            write_header = not path.exists()
            with path.open("a", newline="", encoding="utf-8") as fh:
                writer = csv.DictWriter(fh, fieldnames=_HISTORY_COLUMNS)
                if write_header:
                    writer.writeheader()
                writer.writerow({
                    "id":           rec.signal_id,
                    "symbol":       rec.symbol,
                    "direction":    rec.direction,
                    "entry_dt":     _ms_to_utc_str(rec.entry_ts or rec.htf_ts),
                    "close_dt":     _ms_to_utc_str(rec.closed_at),
                    "entry":        round(rec.entry, 5),
                    "sl":           round(rec.sl, 5),
                    "tp1":          round(rec.tp1, 5),
                    "tp2":          round(rec.tp2, 5),
                    "rr":           round(rec.rr, 3),
                    "outcome":      rec.outcome,
                    "realized_rr":  round(rec.realized_rr, 4),
                    "htf_interval": rec.htf_interval,
                    "ltf_interval": rec.ltf_interval,
                    "pattern":      rec.pattern or "",
                    "wick_ratio":   round(rec.wick_ratio, 3),
                    "htf_high":     round(rec.htf_high, 5),
                    "htf_low":      round(rec.htf_low, 5),
                    "tp_level":     round(rec.tp_level, 5),
                    "ltf_high":     round(rec.ltf_high, 5),
                    "ltf_low":      round(rec.ltf_low, 5),
                    "chart":        "",
                })
            logger.debug(
                "[%s] CSV updated → %s  outcome=%s  rr=%.2f",
                rec.symbol, path.name, rec.outcome, rec.realized_rr,
            )
        except Exception as exc:
            logger.error("[%s] Failed to write live CSV: %s", rec.symbol, exc)
