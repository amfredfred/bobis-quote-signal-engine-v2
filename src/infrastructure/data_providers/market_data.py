"""Direct MetaTrader 5 data client.

The engine keeps internal timestamps as real UTC epoch milliseconds. Some MT5
brokers expose rate/tick timestamps in broker/server time encoded as epoch
seconds, so this adapter calibrates that offset from a live tick and normalizes
broker timestamps before returning candles or scheduler time.
"""

from __future__ import annotations

import datetime
import logging
import time
from typing import Optional

from domain.entities.candle import Candle

logger = logging.getLogger(__name__)


class MarketDataError(Exception):
    pass


try:
    import MetaTrader5 as mt5
except ImportError:  # pragma: no cover - exercised only on missing runtime dep
    mt5 = None


_INTERVAL_MAP = {}
if mt5 is not None:
    _INTERVAL_MAP = {
        "1min": mt5.TIMEFRAME_M1,
        "2min": mt5.TIMEFRAME_M2,
        "3min": mt5.TIMEFRAME_M3,
        "4min": mt5.TIMEFRAME_M4,
        "5min": mt5.TIMEFRAME_M5,
        "6min": mt5.TIMEFRAME_M6,
        "10min": mt5.TIMEFRAME_M10,
        "12min": mt5.TIMEFRAME_M12,
        "15min": mt5.TIMEFRAME_M15,
        "20min": mt5.TIMEFRAME_M20,
        "30min": mt5.TIMEFRAME_M30,
        "1h": mt5.TIMEFRAME_H1,
        "2h": mt5.TIMEFRAME_H2,
        "3h": mt5.TIMEFRAME_H3,
        "4h": mt5.TIMEFRAME_H4,
        "6h": mt5.TIMEFRAME_H6,
        "8h": mt5.TIMEFRAME_H8,
        "12h": mt5.TIMEFRAME_H12,
        "1day": mt5.TIMEFRAME_D1,
        "1week": mt5.TIMEFRAME_W1,
        "1month": mt5.TIMEFRAME_MN1,
    }


def _to_mt5_interval(interval: str) -> int:
    mapped = _INTERVAL_MAP.get(interval)
    if mapped is None:
        raise MarketDataError(
            f"Cannot map interval {interval!r} to MT5 format. "
            f"Supported: {list(_INTERVAL_MAP)}"
        )
    return mapped


def _utc_dt(ms: int) -> datetime.datetime:
    return datetime.datetime.fromtimestamp(ms / 1000, tz=datetime.timezone.utc)


def _now_ms() -> int:
    return int(datetime.datetime.now(tz=datetime.timezone.utc).timestamp() * 1000)


def _last_error() -> str:
    if mt5 is None:
        return "MetaTrader5 package is not installed"
    code, message = mt5.last_error()
    return f"{code}: {message}"


def _row_value(row, key: str, default=0):
    try:
        return row[key]
    except Exception:
        return getattr(row, key, default)


def _parse_rates(rates, broker_time_offset_ms: int = 0) -> list[Candle]:
    if rates is None:
        raise MarketDataError(f"MT5 returned no rates ({_last_error()})")

    candles: list[Candle] = []
    for row in rates:
        timestamp = int(_row_value(row, "time")) * 1000 - broker_time_offset_ms
        candles.append(
            Candle(
                timestamp=timestamp,
                open=float(_row_value(row, "open")),
                high=float(_row_value(row, "high")),
                low=float(_row_value(row, "low")),
                close=float(_row_value(row, "close")),
                volume=float(
                    _row_value(row, "tick_volume", _row_value(row, "real_volume", 0))
                ),
            )
        )
    candles.sort(key=lambda c: c.timestamp)
    return candles


class MarketDataClient:
    """
    Synchronous client for the local MetaTrader 5 terminal.

    The public methods intentionally match the old data client so the signal
    service and backtester can keep using `fetch_candles`,
    `fetch_candles_range`, and `close`.
    """

    def __init__(
        self,
        *,
        terminal_path: str = "",
        login: Optional[int] = None,
        password: str = "",
        server: str = "",
        timeout_ms: int = 60_000,
        portable: bool = False,
        metrics_fn=None,
        settings=None,
    ) -> None:
        if mt5 is None:
            raise MarketDataError(
                "MetaTrader5 package is not installed. Install project dependencies "
                "with `pip install -e .` on the MT5 host."
            )

        self._metrics_fn = metrics_fn
        self._settings = settings
        self._broker_time_offset_ms_by_symbol: dict[str, int] = {}
        self._resolved_symbols: dict[str, str] = {}
        self._shutdown_on_close = False

        kwargs = {"timeout": timeout_ms, "portable": portable}
        if terminal_path:
            kwargs["path"] = terminal_path
        if login is not None:
            kwargs["login"] = login
        if password:
            kwargs["password"] = password
        if server:
            kwargs["server"] = server

        if not mt5.initialize(**kwargs):
            raise MarketDataError(f"MT5 initialize failed: {_last_error()}")

        self._shutdown_on_close = True
        account = mt5.account_info()
        terminal = mt5.terminal_info()
        logger.info(
            "MarketDataClient initialised via MT5  login=%s  terminal=%s",
            getattr(account, "login", None),
            getattr(terminal, "path", None),
        )

    @classmethod
    def from_settings(cls, settings, metrics_fn=None) -> "MarketDataClient":
        login = settings.mt5_login
        return cls(
            terminal_path=settings.mt5_terminal_path,
            login=login if login else None,
            password=settings.mt5_password,
            server=settings.mt5_server,
            timeout_ms=settings.mt5_timeout_ms,
            portable=settings.mt5_portable,
            metrics_fn=metrics_fn,
            settings=settings,
        )

    def _normalize_symbol(self, symbol: str) -> str:
        return "".join(ch for ch in symbol.upper() if ch.isalnum())

    def _ensure_symbol(self, symbol: str) -> str:
        clean = self._normalize_symbol(symbol)
        cached = self._resolved_symbols.get(clean)
        if cached:
            return cached

        info = mt5.symbol_info(clean)
        if info is not None:
            mt5_symbol = getattr(info, "name", clean)
        else:
            symbols = mt5.symbols_get()
            if not symbols:
                raise MarketDataError(
                    f"MT5 symbol {clean!r} not found; symbols_get returned no symbols "
                    f"({_last_error()})"
                )

            matches = [
                str(item.name)
                for item in symbols
                if self._normalize_symbol(str(item.name)).startswith(clean)
                or self._normalize_symbol(str(item.name)).endswith(clean)
            ]
            if not matches:
                related = [
                    str(item.name)
                    for item in symbols
                    if clean[:3] and clean[:3] in self._normalize_symbol(str(item.name))
                ][:10]
                hint = f"; related broker symbols: {related}" if related else ""
                raise MarketDataError(f"MT5 symbol {clean!r} not found{hint}")

            mt5_symbol = min(matches, key=lambda name: (len(name), name.upper()))
            logger.warning(
                "MT5 symbol %r resolved to broker symbol %r%s",
                symbol,
                mt5_symbol,
                f" from {matches}" if len(matches) > 1 else "",
            )
            info = mt5.symbol_info(mt5_symbol)

        if info is None:
            raise MarketDataError(f"MT5 symbol {mt5_symbol!r} could not be inspected")
        if not info.visible and not mt5.symbol_select(mt5_symbol, True):
            raise MarketDataError(
                f"MT5 symbol {mt5_symbol!r} is not visible and could not be selected"
            )
        self._resolved_symbols[clean] = mt5_symbol
        return mt5_symbol

    def _broker_time_offset_ms(self, symbol: str) -> int:
        mt5_symbol = self._ensure_symbol(symbol)
        tick = mt5.symbol_info_tick(mt5_symbol)
        if tick is not None:
            if getattr(tick, "time_msc", 0):
                tick_ms = int(tick.time_msc)
            elif getattr(tick, "time", 0):
                tick_ms = int(tick.time) * 1000
            else:
                tick_ms = 0
            if tick_ms:
                raw_offset = tick_ms - _now_ms()
                offset = round(raw_offset / 3_600_000) * 3_600_000
                self._broker_time_offset_ms_by_symbol[mt5_symbol] = offset
                if self._settings is not None:
                    object.__setattr__(self._settings, "broker_time_offset_ms", offset)
                return offset

        cached = self._broker_time_offset_ms_by_symbol.get(mt5_symbol)
        if cached is not None:
            return cached

        logger.warning("[%s] MT5 broker clock unavailable; assuming UTC offset 0", symbol)
        return 0

    def now_ms(self, symbol: str) -> int:
        """Current real UTC timestamp derived from MT5 tick time for symbol."""
        mt5_symbol = self._ensure_symbol(symbol)
        tick = mt5.symbol_info_tick(mt5_symbol)
        if tick is not None:
            if getattr(tick, "time_msc", 0):
                return int(tick.time_msc) - self._broker_time_offset_ms(symbol)
            if getattr(tick, "time", 0):
                return int(tick.time) * 1000 - self._broker_time_offset_ms(symbol)

        logger.warning("[%s] MT5 tick clock unavailable; falling back to system UTC", symbol)
        return _now_ms()

    def _record_metrics(
        self,
        symbol: str,
        interval: str,
        called_at: int,
        started: float,
        success: bool,
        error: Optional[str] = None,
    ) -> None:
        if not self._metrics_fn:
            return
        duration_ms = (time.perf_counter() - started) * 1000
        self._metrics_fn(symbol, interval, "mt5", called_at, duration_ms, success, error)

    def fetch_candles(
        self,
        symbol: str,
        interval: str,
        outputsize: int = 200,
        allow_gaps: bool = True,
    ) -> list[Candle]:
        mt5_interval = _to_mt5_interval(interval)
        mt5_symbol = self._ensure_symbol(symbol)
        broker_time_offset_ms = self._broker_time_offset_ms(symbol)
        called_at = _now_ms()
        started = time.perf_counter()
        try:
            rates = mt5.copy_rates_from_pos(mt5_symbol, mt5_interval, 0, outputsize)
            candles = _parse_rates(rates, broker_time_offset_ms)
            self._record_metrics(symbol, interval, called_at, started, True)
            logger.debug("[%s %s] fetch_candles: %d candles", symbol, interval, len(candles))
            return candles
        except Exception as exc:
            self._record_metrics(symbol, interval, called_at, started, False, str(exc))
            raise

    def fetch_candles_range(
        self,
        symbol: str,
        interval: str,
        start_ts: int,
        end_ts: Optional[int] = None,
        allow_gaps: bool = True,
    ) -> list[Candle]:
        mt5_interval = _to_mt5_interval(interval)
        mt5_symbol = self._ensure_symbol(symbol)
        broker_time_offset_ms = self._broker_time_offset_ms(symbol)
        end_ts = end_ts or _now_ms()
        called_at = _now_ms()
        started = time.perf_counter()
        try:
            rates = mt5.copy_rates_range(
                mt5_symbol,
                mt5_interval,
                _utc_dt(start_ts + broker_time_offset_ms),
                _utc_dt(end_ts + broker_time_offset_ms),
            )
            candles = _parse_rates(rates, broker_time_offset_ms)
            self._record_metrics(symbol, interval, called_at, started, True)
            logger.debug(
                "[%s %s] fetch_candles_range: %d candles",
                symbol,
                interval,
                len(candles),
            )
            return candles
        except Exception as exc:
            self._record_metrics(symbol, interval, called_at, started, False, str(exc))
            raise

    def close(self) -> None:
        if self._shutdown_on_close and mt5 is not None:
            mt5.shutdown()
            self._shutdown_on_close = False
