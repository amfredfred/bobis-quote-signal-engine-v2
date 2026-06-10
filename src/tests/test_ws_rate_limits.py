"""6.1 — WS abuse protection: subscribe flood guard + per-IP connect limiter."""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from interfaces.ws.server import (
    MAX_CONNECTS_PER_MINUTE_PER_IP,
    MAX_SUBSCRIBES_PER_MINUTE,
    WebSocketServer,
    _RateLimiter,
)


def test_rate_limiter_blocks_after_budget_exhausted() -> None:
    limiter = _RateLimiter(max_per_minute=3)

    assert all(limiter.is_allowed("ip-1") for _ in range(3))
    assert not limiter.is_allowed("ip-1")
    # A different key has its own budget.
    assert limiter.is_allowed("ip-2")


def test_rate_limiter_prune_stale_drops_idle_keys() -> None:
    limiter = _RateLimiter(max_per_minute=3)
    limiter.is_allowed("ip-old")
    limiter.is_allowed("ip-new")
    # Age out ip-old's only call.
    limiter._calls["ip-old"] = [0.0]

    limiter.prune_stale()

    assert "ip-old" not in limiter._calls
    assert "ip-new" in limiter._calls


class _StubScheduler:
    def __init__(self) -> None:
        self.subscribed: list[tuple[str, list[str]]] = []

    def subscribe(self, client_id: str, symbols: list[str]) -> None:
        self.subscribed.append((client_id, symbols))

    def remove_symbols(self, client_id: str, symbols: list[str]) -> None:
        pass

    def unsubscribe(self, client_id: str) -> None:
        pass


class _StubClient:
    def __init__(self) -> None:
        self.client_id = "client-1"
        self.symbols: set[str] = set()
        self.connected = True
        self.sent: list[dict] = []

    async def send_json(self, data: dict) -> bool:
        self.sent.append(data)
        return True


def _server() -> WebSocketServer:
    server = object.__new__(WebSocketServer)
    server._scheduler = _StubScheduler()
    server._service = None
    server._md = None
    server._cfg = None
    server._metrics = None
    server._clients = {}
    server._metrics_subs = set()
    server._rate_limiter = _RateLimiter(max_per_minute=1000)
    server._sub_limiter = _RateLimiter(MAX_SUBSCRIBES_PER_MINUTE)
    server._conn_limiter = _RateLimiter(MAX_CONNECTS_PER_MINUTE_PER_IP)
    return server


def test_subscribe_flood_guard_blocks_excess_subscribes() -> None:
    server = _server()
    client = _StubClient()

    async def flood() -> None:
        for _ in range(MAX_SUBSCRIBES_PER_MINUTE + 3):
            await server._handle_message(
                client, '{"action": "subscribe", "symbols": ["XAUUSD"]}'
            )

    asyncio.run(flood())

    errors = [m for m in client.sent if m.get("event") == "error"]
    subscribed = [m for m in client.sent if m.get("event") == "subscribed"]
    assert len(subscribed) == MAX_SUBSCRIBES_PER_MINUTE
    assert len(errors) == 3
    assert all("rate limit" in e["payload"]["message"].lower() for e in errors)


def test_subscribe_flood_guard_does_not_throttle_pings() -> None:
    server = _server()
    client = _StubClient()

    async def chatter() -> None:
        # Exhaust the subscribe budget...
        for _ in range(MAX_SUBSCRIBES_PER_MINUTE):
            await server._handle_message(
                client, '{"action": "subscribe", "symbols": ["XAUUSD"]}'
            )
        # ...pings must still flow (only the general limiter applies to them).
        await server._handle_message(client, '{"action": "ping"}')

    asyncio.run(chatter())

    assert any(m.get("event") == "pong" for m in client.sent)
