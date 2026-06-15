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
from collections import deque
from typing import Optional

from domain.entities.enums import SignalEvent

logger = logging.getLogger(__name__)

_RECONNECT_DELAYS = [2, 5, 10, 15, 30]   # seconds, last value repeats


class ManagerLogHandler(logging.Handler):
    """Forwards worker log records over the existing manager connection."""

    def __init__(self, client: "ManagerClient", level: int = logging.INFO) -> None:
        super().__init__(level)
        self._client = client

    def emit(self, record: logging.LogRecord) -> None:
        # Transport logs are excluded so a send failure cannot recurse.
        if record.name == __name__:
            return
        try:
            self._client.publish_log({
                "level": record.levelname,
                "logger": record.name,
                "message": self.format(record),
                "created_at": record.created,
            })
        except Exception:
            self.handleError(record)


class ManagerClient:

    def __init__(self, url: str, token: str, broker: str) -> None:
        self._url    = url
        self._token  = token
        self._broker = broker
        self._ws     = None
        self._ready  = asyncio.Event()
        self._stop   = asyncio.Event()
        self._task: Optional[asyncio.Task] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._pending_logs: deque[dict] = deque(maxlen=200)
        self._log_task: Optional[asyncio.Task] = None
        self._metrics_task: Optional[asyncio.Task] = None

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._task = self._loop.create_task(self._connect_loop())
        logger.info("ManagerClient starting → %s  broker=%s", self._url, self._broker)

    def start_metrics(self, build_snapshot_fn, interval_s: float = 5.0) -> None:
        """Start forwarding metrics snapshots to the manager every `interval_s` seconds."""
        self._metrics_task = asyncio.get_running_loop().create_task(
            self._metrics_loop(build_snapshot_fn, interval_s)
        )

    async def stop(self) -> None:
        self._stop.set()
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
        for attr in ("_task", "_log_task", "_metrics_task"):
            t = getattr(self, attr, None)
            if t:
                t.cancel()
                try:
                    await t
                except asyncio.CancelledError:
                    pass
        logger.info("ManagerClient stopped")

    # ── Broadcast (matches WebSocketServer.broadcast signature) ───────────────

    def broadcast(self, event: SignalEvent, payload: dict) -> None:
        if event != SignalEvent.SIGNAL_TRIGGERED:
            return
        task = asyncio.get_running_loop().create_task(
            self._send("signal", event.value, payload)
        )
        task.add_done_callback(self._log_error)

    def create_log_handler(self, level: int = logging.INFO) -> ManagerLogHandler:
        return ManagerLogHandler(self, level)

    def publish_log(self, payload: dict) -> None:
        if self._loop is None:
            return
        self._loop.call_soon_threadsafe(self._queue_log, payload)

    def _queue_log(self, payload: dict) -> None:
        self._pending_logs.append(payload)
        self._start_log_flush()

    def _start_log_flush(self) -> None:
        if not self._ready.is_set() or self._ws is None:
            return
        if self._log_task and not self._log_task.done():
            return
        self._log_task = asyncio.create_task(self._flush_logs())

    async def _flush_logs(self) -> None:
        while self._pending_logs and self._ready.is_set() and self._ws is not None:
            payload = self._pending_logs.popleft()
            await self._send("log", "log.record", payload)

    @staticmethod
    def _log_error(task: asyncio.Task) -> None:
        if not task.cancelled() and (exc := task.exception()):
            logger.warning("ManagerClient: send failed: %s", exc)

    # ── Internal ─────────────────────────────────────────────────────────────

    async def _send(self, message_type: str, event: str, payload: dict) -> None:
        if not self._ready.is_set() or self._ws is None:
            if message_type == "signal":
                logger.warning("ManagerClient: not connected — signal %s dropped", payload.get("id"))
            return
        try:
            await self._ws.send(json.dumps({
                "type":    message_type,
                "event":   event,
                "payload": payload,
            }))
        except Exception as exc:
            if message_type == "signal":
                logger.warning("ManagerClient: send error: %s", exc)
            self._ready.clear()

    async def _metrics_loop(self, build_snapshot_fn, interval_s: float) -> None:
        try:
            while True:
                await asyncio.sleep(interval_s)
                if not self._ready.is_set():
                    continue
                try:
                    snapshot = build_snapshot_fn()
                    await self._send("metrics", "metrics.snapshot", snapshot)
                except Exception as exc:
                    logger.debug("ManagerClient: metrics send failed: %s", exc)
        except asyncio.CancelledError:
            pass

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
            self._start_log_flush()
            logger.info("ManagerClient: connected  broker=%s", self._broker)

            async for _ in ws:
                pass   # manager may send pong or future commands
