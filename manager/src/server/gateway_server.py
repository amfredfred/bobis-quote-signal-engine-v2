"""
server/gateway_server.py — Serves the NestJS execution gateway.

Speaks exactly the same protocol as the signal engine's WebSocketServer so
the gateway's SignalEngineSubscriberService requires zero changes:

  Gateway → Manager  {action: "subscribe",         symbols: [...]}
  Gateway → Manager  {action: "unsubscribe",        symbols: [...]}
  Gateway → Manager  {action: "subscribe_metrics"}
  Gateway → Manager  {action: "unsubscribe_metrics"}
  (Gateway also sends WS-level PING frames — websockets library auto-pongs)

  Manager → Gateway  {event: "connected",       payload: {clientId, supported_symbols}}
  Manager → Gateway  {event: "subscribed",       payload: {symbols, newSymbols}}
  Manager → Gateway  {event: "unsubscribed",     payload: {symbols}}
  Manager → Gateway  {event: "signal.triggered", payload: {..., broker, consensus}}
  Manager → Gateway  {event: "log.record",       payload: {..., broker}}
  Manager → Gateway  {event: "metrics.snapshot", payload: {...}}

Authentication:
  When gateway_secret is set the NestJS appends ?token=<secret> to the URL.
  Connections without a valid token are rejected with 1008.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlparse

import websockets.server

logger = logging.getLogger(__name__)


def _extract_token(websocket: Any) -> str:
    """Pull the ?token= query parameter from the WS upgrade request path."""
    try:
        path = websocket.request.path        # websockets v14+ asyncio API
    except AttributeError:
        path = getattr(websocket, "path", "/")   # legacy API fallback
    try:
        qs = parse_qs(urlparse(path).query)
        return qs.get("token", [""])[0]
    except Exception:
        return ""


@dataclass
class _Client:
    client_id:   str
    ws:          Any
    symbols:     set[str] = field(default_factory=set)
    metrics_sub: bool     = False


class GatewayServer:

    def __init__(
        self,
        host:              str,
        port:              int,
        supported_symbols: tuple[str, ...],
        secret:            str = "",
    ) -> None:
        self._host      = host
        self._port      = port
        self._supported = set(supported_symbols)
        self._secret    = secret
        self._clients:  dict[str, _Client] = {}
        self._counter   = 0
        self._server: Any = None
        self._metrics_task: asyncio.Task | None = None
        self._started_at = time.time()
        self._signals_delivered = 0
        self._stats_provider = None

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        self._server = await websockets.server.serve(
            self._handle, self._host, self._port
        )
        self._metrics_task = asyncio.get_running_loop().create_task(
            self._metrics_loop()
        )
        logger.info("GatewayServer listening on ws://%s:%d", self._host, self._port)

    async def stop(self) -> None:
        if self._metrics_task:
            self._metrics_task.cancel()
        if self._server:
            self._server.close()
            await self._server.wait_closed()
        logger.info("GatewayServer stopped")

    def set_stats_provider(self, fn) -> None:
        """Register a callable that returns per-broker stats for by_source."""
        self._stats_provider = fn

    # ── Broadcast (called by router) ──────────────────────────────────────────

    def broadcast(self, event: str, payload: dict) -> None:
        asyncio.get_running_loop().create_task(
            self._broadcast_async(event, payload)
        )

    def broadcast_event(self, event: str, payload: dict) -> None:
        """Broadcast to all clients with no symbol filter (for log/rejection events)."""
        asyncio.get_running_loop().create_task(
            self._broadcast_event_async(event, payload)
        )

    # ── Internal ─────────────────────────────────────────────────────────────

    async def _broadcast_event_async(self, event: str, payload: dict) -> None:
        message = json.dumps({"event": event, "payload": payload})
        dead: list[str] = []
        for cid, client in list(self._clients.items()):
            try:
                await client.ws.send(message)
            except Exception:
                dead.append(cid)
        for cid in dead:
            self._clients.pop(cid, None)

    async def _broadcast_async(self, event: str, payload: dict) -> None:
        symbol  = (payload.get("symbol") or "").upper()
        message = json.dumps({"event": event, "payload": payload})
        dead: list[str] = []

        for cid, client in list(self._clients.items()):
            if symbol and symbol not in client.symbols:
                continue
            try:
                await client.ws.send(message)
                self._signals_delivered += 1
            except Exception:
                dead.append(cid)

        for cid in dead:
            self._clients.pop(cid, None)

    async def _metrics_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(5)
                subs = [c for c in self._clients.values() if c.metrics_sub]
                if not subs:
                    continue
                message = json.dumps({
                    "event": "metrics.snapshot",
                    "payload": self._build_metrics(),
                })
                for client in subs:
                    try:
                        await client.ws.send(message)
                    except Exception:
                        pass
        except asyncio.CancelledError:
            pass

    def _build_metrics(self) -> dict:
        uptime_ms  = int((time.time() - self._started_at) * 1000)
        by_source  = self._stats_provider() if self._stats_provider else {}

        total_received = sum(v.get("signals_received", 0) for v in by_source.values())

        # Aggregate scheduler rows and scanner counters/gauges across all broker engines.
        agg_scheduler: list[dict] = []
        for broker, broker_stats in by_source.items():
            for row in (broker_stats.get("scheduler") or []):
                agg_scheduler.append({**row, "broker": broker})

        agg_counters: dict[str, int] = {}
        agg_gauges:   dict[str, float] = {}
        for broker_stats in by_source.values():
            bm = broker_stats.get("latest_metrics") or {}
            for k, v in (bm.get("raw_counters") or {}).items():
                agg_counters[k] = agg_counters.get(k, 0) + int(v)
            for k, v in (bm.get("raw_gauges") or {}).items():
                # Gauges: take the worst/max so lag and errors surface.
                cur = agg_gauges.get(k, 0.0)
                agg_gauges[k] = max(cur, float(v))

        def _c(key: str) -> int:
            return agg_counters.get(key, 0)
        def _g(key: str) -> float:
            return agg_gauges.get(key, 0.0)

        return {
            "ts": int(time.time() * 1000),
            "system": {
                "uptime_ms": uptime_ms,
                "uptime_s":  uptime_ms // 1000,
                "memory_mb": None,
            },
            "metrics": {
                # Gateway-level
                "signals_delivered":      self._signals_delivered,
                "signals_received":       total_received,
                "gateway_clients":        len(self._clients),
                "engine_count":           len(by_source),
                "engine_online":          sum(1 for v in by_source.values() if v.get("connected")),
                # Aggregated scanner metrics (sum across all broker engines)
                "raw_counters":           agg_counters,
                "raw_gauges":             agg_gauges,
                "scanner_ticks":          _c("scanner.ticks"),
                "scanner_tick_errors":    _c("scanner.tick_errors"),
                "analysis_started":       _c("scanner.analysis_started"),
                "signals_pending":        _c("signals.pending"),
                "signals_emitted":        _c("signals.emitted"),
                "signals_triggered":      _c("signals.triggered"),
                "signals_stale_skipped":  _c("signals.stale_skipped"),
                "signals_dedup_blocked":  _c("signals.dedup_blocked"),
                "signals_decision_blocked": _c("signals.decision_blocked"),
                "signals_no_rejection":   _c("signals.no_rejection"),
                "mt5_calls":              _c("mt5.calls"),
                "mt5_errors":             _c("mt5.errors"),
                "last_emit_lag_ms":       _g("latency.last_signal_emit_lag_ms"),
                "last_scan_lag_ms":       _g("scanner.scan_lag_ms"),
                "last_analysis_ms":       _g("scanner.analysis_ms"),
            },
            "latency":        {},
            "scheduler":      agg_scheduler,
            "active_signals":  [],
            "active_zones":    [],
            "api": {
                "calls_last_min": None,
                "by_source":      None,
            },
            # Per-source breakdown — keyed by broker name.
            "by_source": by_source,
        }

    # ── Connection handler ────────────────────────────────────────────────────

    async def _handle(self, websocket: Any) -> None:
        # Token auth — only enforced when gateway_secret is configured.
        if self._secret:
            provided = _extract_token(websocket)
            if provided != self._secret:
                logger.warning(
                    "GatewayServer: rejected connection — bad or missing token"
                )
                await websocket.close(1008, "invalid token")
                return

        self._counter += 1
        client_id = f"gw-{self._counter}"
        client    = _Client(client_id=client_id, ws=websocket)
        self._clients[client_id] = client
        logger.info("GatewayServer: client %s connected", client_id)

        try:
            await websocket.send(json.dumps({
                "event": "connected",
                "payload": {
                    "clientId":          client_id,
                    "message":           "Connected to Signal Manager.",
                    "supported_symbols": sorted(self._supported),
                },
            }))

            async for raw in websocket:
                await self._on_message(client, raw)

        except websockets.exceptions.ConnectionClosed:
            pass
        except Exception as exc:
            logger.debug("GatewayServer: client %s error: %s", client_id, exc)
        finally:
            self._clients.pop(client_id, None)
            logger.info("GatewayServer: client %s disconnected", client_id)

    async def _on_message(self, client: _Client, raw: str | bytes) -> None:
        try:
            msg = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return

        action  = msg.get("action", "")
        symbols = [s.upper() for s in msg.get("symbols", []) if isinstance(s, str)]

        if action == "subscribe":
            valid = [s for s in symbols if s in self._supported]
            client.symbols.update(valid)
            await client.ws.send(json.dumps({
                "event": "subscribed",
                "payload": {
                    "symbols":    list(client.symbols),
                    "newSymbols": valid,
                },
            }))
            logger.debug(
                "GatewayServer: %s subscribed to %s", client.client_id, valid
            )

        elif action == "unsubscribe":
            for s in symbols:
                client.symbols.discard(s)
            await client.ws.send(json.dumps({
                "event": "unsubscribed",
                "payload": {"symbols": list(client.symbols)},
            }))

        elif action == "subscribe_metrics":
            client.metrics_sub = True
            await client.ws.send(json.dumps({
                "event": "metrics.snapshot",
                "payload": self._build_metrics(),
            }))
            await client.ws.send(json.dumps({
                "event":   "metrics_subscribed",
                "payload": {"interval_ms": 5000},
            }))

        elif action == "unsubscribe_metrics":
            client.metrics_sub = False
