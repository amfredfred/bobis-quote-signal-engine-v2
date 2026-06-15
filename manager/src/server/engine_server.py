"""
server/engine_server.py — Accepts WebSocket connections from signal engine workers.

Protocol (JSON over WS):
  Engine → Manager  {"type": "hello",  "broker": "fundednext", "token": "..."}
  Manager → Engine  {"type": "ack",    "broker": "fundednext"}
  Engine → Manager  {"type": "signal", "event": "signal.triggered", "payload": {...}}
  Engine → Manager  {"type": "log",    "event": "log.record",       "payload": {...}}
  Engine → Manager  {"type": "ping"}
  Manager → Engine  {"type": "pong"}

One connection per broker. If a broker reconnects, the old connection is replaced.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Callable

import websockets.server

logger = logging.getLogger(__name__)

OnSignal = Callable[[str, dict, str], None]   # (event, payload, broker)
OnEvent = Callable[[str, dict], None]          # (event, payload)


class EngineServer:

    def __init__(
        self,
        host: str,
        port: int,
        token: str,
        on_signal: OnSignal,
        on_event: OnEvent | None = None,
    ) -> None:
        self._host      = host
        self._port      = port
        self._token     = token
        self._on_signal = on_signal
        self._on_event  = on_event
        self._connected: dict[str, websockets.server.ServerConnection] = {}
        self._server: websockets.server.WebSocketServer | None = None
        # Per-broker tracking
        self._signals_received: dict[str, int]  = {}
        self._connected_at:     dict[str, float] = {}
        self._latest_metrics:   dict[str, dict]  = {}

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        self._server = await websockets.server.serve(
            self._handle, self._host, self._port
        )
        logger.info("EngineServer listening on ws://%s:%d", self._host, self._port)

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()
        logger.info("EngineServer stopped")

    # ── Status ────────────────────────────────────────────────────────────────

    def connected_brokers(self) -> list[str]:
        return list(self._connected.keys())

    def broker_stats(self) -> dict[str, dict]:
        """Per-broker stats included in the metrics snapshot sent to the gateway."""
        all_brokers = sorted(set(self._connected) | set(self._signals_received))
        return {
            broker: {
                "connected":        broker in self._connected,
                "connected_since":  self._connected_at.get(broker),
                "signals_received": self._signals_received.get(broker, 0),
                "latest_metrics":   self._latest_metrics.get(broker),
            }
            for broker in all_brokers
        }

    # ── Connection handler ────────────────────────────────────────────────────

    async def _handle(self, websocket: websockets.server.ServerConnection) -> None:
        broker: str | None = None
        try:
            raw = await asyncio.wait_for(websocket.recv(), timeout=10)
            msg = json.loads(raw)

            if msg.get("type") != "hello":
                await websocket.close(1008, "expected hello")
                return

            if msg.get("token") != self._token:
                await websocket.close(1008, "invalid token")
                logger.warning("EngineServer: rejected - bad token from %s", websocket.remote_address)
                return

            broker = str(msg.get("broker", "unknown"))
            self._connected[broker] = websocket
            self._connected_at[broker] = time.time()
            await websocket.send(json.dumps({"type": "ack", "broker": broker}))
            logger.info("EngineServer: broker '%s' connected  [%d total]", broker, len(self._connected))

            async for raw_msg in websocket:
                await self._on_message(broker, websocket, raw_msg)

        except asyncio.TimeoutError:
            logger.debug("EngineServer: handshake timeout from %s", getattr(websocket, "remote_address", "?"))
        except websockets.exceptions.ConnectionClosed:
            pass
        except Exception as exc:
            logger.debug("EngineServer: error for '%s': %s", broker, exc)
        finally:
            if broker and self._connected.get(broker) is websocket:
                del self._connected[broker]
                logger.info("EngineServer: broker '%s' disconnected  [%d total]", broker, len(self._connected))

    async def _on_message(
        self,
        broker: str,
        websocket: websockets.server.ServerConnection,
        raw: str,
    ) -> None:
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            return

        msg_type = msg.get("type")

        if msg_type == "signal":
            event   = msg.get("event", "")
            payload = msg.get("payload", {})
            if event and isinstance(payload, dict):
                self._signals_received[broker] = self._signals_received.get(broker, 0) + 1
                self._on_signal(event, payload, broker)

        elif msg_type == "metrics":
            # Workers forward their own metrics snapshot so the gateway can
            # surface per-source metrics to the dashboard.
            payload = msg.get("payload", {})
            if isinstance(payload, dict):
                self._latest_metrics[broker] = payload
                logger.debug("EngineServer: metrics update from '%s'", broker)

        elif msg_type == "log":
            event = msg.get("event", "log.record")
            payload = msg.get("payload", {})
            if self._on_event and isinstance(event, str) and isinstance(payload, dict):
                self._on_event(event, {**payload, "broker": broker})

        elif msg_type == "ping":
            try:
                await websocket.send(json.dumps({"type": "pong"}))
            except Exception:
                pass
