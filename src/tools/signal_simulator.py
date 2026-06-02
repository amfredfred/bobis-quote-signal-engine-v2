"""
Signal Simulator
================

Connects to the Signal Engine as a client and injects fake signals via the
  {"action": "inject", "payload": { ...signal }}
message. The Signal Engine then broadcasts the signal to all subscribed
clients (including the Execution Engine) through its normal path.

Flow:
    Signal Simulator  →  (inject)  →  Signal Engine  →  (broadcast)  →  Execution Engine

Signal structure matches TradeSignal.to_dict() exactly — uses the same
HtfRange / LtfRange / RejectionCandle geometry as the live engine so the
Execution Engine cannot distinguish injected signals from real ones.

Usage
-----
    python tools/signal_simulator.py                          # interactive
    python tools/signal_simulator.py --mode auto              # random, every 10s
    python tools/signal_simulator.py --mode auto --interval 5 --symbols EUR/USD,XAU/USD
    python tools/signal_simulator.py --engine-port 8765       # explicit engine port

Ports
-----
    --port         Port the simulator process is identified with (default: 8766)
    --engine-port  Port the Signal Engine WebSocket server listens on (default: 8765)

No external dependencies — pure stdlib asyncio RFC 6455 client.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import logging
import os
import random
import struct
import time
import uuid

logging.basicConfig(
    format="%(asctime)s  %(levelname)-8s  %(name)-20s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    level=logging.INFO,
)
logger = logging.getLogger("signal_simulator")

_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
_OP_TEXT = 0x1
_OP_CLOSE = 0x8
_OP_PING = 0x9
_OP_PONG = 0xA

# ── Per-symbol market data  (price_lo, price_hi, digits) ─────────────────────
_HTF_BAR_MS = 3_600_000  # 1 hour HTF bar in milliseconds

# ── Per-symbol market data  (price_lo, price_hi, digits) ─────────────────────
# Updated ~ March 2026 realistic levels (approximate spot ± small range)
_SYMBOLS: dict[str, tuple[float, float, int]] = {
    "XAUUSD": (2650.0, 2750.0, 2),
}
_ALL_SYMBOLS = list(_SYMBOLS.keys())


# ═════════════════════════════════════════════════════════════════════════════
# RFC 6455 client — pure stdlib
# ═════════════════════════════════════════════════════════════════════════════


class WsClient:
    def __init__(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        self._reader = reader
        self._writer = writer
        self._connected = True
        self._send_lock = asyncio.Lock()

    @classmethod
    async def connect(cls, host: str, port: int, path: str = "/") -> "WsClient":
        """
        Open a TCP connection to *host:port* and perform the WebSocket
        upgrade handshake.  *port* here is the Signal Engine's engine-port,
        NOT the simulator's own --port identifier.
        """
        reader, writer = await asyncio.open_connection(host, port)
        client = cls(reader, writer)
        await client._handshake(host, port, path)
        return client

    async def _handshake(self, host: str, port: int, path: str) -> None:
        nonce = base64.b64encode(os.urandom(16)).decode()
        accept_key = base64.b64encode(
            hashlib.sha1(f"{nonce}{_WS_GUID}".encode()).digest()
        ).decode()
        self._writer.write(
            (
                f"GET {path} HTTP/1.1\r\n"
                f"Host: {host}:{port}\r\n"
                f"Upgrade: websocket\r\nConnection: Upgrade\r\n"
                f"Sec-WebSocket-Key: {nonce}\r\n"
                f"Sec-WebSocket-Version: 13\r\n\r\n"
            ).encode()
        )
        await self._writer.drain()

        response = b""
        while b"\r\n\r\n" not in response:
            chunk = await asyncio.wait_for(self._reader.read(1024), timeout=10)
            if not chunk:
                raise ConnectionError("Server closed during handshake")
            response += chunk

        first_line = response.split(b"\r\n")[0].decode()
        if "101" not in first_line:
            raise ConnectionError(f"Upgrade failed: {first_line}")

        for line in response.split(b"\r\n"):
            if line.lower().startswith(b"sec-websocket-accept:"):
                if line.split(b":", 1)[1].strip().decode() != accept_key:
                    raise ConnectionError("Sec-WebSocket-Accept mismatch")

    async def send_json(self, data: dict) -> None:
        payload = json.dumps(data, default=str).encode()
        async with self._send_lock:
            self._writer.write(_encode_frame(_OP_TEXT, payload))
            await self._writer.drain()

    async def recv_json(self) -> dict | None:
        """Read one text frame. Handles ping/pong/close internally."""
        while True:
            opcode, data = await _read_frame(self._reader)
            if opcode == _OP_CLOSE:
                self._connected = False
                return None
            elif opcode == _OP_PING:
                async with self._send_lock:
                    self._writer.write(_encode_frame(_OP_PONG, data))
                    await self._writer.drain()
            elif opcode == _OP_TEXT:
                try:
                    return json.loads(data.decode())
                except json.JSONDecodeError:
                    return None

    async def close(self) -> None:
        if not self._connected:
            return
        self._connected = False
        try:
            async with self._send_lock:
                self._writer.write(_encode_frame(_OP_CLOSE, b""))
                await self._writer.drain()
        except Exception:
            pass
        self._writer.close()
        try:
            await self._writer.wait_closed()
        except Exception:
            pass


def _encode_frame(opcode: int, payload: bytes) -> bytes:
    """Client→server frame: masked as required by RFC 6455."""
    mask = os.urandom(4)
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    length = len(payload)
    header = bytearray([0x80 | opcode])
    if length <= 125:
        header.append(0x80 | length)
    elif length <= 65535:
        header.append(0x80 | 126)
        header.extend(struct.pack(">H", length))
    else:
        header.append(0x80 | 127)
        header.extend(struct.pack(">Q", length))
    header.extend(mask)
    return bytes(header) + masked


async def _read_frame(reader: asyncio.StreamReader) -> tuple[int, bytes]:
    """Server→client frame: unmasked."""
    h = await reader.readexactly(2)
    opcode = h[0] & 0x0F
    masked = bool(h[1] & 0x80)
    length = h[1] & 0x7F
    if length == 126:
        length = struct.unpack(">H", await reader.readexactly(2))[0]
    elif length == 127:
        length = struct.unpack(">Q", await reader.readexactly(8))[0]
    mask_key = await reader.readexactly(4) if masked else b""
    data = await reader.readexactly(length)
    if masked:
        data = bytes(b ^ mask_key[i % 4] for i, b in enumerate(data))
    return opcode, data


# ═════════════════════════════════════════════════════════════════════════════
# Inline domain types (mirrors types.py — no import needed in standalone tool)
# ═════════════════════════════════════════════════════════════════════════════

from enum import Enum
from dataclasses import dataclass
from typing import Optional


class SignalDirection(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"


class SignalStatus(str, Enum):
    TRIGGERED = "TRIGGERED"


class BosDirection(str, Enum):
    BULLISH = "BULLISH"
    BEARISH = "BEARISH"


class CandlePattern(str, Enum):
    HAMMER = "HAMMER"
    SHOOTING_STAR = "SHOOTING_STAR"


@dataclass
class HtfRange:
    range_high: float
    range_low: float
    bos_direction: BosDirection
    timestamp: int
    broken_at: int = 0
    tp_level: float = 0.0
    htf_candle_open: int = 0
    htf_candle_close: int = 0

    @property
    def midpoint(self) -> float:
        return (self.range_high + self.range_low) / 2

    @property
    def height(self) -> float:
        return self.range_high - self.range_low


@dataclass
class LtfRange:
    range_high: float
    range_low: float
    timestamp: int
    direction: SignalDirection

    @property
    def sl_level(self) -> float:
        return (
            self.range_high
            if self.direction == SignalDirection.SHORT
            else self.range_low
        )


@dataclass
class RejectionCandle:
    open: float
    high: float
    low: float
    close: float
    timestamp: int
    wick_ratio: float
    pattern: CandlePattern

    @property
    def wick_tip(self) -> float:
        return self.high if self.pattern == CandlePattern.SHOOTING_STAR else self.low


@dataclass
class TradeSignal:
    id: str
    symbol: str
    direction: SignalDirection
    status: SignalStatus
    entry_price: float
    stop_loss: float
    tp1: float
    tp2: float
    htf_range: HtfRange
    ltf_range: LtfRange
    rejection_candle: RejectionCandle
    risk_reward_ratio: float
    risk_pips: float
    created_at: int
    pending_at: Optional[int] = None
    triggered_at: Optional[int] = None
    tp1_hit_at: Optional[int] = None
    tp2_hit_at: Optional[int] = None
    sl_hit_at: Optional[int] = None
    invalidated_at: Optional[int] = None
    invalidation_logged_at: Optional[int] = None
    expired_at: Optional[int] = None
    closed_at: Optional[int] = None
    outcome: Optional[str] = None
    realized_rr: Optional[float] = None
    close_price: Optional[float] = None
    chart_path: Optional[str] = None
    chart_data: Optional[dict] = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "symbol": self.symbol,
            "direction": self.direction.value,
            "status": self.status.value,
            "entryPrice": self.entry_price,
            "stopLoss": self.stop_loss,
            "tp1": self.tp1,
            "tp2": self.tp2,
            "riskRewardRatio": round(self.risk_reward_ratio, 4),
            "riskPips": round(self.risk_pips, 6),
            "htfRange": {
                "rangeHigh": self.htf_range.range_high,
                "rangeLow": self.htf_range.range_low,
                "bosDirection": self.htf_range.bos_direction.value,
                "timestamp": self.htf_range.timestamp,
                "brokenAt": self.htf_range.broken_at,
                "tpLevel": self.htf_range.tp_level,
                "midpoint": self.htf_range.midpoint,
                "height": round(self.htf_range.height, 6),
                "htfCandleOpen": self.htf_range.htf_candle_open,
                "htfCandleClose": self.htf_range.htf_candle_close,
            },
            "ltfRange": {
                "rangeHigh": self.ltf_range.range_high,
                "rangeLow": self.ltf_range.range_low,
                "timestamp": self.ltf_range.timestamp,
                "direction": self.ltf_range.direction.value,
                "slLevel": self.ltf_range.sl_level,
            },
            "rejectionCandle": {
                "open": self.rejection_candle.open,
                "high": self.rejection_candle.high,
                "low": self.rejection_candle.low,
                "close": self.rejection_candle.close,
                "timestamp": self.rejection_candle.timestamp,
                "wickRatio": round(self.rejection_candle.wick_ratio, 4),
                "pattern": self.rejection_candle.pattern.value,
                "wickTip": self.rejection_candle.wick_tip,
            },
            "createdAt": self.created_at,
            "pendingAt": self.pending_at,
            "triggeredAt": self.triggered_at,
            "tp1HitAt": self.tp1_hit_at,
            "tp2HitAt": self.tp2_hit_at,
            "slHitAt": self.sl_hit_at,
            "invalidatedAt": self.invalidated_at,
            "invalidationLoggedAt": self.invalidation_logged_at,
            "expiredAt": self.expired_at,
            "closedAt": self.closed_at,
            "outcome": self.outcome,
            "realizedRR": self.realized_rr,
            "closePrice": self.close_price,
            "chartPath": self.chart_path,
            "chartData": self.chart_data,
        }


# ═════════════════════════════════════════════════════════════════════════════
# Signal factory — builds TradeSignal objects and serialises via to_dict()
# ═════════════════════════════════════════════════════════════════════════════


def _build_signal(symbol: str, direction: str, rr: float) -> dict:
    """
    Build a TradeSignal using the same domain types as the live engine,
    then serialise via to_dict() so the payload is byte-for-byte identical
    to what signal_service.py broadcasts.

    Geometry (mirrors types.py docstrings):

      HTF range  — wide swing candle box.
                   tp_level = BOS target swing (far from entry → avg 1:4 RR).
                   Do NOT use range_high/range_low as TP — those are zone
                   boundaries adjacent to entry (~1:1 RR).

      LTF range  — tighter box inside the HTF candle's wick extremes.
                   sl_level = range_high (SHORT) or range_low (LONG).

      Rejection  — candle that tapped LTF range and closed back outside.
                   HAMMER        (LONG):  lower wick into zone, close > ltf_high.
                   SHOOTING_STAR (SHORT): upper wick into zone, close < ltf_low.
                   wick_tip      = computed by RejectionCandle.wick_tip property.

      SL         — beyond the LTF swing (swing placement method).
      TP1        — 50 % of the way to TP2  (tp1_multiplier = 0.5).
      TP2        — htf_range.tp_level  (BOS target swing).
    """
    lo, hi, digits = _SYMBOLS[symbol]
    dir_enum = SignalDirection(direction)

    # ── Core price distances ──────────────────────────────────────────────────
    entry = round(random.uniform(lo, hi), digits)
    sl_dist = round(entry * random.uniform(0.002, 0.005), digits)
    tp2_dist = round(sl_dist * rr, digits)
    tp1_dist = round(tp2_dist * 0.5, digits)  # tp1_multiplier = 0.5
    htf_half = round(sl_dist * random.uniform(1.2, 2.0), digits)

    # Rejection candle geometry
    wick_ratio = round(random.uniform(0.60, 0.85), 4)
    body_size = round(sl_dist * 0.15, digits)
    wick_reach = round(sl_dist * 0.60, digits)  # wick depth into zone

    if dir_enum == SignalDirection.LONG:
        sl = round(entry - sl_dist, digits)
        tp1 = round(entry + tp1_dist, digits)
        tp2 = round(entry + tp2_dist, digits)

        # HTF: entry sits near top (bullish re-test from below)
        htf_high = round(entry + htf_half, digits)
        htf_low = round(entry - htf_half * 1.5, digits)

        # LTF: tight box just below entry; sl_level = range_low
        ltf_high = round(entry + sl_dist * 0.3, digits)
        ltf_low = round(entry - sl_dist * 0.5, digits)

        # HAMMER: lower wick dips into zone, close = entry (above ltf_high)
        rej = RejectionCandle(
            open=round(entry - body_size, digits),
            high=round(entry + body_size * 0.5, digits),
            low=round(ltf_low - wick_reach, digits),  # wick tip
            close=entry,
            timestamp=int(time.time() * 1000) - 300_000,
            wick_ratio=wick_ratio,
            pattern=CandlePattern.HAMMER,
        )
        bos_dir = BosDirection.BULLISH

    else:  # SHORT
        sl = round(entry + sl_dist, digits)
        tp1 = round(entry - tp1_dist, digits)
        tp2 = round(entry - tp2_dist, digits)

        # HTF: entry sits near bottom (bearish re-test from above)
        htf_high = round(entry + htf_half * 1.5, digits)
        htf_low = round(entry - htf_half, digits)

        # LTF: tight box just above entry; sl_level = range_high
        ltf_high = round(entry + sl_dist * 0.5, digits)
        ltf_low = round(entry - sl_dist * 0.3, digits)

        # SHOOTING STAR: upper wick spikes into zone, close = entry (below ltf_low)
        rej = RejectionCandle(
            open=round(entry + body_size, digits),
            high=round(ltf_high + wick_reach, digits),  # wick tip
            low=round(entry - body_size * 0.5, digits),
            close=entry,
            timestamp=int(time.time() * 1000) - 300_000,
            wick_ratio=wick_ratio,
            pattern=CandlePattern.SHOOTING_STAR,
        )
        bos_dir = BosDirection.BEARISH

    # ── Timestamps ────────────────────────────────────────────────────────────
    now_ms = int(time.time() * 1000)
    htf_candle_open = now_ms - _HTF_BAR_MS * 2
    htf_candle_close = now_ms - _HTF_BAR_MS
    ltf_ts = htf_candle_open + (_HTF_BAR_MS // 4)
    created_at = rej.timestamp
    triggered_at = now_ms

    # ── Domain objects ────────────────────────────────────────────────────────
    htf = HtfRange(
        range_high=htf_high,
        range_low=htf_low,
        bos_direction=bos_dir,
        timestamp=htf_candle_open,
        broken_at=htf_candle_close,
        tp_level=tp2,  # BOS target = the far TP
        htf_candle_open=htf_candle_open,
        htf_candle_close=htf_candle_close,
    )
    ltf = LtfRange(
        range_high=ltf_high,
        range_low=ltf_low,
        timestamp=ltf_ts,
        direction=dir_enum,
    )

    actual_rr = round(abs(tp2 - entry) / sl_dist, 4) if sl_dist > 0 else rr
    _normalized = symbol.replace("/", "")
    signal = TradeSignal(
        id=f"SIM_{_normalized}_{triggered_at}_{direction}",
        symbol=_normalized,
        direction=dir_enum,
        status=SignalStatus.TRIGGERED,
        entry_price=entry,
        stop_loss=sl,
        tp1=tp1,
        tp2=tp2,
        htf_range=htf,
        ltf_range=ltf,
        rejection_candle=rej,
        risk_reward_ratio=actual_rr,
        risk_pips=round(sl_dist, digits + 1),
        created_at=created_at,
        pending_at=created_at - 60_000,
        triggered_at=triggered_at,
    )

    return signal.to_dict()


# ═════════════════════════════════════════════════════════════════════════════
# Simulator logic
# ═════════════════════════════════════════════════════════════════════════════


async def _wait_for_ack(ws: WsClient, timeout: float = 5.0) -> bool:
    """
    Wait for the server's 'subscribed' ack before firing the first signal.
    Returns True if ack received, False if timed out.
    """
    try:
        msg = await asyncio.wait_for(ws.recv_json(), timeout=timeout)
        if msg and msg.get("event") == "subscribed":
            logger.info("Subscribed  symbols=%s", msg.get("payload", {}).get("symbols"))
            return True
        # Might be a 'connected' welcome message first — try one more
        msg = await asyncio.wait_for(ws.recv_json(), timeout=timeout)
        if msg and msg.get("event") == "subscribed":
            logger.info("Subscribed  symbols=%s", msg.get("payload", {}).get("symbols"))
            return True
    except asyncio.TimeoutError:
        logger.warning("Timed out waiting for subscribe ack — proceeding anyway")
    return False


async def _inject(ws: WsClient, symbol: str, direction: str, rr: float) -> None:
    """Send one signal via the inject action."""
    payload = _build_signal(symbol, direction, rr)
    await ws.send_json({"action": "inject", "payload": payload})
    logger.info(
        "▶ inject  %-12s  %-5s  entry=%-12s  sl=%-12s  tp1=%-12s  tp2=%-12s  rr=%.2f",
        payload["symbol"],
        payload["direction"],
        payload["entryPrice"],
        payload["stopLoss"],
        payload["tp1"],
        payload["tp2"],
        payload["riskRewardRatio"],
    )


async def _recv_loop(ws: WsClient) -> None:
    """Background task — logs server responses."""
    while ws._connected:
        try:
            msg = await asyncio.wait_for(ws.recv_json(), timeout=60)
            if msg is None:
                break
            event = msg.get("event", "")
            if event == "injected":
                logger.info(
                    "Server confirmed inject  id=%s",
                    msg.get("payload", {}).get("id", "?"),
                )
            elif event == "error":
                logger.warning(
                    "Server error: %s", msg.get("payload", {}).get("message")
                )
            elif event in ("pong", "connected"):
                pass
            else:
                logger.debug("Server: %s", msg)
        except asyncio.TimeoutError:
            continue
        except Exception:
            break


# ── Interactive ───────────────────────────────────────────────────────────────


async def _interactive(ws: WsClient, symbols: list[str]) -> None:
    loop = asyncio.get_event_loop()
    await ws.send_json({"action": "subscribe", "symbols": symbols})
    await _wait_for_ack(ws)

    print("\n" + "═" * 64)
    print("  Signal Simulator  —  interactive mode")
    print("  Signals injected → Signal Engine → Execution Engine")
    print("  Signal structure matches TradeSignal.to_dict() exactly")
    print("═" * 64)

    while ws._connected:
        _print_menu()
        try:
            raw = await loop.run_in_executor(None, input, "  > ")
        except (EOFError, KeyboardInterrupt):
            break
        raw = raw.strip()
        if not raw or raw.lower() in ("q", "quit"):
            break
        result = _parse_input(raw)
        if result:
            await _inject(ws, *result)


# ── Auto ──────────────────────────────────────────────────────────────────────


async def _auto(ws: WsClient, symbols: list[str], interval: float) -> None:
    await ws.send_json({"action": "subscribe", "symbols": symbols})
    await _wait_for_ack(ws)
    logger.info("Auto mode — firing every %.1fs", interval)
    while ws._connected:
        await _inject(
            ws,
            random.choice(symbols),
            random.choice(["LONG", "SHORT"]),
            round(random.uniform(1.5, 5.0), 1),
        )
        await asyncio.sleep(interval)


# ── Helpers ───────────────────────────────────────────────────────────────────


def _print_menu() -> None:
    print("\n  Symbols:")
    items = list(enumerate(_ALL_SYMBOLS, 1))
    for i in range(0, len(items), 3):
        print("  " + "   ".join(f"{n:>2}. {s:<12}" for n, s in items[i : i + 3]))
    print("\n  Input: <number|symbol> <L|S> [rr]   e.g.  1 L  /  xau/usd S 3  /  q")


def _parse_input(raw: str) -> tuple[str, str, float] | None:
    parts = raw.split()
    try:
        sym = parts[0]
        if sym.isdigit():
            idx = int(sym) - 1
            if not (0 <= idx < len(_ALL_SYMBOLS)):
                print(f"  ✗ Number must be 1–{len(_ALL_SYMBOLS)}")
                return None
            symbol = _ALL_SYMBOLS[idx]
        else:
            symbol = sym.upper().replace("-", "/")
            if symbol not in _SYMBOLS:
                print(f"  ✗ Unknown symbol: {symbol}")
                return None
        direction = (
            "LONG" if (len(parts) < 2 or parts[1].upper().startswith("L")) else "SHORT"
        )
        rr = float(parts[2]) if len(parts) > 2 else 2.0
        if rr <= 0:
            print("  ✗ R:R must be > 0")
            return None
        return symbol, direction, rr
    except (IndexError, ValueError) as exc:
        print(f"  ✗ Parse error: {exc}")
        return None


# ═════════════════════════════════════════════════════════════════════════════
# Entry point
# ═════════════════════════════════════════════════════════════════════════════


async def _main(args: argparse.Namespace) -> None:
    symbols = (
        [s.strip() for s in args.symbols.split(",")] if args.symbols else _ALL_SYMBOLS
    )

    logger.info(
        "Simulator port : %d  (identifier only — this process does not listen)",
        args.port,
    )
    logger.info(
        "Connecting to Signal Engine at ws://%s:%d ...",
        args.host,
        args.engine_port,
    )

    try:
        ws = await WsClient.connect(args.host, args.engine_port)
    except (ConnectionRefusedError, OSError) as exc:
        logger.error(
            "Cannot connect to Signal Engine on port %d: %s", args.engine_port, exc
        )
        return

    logger.info("Connected to Signal Engine on port %d", args.engine_port)
    recv_task = asyncio.create_task(_recv_loop(ws))

    try:
        if args.mode == "auto":
            await _auto(ws, symbols, args.interval)
        else:
            await _interactive(ws, symbols)
    except KeyboardInterrupt:
        pass
    finally:
        recv_task.cancel()
        await ws.close()
        logger.info("Disconnected")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Signal Engine simulator",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Ports:
  --port         Simulator's own port identifier (default: 8766, not a listener)
  --engine-port  Signal Engine WebSocket port to connect to (default: 8765)

Examples:
  python tools/signal_simulator.py
  python tools/signal_simulator.py --mode auto --interval 5
  python tools/signal_simulator.py --engine-port 8765 --port 8766
  python tools/signal_simulator.py --mode auto --symbols EUR/USD,XAU/USD --interval 10
        """,
    )
    parser.add_argument(
        "--host", default="localhost", help="Signal Engine host (default: localhost)"
    )
    parser.add_argument(
        "--port",
        default=8766,
        type=int,
        help="Simulator port identifier (default: 8766)",
    )
    parser.add_argument(
        "--engine-port",
        default=8765,
        type=int,
        help="Signal Engine WebSocket port (default: 8765)",
    )
    parser.add_argument(
        "--mode", default="interactive", choices=["interactive", "auto"]
    )
    parser.add_argument("--interval", default=10.0, type=float)
    parser.add_argument("--symbols", default=None)
    args = parser.parse_args()

    try:
        asyncio.run(_main(args))
    except KeyboardInterrupt:
        print("\nSimulator stopped.")
