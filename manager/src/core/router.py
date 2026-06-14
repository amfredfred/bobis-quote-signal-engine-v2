"""
core/router.py — Wires engine_server → consensus → gateway_server.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.core.consensus import ConsensusEngine
    from src.server.gateway_server import GatewayServer

logger = logging.getLogger(__name__)


class SignalRouter:

    def __init__(self, consensus: "ConsensusEngine", gateway: "GatewayServer") -> None:
        self._consensus = consensus
        self._gateway   = gateway
        self._drain_task: asyncio.Task | None = None

    def start(self) -> None:
        self._drain_task = asyncio.get_running_loop().create_task(self._drain_loop())

    def stop(self) -> None:
        if self._drain_task:
            self._drain_task.cancel()

    def on_signal(self, event: str, payload: dict, broker: str) -> None:
        """
        Called by EngineServer (from an async context) when a signal arrives.
        Only SIGNAL_TRIGGERED events are routed; everything else is dropped.
        """
        if event != "signal.triggered":
            return

        # Tag the originating broker (belt-and-suspenders — engine already sets it)
        payload = {**payload, "broker": broker}

        ready = self._consensus.push(payload, broker)
        if ready is not None:
            self._emit("signal.triggered", ready)

    # ── Internal ─────────────────────────────────────────────────────────────

    def _emit(self, event: str, signal: dict) -> None:
        consensus = signal.get("consensus", [])
        logger.info(
            "Signal %s (%s %s) → gateway  consensus=%s",
            signal.get("id"), signal.get("symbol"), signal.get("direction"), consensus,
        )
        self._gateway.broadcast(event, signal)

    async def _drain_loop(self) -> None:
        """Flush single-broker signals once their consensus window closes."""
        try:
            while True:
                await asyncio.sleep(1)
                for signal in self._consensus.drain_expired():
                    self._emit("signal.triggered", signal)
        except asyncio.CancelledError:
            pass
