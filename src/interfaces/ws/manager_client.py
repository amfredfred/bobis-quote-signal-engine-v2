"""
interfaces/ws/manager_client.py — Signal engine worker-mode publisher.

Replaces WebSocketServer when manager.mode = worker.
Connects outward to the Signal Manager and pushes every SIGNAL_TRIGGERED
event to it.  The manager fans the signal to the gateway.

Drop-in duck-type: exposes broadcast(event, payload) and stop().
The scheduler still runs normally; symbols are auto-subscribed from config.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Optional

from domain.entities.enums import SignalEvent

logger = logging.getLogger(__name__)

_RECONNECT_DELAYS = [2, 5, 10, 15, 30]   # seconds, last value repeats


class ManagerClient:

    def __init__(self, url: str, token: str, broker: str) -> None:
        self._url    = url
        self._token  = token
        self._broker = broker
        self._ws     = None
        self._ready  = asyncio.Event()
        self._stop   = asyncio.Event()
        self._task: Optional[asyncio.Task] = None

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        self._task = asyncio.get_running_loop().create_task(self._connect_loop())
        logger.info("ManagerClient starting → %s  broker=%s", self._url, self._broker)

    async def stop(self) -> None:
        self._stop.set()
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("ManagerClient stopped")

    # ── Broadcast (matches WebSocketServer.broadcast signature) ───────────────

    def broadcast(self, event: SignalEvent, payload: dict) -> None:
        if event != SignalEvent.SIGNAL_TRIGGERED:
            return
        task = asyncio.get_running_loop().create_task(
            self._send(event.value, payload)
        )
        task.add_done_callback(self._log_error)

    @staticmethod
    def _log_error(task: asyncio.Task) -> None:
        if not task.cancelled() and (exc := task.exception()):
            logger.warning("ManagerClient: send failed: %s", exc)

    # ── Internal ─────────────────────────────────────────────────────────────

    async def _send(self, event: str, payload: dict) -> None:
        if not self._ready.is_set() or self._ws is None:
            logger.warning("ManagerClient: not connected — signal %s dropped", payload.get("id"))
            return
        try:
            await self._ws.send(json.dumps({
                "type":    "signal",
                "event":   event,
                "payload": payload,
            }))
        except Exception as exc:
            logger.warning("ManagerClient: send error: %s", exc)
            self._ready.clear()

    async def _connect_loop(self) -> None:
        import websockets.client
        attempt = 0
        while not self._stop.is_set():
            try:
                await self._connect(websockets.client)
            except Exception as exc:
                logger.warning("ManagerClient: connection error: %s", exc)
            self._ready.clear()
            self._ws = None

            if self._stop.is_set():
                break
            delay = _RECONNECT_DELAYS[min(attempt, len(_RECONNECT_DELAYS) - 1)]
            attempt += 1
            logger.info("ManagerClient: reconnecting in %ds (attempt %d)", delay, attempt)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass

    async def _connect(self, ws_module) -> None:
        async with ws_module.connect(self._url) as ws:
            self._ws = ws
            await ws.send(json.dumps({
                "type":   "hello",
                "broker": self._broker,
                "token":  self._token,
            }))
            raw = await asyncio.wait_for(ws.recv(), timeout=10)
            msg = json.loads(raw)
            if msg.get("type") != "ack":
                raise ValueError(f"Unexpected handshake response: {msg}")

            self._ready.set()
            logger.info("ManagerClient: connected  broker=%s", self._broker)

            async for _ in ws:
                pass   # manager may send pong or future commands
