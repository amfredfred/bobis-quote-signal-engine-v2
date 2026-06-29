"""
core/consensus.py — Cross-broker signal agreement engine.

Each signal engine runs independently against its own MT5 terminal.
When two or more brokers detect the same setup (same symbol, direction,
and setup candle open timestamp), that's a consensus signal — higher
conviction, emitted immediately.

Single-broker signals are held for `window_ms` then emitted alone.
The consensus list travels in the payload so downstream consumers
can weight or log it.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class _Pending:
    signal:     dict
    brokers:    list[str]
    expires_at: float


class ConsensusEngine:

    def __init__(self, window_ms: int = 0) -> None:
        self._window    = window_ms / 1000
        self._pending:  dict[str, _Pending] = {}

    # ── Public API ────────────────────────────────────────────────────────────

    def push(self, signal: dict, broker: str) -> dict | None:
        """
        Register a signal from `broker`.

        Returns the enriched signal dict to emit now if:
          - a second broker just confirmed the same setup, OR
          - the signal already exists but same broker re-sent it (idempotent).

        Returns None if the signal is buffered and waiting for more brokers.
        """
        key = self._key(signal)
        now = time.monotonic()

        existing = self._pending.get(key)
        if existing is not None:
            if broker in existing.brokers:
                return None  # duplicate from same engine, ignore
            existing.brokers.append(broker)
            del self._pending[key]
            return {**existing.signal, "consensus": list(existing.brokers)}

        # First broker — buffer and wait
        self._pending[key] = _Pending(
            signal=signal,
            brokers=[broker],
            expires_at=now + self._window,
        )
        return None

    def drain_expired(self) -> list[dict]:
        """
        Call periodically. Returns signals whose consensus window has closed
        (i.e. only one broker fired within the window).
        """
        now   = time.monotonic()
        ready = [(k, p) for k, p in list(self._pending.items()) if p.expires_at <= now]
        result = []
        for k, p in ready:
            del self._pending[k]
            result.append({**p.signal, "consensus": list(p.brokers)})
        return result

    # ── Internal ──────────────────────────────────────────────────────────────

    @staticmethod
    def _key(signal: dict) -> str:
        # setupCandleOpenAt is the anchor timestamp for the setup candle —
        # identical across brokers for the same real-world candle after UTC normalisation.
        ts = (
            signal.get("setupCandleOpenAt")
            or signal.get("createdAt")
            or 0
        )
        return f"{signal.get('symbol', '')}|{signal.get('direction', '')}|{ts}"
