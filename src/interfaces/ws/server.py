"""
interfaces/ws/server.py — pure asyncio WebSocket server (RFC 6455).

No third-party WebSocket library. All external dependencies are injected
at construction time — no module-level _cfg singleton.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import struct
import time
import uuid
from typing import Optional
from urllib.parse import parse_qs, urlparse

from domain.assets.profiles import SUPPORTED_SYMBOLS, normalize_symbol
from domain.entities.enums import SignalEvent

logger = logging.getLogger(__name__)

_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

_OP_CONTINUATION = 0x0
_OP_TEXT         = 0x1
_OP_BINARY       = 0x2
_OP_CLOSE        = 0x8
_OP_PING         = 0x9
_OP_PONG         = 0xA

MAX_FRAME_SIZE          = 1024 * 1024
MAX_SYMBOLS_PER_CLIENT  = 100
MAX_MESSAGE_QUEUE_SIZE  = 100
# 6.1 — Abuse protection: subscribe churn hits the scheduler much harder than
# other messages, and connection churn bypasses the concurrent-client cap.
MAX_SUBSCRIBES_PER_MINUTE = 12
MAX_CONNECTS_PER_MINUTE_PER_IP = 12


# ── Frame codec ───────────────────────────────────────────────────────────────

def _encode_frame(payload: bytes, opcode: int = _OP_TEXT) -> bytes:
    length = len(payload)
    header = bytearray()
    header.append(0x80 | opcode)
    if length <= 125:
        header.append(length)
    elif length <= 65535:
        header.append(126)
        header.extend(struct.pack(">H", length))
    else:
        header.append(127)
        header.extend(struct.pack(">Q", length))
    return bytes(header) + payload


def _make_accept_key(client_key: str) -> str:
    combined = (client_key + _WS_GUID).encode("utf-8")
    return base64.b64encode(hashlib.sha1(combined).digest()).decode("utf-8")


# ── Rate limiter ──────────────────────────────────────────────────────────────

class _RateLimiter:
    def __init__(self, max_per_minute: int = 60) -> None:
        self._max   = max_per_minute
        self._calls: dict[str, list[float]] = {}

    def is_allowed(self, client_id: str) -> bool:
        now   = time.time()
        calls = self._calls.setdefault(client_id, [])
        self._calls[client_id] = [t for t in calls if now - t < 60]
        if len(self._calls[client_id]) >= self._max:
            return False
        self._calls[client_id].append(now)
        return True

    def remove(self, client_id: str) -> None:
        """Discard tracking state for a disconnected client to prevent memory growth."""
        self._calls.pop(client_id, None)

    def prune_stale(self) -> None:
        """
        6.1 — Drop keys with no calls in the last window. Needed for limiters
        keyed by IP, where remove() is never called for a specific key.
        """
        now = time.time()
        stale = [
            key
            for key, calls in self._calls.items()
            if not any(now - t < 60 for t in calls)
        ]
        for key in stale:
            del self._calls[key]


# ── WsClient ──────────────────────────────────────────────────────────────────

class WsClient:
    def __init__(
        self,
        client_id: str,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        self.client_id   = client_id
        self.reader      = reader
        self.writer      = writer
        self.symbols:    set[str]                = set()
        self.connected   = True
        self._send_lock  = asyncio.Lock()
        self._send_queue: asyncio.Queue[Optional[bytes]] = asyncio.Queue(
            maxsize=MAX_MESSAGE_QUEUE_SIZE
        )
        self._queue_task: Optional[asyncio.Task] = None

    async def start_queue_processor(self) -> None:
        if self._queue_task is None:
            self._queue_task = asyncio.create_task(self._process_queue())

    async def _process_queue(self) -> None:
        while self.connected:
            try:
                data = await self._send_queue.get()
                if data is None:          # poison pill — time to stop
                    break
                async with self._send_lock:
                    self.writer.write(data)
                    await self.writer.drain()
            except asyncio.CancelledError:
                # FIX 3: re-raise so the task actually cancels instead of
                # silently swallowing the signal and continuing to run.
                raise
            except (ConnectionResetError, BrokenPipeError):
                self.connected = False
                break
            except Exception as exc:
                logger.warning("[%s] Send error: %s", self.client_id, exc)
                self.connected = False
                break

    async def send_json(self, data: dict) -> bool:
        if not self.connected:
            return False
        try:
            payload = json.dumps(data, default=str).encode("utf-8")
            frame   = _encode_frame(payload, _OP_TEXT)
            # FIX 2: use put_nowait() so QueueFull is actually reachable.
            # await queue.put() blocks until space is available and never
            # raises QueueFull, making the except branch permanently dead.
            try:
                self._send_queue.put_nowait(frame)
                return True
            except asyncio.QueueFull:
                logger.warning("[%s] Send queue full — dropping message", self.client_id)
                return False
        except Exception as exc:
            logger.warning("[%s] Serialise error: %s", self.client_id, exc)
            self.connected = False
            return False

    async def send_ping(self) -> None:
        if not self.connected:
            return
        try:
            frame = _encode_frame(b"ping", _OP_PING)
            async with self._send_lock:
                self.writer.write(frame)
                await self.writer.drain()
        except Exception:
            self.connected = False

    async def close(self) -> None:
        self.connected = False
        if self._queue_task:
            # Send poison pill; if the queue is full just cancel directly.
            try:
                self._send_queue.put_nowait(None)
            except asyncio.QueueFull:
                pass
            try:
                await asyncio.wait_for(self._queue_task, timeout=2.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._queue_task.cancel()
            self._queue_task = None

        # FIX 4: explicit try/except blocks preserve the awaits that flush
        # the close frame.  The lambda loop in the previous version was
        # synchronous and silently skipped drain(), so the peer never
        # received the RFC 6455 close handshake.
        try:
            self.writer.write(_encode_frame(b"", _OP_CLOSE))
            await self.writer.drain()
        except Exception:
            pass
        try:
            self.writer.close()
        except Exception:
            pass
        try:
            if self.writer.transport:
                self.writer.transport.abort()
        except Exception:
            pass


# ── WebSocketServer ───────────────────────────────────────────────────────────

class WebSocketServer:
    """
    Async WebSocket server — handles HTTP upgrade, frame codec,
    subscription management, and signal event broadcasting.

    All dependencies are injected; no module-level singletons.
    """

    def __init__(
        self,
        scheduler,       # SignalScheduler
        service,         # SignalService
        market_data,     # MarketDataClient
        settings,        # Settings
        metrics=None,    # MetricsCollector | None
    ) -> None:
        self._scheduler     = scheduler
        self._service       = service
        self._md            = market_data
        self._cfg           = settings
        self._metrics       = metrics
        self._clients:      dict[str, WsClient]          = {}
        self._server:       Optional[asyncio.AbstractServer] = None
        self._metrics_subs: set[str]                     = set()
        self._bg_tasks:     list[asyncio.Task]           = []
        self._conn_tasks:   set[asyncio.Task]            = set()
        self._rate_limiter  = _RateLimiter()
        # 6.1 — Tighter limiter for subscribe/unsubscribe (scheduler churn)
        self._sub_limiter   = _RateLimiter(MAX_SUBSCRIBES_PER_MINUTE)
        # 6.1 — Per-IP connection-attempt limiter (keyed by peer address)
        self._conn_limiter  = _RateLimiter(MAX_CONNECTS_PER_MINUTE_PER_IP)

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self, host: str, port: int) -> None:
        self._server = await asyncio.start_server(self._accept_connection, host, port)
        logger.info("WebSocket server listening on ws://%s:%d", host, port)
        self._bg_tasks.append(asyncio.create_task(self._heartbeat()))
        self._bg_tasks.append(asyncio.create_task(self._metrics_push_loop()))

    async def stop(self) -> None:
        if self._server:
            self._server.close()
        for t in self._bg_tasks:
            t.cancel()
        if self._bg_tasks:
            await asyncio.gather(*self._bg_tasks, return_exceptions=True)
        self._bg_tasks.clear()

        close_tasks = [c.close() for c in list(self._clients.values())]
        if close_tasks:
            await asyncio.gather(*close_tasks, return_exceptions=True)
        self._clients.clear()

        for t in list(self._conn_tasks):
            t.cancel()
        if self._conn_tasks:
            await asyncio.gather(*self._conn_tasks, return_exceptions=True)
        self._conn_tasks.clear()

        if self._server:
            try:
                await asyncio.wait_for(self._server.wait_closed(), timeout=3.0)
            except asyncio.TimeoutError:
                logger.warning("wait_closed() timed out — continuing shutdown")
        logger.info("WebSocket server stopped")

    # ── Broadcast ─────────────────────────────────────────────────────────────

    def broadcast(self, event: SignalEvent, payload: dict) -> None:
        symbol = payload.get("symbol") or payload.get("signal", {}).get("symbol")
        task = asyncio.get_running_loop().create_task(
            self._broadcast_async(event, payload, symbol)
        )
        task.add_done_callback(self._on_broadcast_done)

    @staticmethod
    def _on_broadcast_done(task: asyncio.Task) -> None:
        if not task.cancelled() and (exc := task.exception()):
            logger.error("broadcast task raised an unhandled exception: %s", exc, exc_info=exc)

    async def _broadcast_async(
        self, event: SignalEvent, payload: dict, symbol: Optional[str]
    ) -> None:
        message = {"event": event.value, "payload": payload}
        dead, tasks = [], []
        for cid, client in list(self._clients.items()):
            if not client.connected:
                dead.append(cid)
                continue
            if symbol is None or symbol.upper() in client.symbols:
                tasks.append(client.send_json(message))
        for i in range(0, len(tasks), 100):
            await asyncio.gather(*tasks[i : i + 100], return_exceptions=True)
        for cid in dead:
            self._remove_client(cid)

        if self._metrics and event == SignalEvent.SIGNAL_TRIGGERED and symbol:
            sig_id   = payload.get("id", "")
            fired_at = payload.get("triggeredAt") or payload.get("createdAt") or 0
            if sig_id:
                self._metrics.signal_broadcast_done(sig_id, symbol, fired_at)

    # ── Background tasks ──────────────────────────────────────────────────────

    async def _heartbeat(self) -> None:
        try:
            while True:
                await asyncio.sleep(30)
                # 6.1 — The per-IP limiter has no per-client remove() hook;
                # prune idle IPs here so the map cannot grow without bound.
                self._conn_limiter.prune_stale()
                dead, ping_tasks = [], []
                for cid, client in list(self._clients.items()):
                    if not client.connected:
                        dead.append(cid)
                    else:
                        ping_tasks.append(client.send_ping())
                if ping_tasks:
                    await asyncio.gather(*ping_tasks, return_exceptions=True)
                for cid in dead:
                    self._remove_client(cid)
        except asyncio.CancelledError:
            pass

    async def _metrics_push_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(5)
                if not self._metrics_subs or not self._metrics:
                    continue
                try:
                    snapshot = self._metrics.build_snapshot()
                    message  = {"event": "metrics.snapshot", "payload": snapshot}
                    dead, tasks = [], []
                    for cid in list(self._metrics_subs):
                        client = self._clients.get(cid)
                        if not client or not client.connected:
                            dead.append(cid)
                            continue
                        tasks.append(client.send_json(message))
                    if tasks:
                        await asyncio.gather(*tasks, return_exceptions=True)
                    for cid in dead:
                        self._metrics_subs.discard(cid)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.error("Metrics push error: %s", exc)
        except asyncio.CancelledError:
            pass

    # ── Connection handling ───────────────────────────────────────────────────

    async def _accept_connection(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        peer      = writer.get_extra_info("peername", ("?", 0))
        client_id = str(uuid.uuid4())[:8]
        logger.info("[%s] Connection from %s:%s", client_id, *peer)

        # 6.1 — Per-IP connection-attempt rate limit: the concurrent-client cap
        # below does not stop rapid connect/disconnect churn from one host.
        peer_ip = str(peer[0]) if peer else "?"
        if not self._conn_limiter.is_allowed(peer_ip):
            logger.warning("[%s] Rejected — connection rate limit for %s", client_id, peer_ip)
            writer.write(
                b"HTTP/1.1 429 Too Many Requests\r\n"
                b"Content-Type: text/plain\r\n\r\n"
                b"Too many connection attempts\n"
            )
            await writer.drain()
            writer.close()
            return

        if len(self._clients) >= self._cfg.max_ws_clients:
            logger.warning("[%s] Rejected — connection cap reached", client_id)
            writer.write(
                b"HTTP/1.1 503 Service Unavailable\r\n"
                b"Content-Type: text/plain\r\n\r\n"
                b"Connection limit reached\n"
            )
            await writer.drain()
            writer.close()
            return

        try:
            success = await self._handshake(reader, writer)
        except Exception as exc:
            logger.warning("[%s] Handshake failed: %s", client_id, exc)
            writer.close()
            return

        if not success:
            writer.close()
            return

        client = WsClient(client_id, reader, writer)
        await client.start_queue_processor()
        self._clients[client_id] = client

        task = asyncio.current_task()
        if task:
            self._conn_tasks.add(task)

        logger.info("[%s] WS upgrade complete — %d clients", client_id, len(self._clients))

        if self._metrics:
            self._metrics.update_ws_client(client_id, [])
            self._metrics.record_ws_event(client_id, "connected")

        await client.send_json({
            "event": "connected",
            "payload": {
                "clientId": client_id,
                "message": "Connected to Signal Engine. Send {action: subscribe, symbols: [...]} to start.",
                "supported_symbols": sorted(SUPPORTED_SYMBOLS),
            },
        })

        try:
            await self._read_loop(client)
        except Exception as exc:
            logger.debug("[%s] Read loop ended: %s", client_id, exc)
        finally:
            if task:
                self._conn_tasks.discard(task)
            self._remove_client(client_id)

    def _remove_client(self, client_id: str) -> None:
        client = self._clients.pop(client_id, None)
        if client:
            self._scheduler.unsubscribe(client_id)
            self._metrics_subs.discard(client_id)
            self._rate_limiter.remove(client_id)   # FIX 5: prevent memory growth
            self._sub_limiter.remove(client_id)
            if self._metrics:
                self._metrics.record_ws_event(client_id, "disconnected")
            # FIX 1: schedule async teardown so the queue processor task,
            # writer, and transport are actually cleaned up.  Without this
            # every natural disconnect left a zombie queue task and an
            # unclosed socket.
            # BUG-11: asyncio.get_event_loop() is deprecated in Python 3.10+
            # (emits DeprecationWarning, raises RuntimeError in 3.12 when there
            # is no current event loop). Use get_running_loop() instead — this
            # method is always called from within a running coroutine context.
            asyncio.get_running_loop().create_task(client.close())
            logger.info("[%s] Disconnected — %d clients remain", client_id, len(self._clients))

    async def _handshake(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> bool:
        raw     = await asyncio.wait_for(reader.read(4096), timeout=10)
        request = raw.decode("utf-8", errors="replace")
        lines   = request.split("\r\n")

        if not lines or not lines[0].startswith("GET"):
            return False

        headers: dict[str, str] = {}
        for line in lines[1:]:
            if ": " in line:
                key, _, val = line.partition(": ")
                headers[key.strip().lower()] = val.strip()

        ws_key  = headers.get("sec-websocket-key", "")
        upgrade = headers.get("upgrade", "").lower()

        if upgrade != "websocket" or not ws_key:
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\n\r\n"
                b"Signal Engine WebSocket Server\n"
            )
            await writer.drain()
            return False

        if self._cfg.ws_secret:
            request_line = lines[0] if lines else ""
            path         = request_line.split(" ")[1] if " " in request_line else "/"
            qs           = parse_qs(urlparse(path).query)
            provided     = headers.get("sec-websocket-protocol", "") or  qs.get("token", [""])[0]
            # SECURITY NOTE: this is a shared symmetric secret — any holder can
            # connect and inject arbitrary signals to all subscribed engines.
            # Before multi-tenant deployment, replace with asymmetric auth
            # (e.g. mTLS or per-client signed tokens) so the gateway cannot be
            # impersonated even if the secret leaks.
            if not hmac.compare_digest(provided, self._cfg.ws_secret):
                logger.warning("Rejected unauthenticated WS connection")
                writer.write(b"HTTP/1.1 401 Unauthorized\r\n\r\nInvalid or missing token\n")
                await writer.drain()
                return False

        response = (
            "HTTP/1.1 101 Switching Protocols\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Accept: {_make_accept_key(ws_key)}\r\n"
            "\r\n"
        )
        writer.write(response.encode("utf-8"))
        await writer.drain()
        return True

    # ── Frame reader ──────────────────────────────────────────────────────────

    async def _read_loop(self, client: WsClient) -> None:
        while client.connected:
            try:
                frame = await asyncio.wait_for(self._read_frame(client.reader), timeout=60)
            except asyncio.TimeoutError:
                await client.send_ping()
                continue
            except (ConnectionResetError, asyncio.IncompleteReadError, EOFError):
                break
            if frame is None:
                break
            opcode, payload = frame
            if opcode == _OP_CLOSE:
                break
            elif opcode == _OP_PING:
                try:
                    client.writer.write(_encode_frame(payload, _OP_PONG))
                    await client.writer.drain()
                except Exception:
                    break
            elif opcode == _OP_TEXT:
                try:
                    await self._handle_message(client, payload.decode("utf-8", errors="replace"))
                except Exception as exc:
                    logger.error("[%s] Handler error: %s", client.client_id, exc, exc_info=True)

    async def _read_frame(
        self, reader: asyncio.StreamReader
    ) -> Optional[tuple[int, bytes]]:
        header = await reader.readexactly(2)
        opcode = header[0] & 0x0F
        masked = bool(header[1] & 0x80)
        length = header[1] & 0x7F
        if length == 126:
            length = struct.unpack(">H", await reader.readexactly(2))[0]
        elif length == 127:
            length = struct.unpack(">Q", await reader.readexactly(8))[0]
        if length > MAX_FRAME_SIZE:
            logger.error("Frame too large: %d bytes", length)
            return None
        mask_key = await reader.readexactly(4) if masked else b""
        data     = await reader.readexactly(length)
        if masked:
            data = bytes(b ^ mask_key[i % 4] for i, b in enumerate(data))
        return opcode, data

    # ── Message handler ───────────────────────────────────────────────────────

    async def _handle_message(self, client: WsClient, raw: str) -> None:
        if not self._rate_limiter.is_allowed(client.client_id):
            await client.send_json({"event": "error", "payload": {"message": "Rate limit exceeded."}})
            return

        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            await client.send_json({"event": "error", "payload": {"message": "Invalid JSON"}})
            return

        action  = msg.get("action", "")
        symbols = [
            normalize_symbol(s)
            for s in msg.get("symbols", [])
            if isinstance(s, str)
        ]

        if action in ("subscribe", "unsubscribe") and not self._sub_limiter.is_allowed(
            client.client_id
        ):
            # 6.1 — Subscribe flood guard: subscribe/unsubscribe churn drives
            # scheduler work, so it gets a tighter budget than other messages.
            await client.send_json({
                "event": "error",
                "payload": {"message": "Subscribe rate limit exceeded."},
            })
            return

        if action == "subscribe":
            if not symbols:
                await client.send_json({"event": "error", "payload": {"message": "subscribe requires at least one symbol"}})
                return
            unsupported = sorted(set(symbols) - SUPPORTED_SYMBOLS)
            if unsupported:
                allowed = ", ".join(sorted(SUPPORTED_SYMBOLS))
                await client.send_json({
                    "event": "error",
                    "payload": {
                        "message": f"Unsupported symbol(s): {', '.join(unsupported)}. Allowed: {allowed}"
                    },
                })
                symbols = [s for s in symbols if s in SUPPORTED_SYMBOLS]
                if not symbols:
                    return
            if len(client.symbols) + len(symbols) > MAX_SYMBOLS_PER_CLIENT:
                await client.send_json({"event": "error", "payload": {"message": f"Max {MAX_SYMBOLS_PER_CLIENT} symbols"}})
                return
            client.symbols.update(symbols)
            self._scheduler.subscribe(client.client_id, symbols)
            if self._metrics:
                self._metrics.update_ws_client(client.client_id, list(client.symbols))
                self._metrics.record_ws_event(client.client_id, "subscribed", symbols)
            await client.send_json({
                "event": "subscribed",
                "payload": {"symbols": list(client.symbols), "newSymbols": symbols},
            })

        elif action == "unsubscribe":
            for sym in symbols:
                client.symbols.discard(sym)
            self._scheduler.remove_symbols(client.client_id, symbols)
            if self._metrics:
                self._metrics.update_ws_client(client.client_id, list(client.symbols))
            await client.send_json({"event": "unsubscribed", "payload": {"symbols": list(client.symbols)}})

        elif action == "subscribe_metrics":
            self._metrics_subs.add(client.client_id)
            if self._metrics:
                await client.send_json({"event": "metrics.snapshot", "payload": self._metrics.build_snapshot()})
            await client.send_json({"event": "metrics_subscribed", "payload": {"interval_ms": 5000}})

        elif action == "unsubscribe_metrics":
            self._metrics_subs.discard(client.client_id)
            await client.send_json({"event": "metrics_unsubscribed", "payload": {}})

        elif action == "ping":
            await client.send_json({"event": "pong", "payload": {}})

        elif action == "status":
            await client.send_json({"event": "status", "payload": {
                "connectedClients": len(self._clients),
                "schedules":        self._scheduler.get_status(),
                "yourSymbols":      list(client.symbols),
            }})

        elif action == "candles":
            symbol   = msg.get("symbol", "").upper()
            interval = msg.get("interval", "1h")
            limit    = min(int(msg.get("limit", 200)), 1000)
            req_id   = msg.get("reqId")
            if not symbol:
                await client.send_json({"event": "error", "payload": {"message": "candles requires symbol"}})
                return
            try:
                loop    = asyncio.get_running_loop()
                candles = await loop.run_in_executor(
                    None, lambda: self._md.fetch_candles(symbol, interval, limit)
                )
                await client.send_json({"event": "candles", "payload": {
                    "symbol":   symbol,
                    "interval": interval,
                    "reqId":    req_id,
                    "candles":  [
                        {"t": c.timestamp, "o": c.open, "h": c.high, "l": c.low, "c": c.close}
                        for c in candles
                    ],
                }})
            except Exception as exc:
                await client.send_json({"event": "error", "payload": {"message": f"candles fetch failed: {exc}"}})

        elif action == "inject":
            payload = msg.get("payload")
            if not payload or not isinstance(payload, dict):
                await client.send_json({"event": "error", "payload": {"message": "inject requires a payload object"}})
                return
            sym = payload.get("symbol")
            await self._broadcast_async(SignalEvent.SIGNAL_TRIGGERED, payload, sym)
            await client.send_json({"event": "injected", "payload": {"id": payload.get("id"), "symbol": sym}})

        elif action == "signal.query":
            request_id  = msg.get("requestId", "")
            signal_data = msg.get("signal")
            if not signal_data or not isinstance(signal_data, dict):
                await client.send_json({
                    "event":     "signal.query_result",
                    "requestId": request_id,
                    "error":     "missing signal payload",
                })
                return
            result = await self._service.query_signal_status(signal_data, request_id)
            await client.send_json({"event": "signal.query_result", **result})

        elif action == "zone.sync":
            request_id = msg.get("requestId", "")
            zones      = self._service.get_armed_zones()
            await client.send_json({
                "event":     "zone.sync_result",
                "requestId": request_id,
                "zones":     zones,
                "count":     len(zones),
            })

        else:
            await client.send_json({"event": "error", "payload": {"message": f"Unknown action: {action!r}"}})
