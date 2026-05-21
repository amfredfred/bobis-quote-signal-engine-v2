"""
infrastructure/data_providers/market_data.py - direct MetaTrader 5 data client.

The engine reads OHLC candles from the local MetaTrader 5 terminal through the
official Python package. MT5 Python returns bar timestamps in UTC, so the engine
keeps them as UTC milliseconds.
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


def _parse_rates(rates) -> list[Candle]:
    if rates is None:
        raise MarketDataError(f"MT5 returned no rates ({_last_error()})")

    candles: list[Candle] = []
    for row in rates:
        # MT5 Python rate timestamps are UTC epoch seconds. Broker chart/server
        # time is a terminal display concern and must not be subtracted here.
        timestamp = int(_row_value(row, "time")) * 1000
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
    ) -> None:
        if mt5 is None:
            raise MarketDataError(
                "MetaTrader5 package is not installed. Install project dependencies "
                "with `pip install -e .` on the MT5 host."
            )

        self._metrics_fn = metrics_fn
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
        )

    def _normalize_symbol(self, symbol: str) -> str:
        return symbol.upper().replace("/", "")

    def _ensure_symbol(self, symbol: str) -> str:
        mt5_symbol = self._normalize_symbol(symbol)
        info = mt5.symbol_info(mt5_symbol)
        if info is None:
            raise MarketDataError(f"MT5 symbol {mt5_symbol!r} not found")
        if not info.visible and not mt5.symbol_select(mt5_symbol, True):
            raise MarketDataError(
                f"MT5 symbol {mt5_symbol!r} is not visible and could not be selected"
            )
        return mt5_symbol

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
        called_at = _now_ms()
        started = time.perf_counter()
        try:
            rates = mt5.copy_rates_from_pos(mt5_symbol, mt5_interval, 0, outputsize)
            candles = _parse_rates(rates)
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
        end_ts = end_ts or _now_ms()
        called_at = _now_ms()
        started = time.perf_counter()
        try:
            rates = mt5.copy_rates_range(
                mt5_symbol,
                mt5_interval,
                _utc_dt(start_ts),
                _utc_dt(end_ts),
            )
            candles = _parse_rates(rates)
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
