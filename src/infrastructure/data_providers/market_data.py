"""
infrastructure/data_providers/market_data.py — MT5 HTTP bridge client.

All candle data is fetched from the local MT5 bridge server.

Fixes vs old market_data.py
────────────────────────────
  BUG: string-format timestamps were tagged with session_tz instead of UTC.
       The MT5 server always returns UTC. Fixed: datetime strings are now
       always parsed as UTC regardless of session_tz.

  BUG: client expected a flat list from the server but the server wraps
       successful responses in {"status": "success", "count": N, "candles": [...]}.
       Fixed: _unwrap_raw() normalises both success and error envelopes before
       passing data to _parse_candles().

  IMPROVEMENT: MarketDataClient is now a plain class — no module-level _cfg.
               Inject base_url and settings at construction time.

API contract (POST /time-series)
──────────────────────────────────
  Request:  { "symbols": [...], "timeframes": [...], "limit": N }
            { "symbols": [...], "timeframes": [...], "from_date": "...", "to_date": "..." }
  Response: { "EUR/USD": { "1h": { "status": "success", "count": N, "candles": [...] } } }
         or { "EUR/USD": { "1h": { "status": "error",   "error": "..." } } }

Interval map (config format → MT5 format):
  "1min"→"1m"  "5min"→"5m" "10min"→"10m"  "15min"→"15m"  "30min"→"30m"
  "1h"→"1h"    "4h"→"4h"    "1day"→"d1"     "1week"→"w1"
"""

from __future__ import annotations

import datetime
import logging
import time
from typing import Optional

import requests
from requests.adapters import HTTPAdapter

from domain.entities.candle import Candle

logger = logging.getLogger(__name__)

_INTERVAL_MAP: dict[str, str] = {
    "1min": "1m",
    "5min": "5m",
    "6min": "6m",
    "10min": "10m",
    "15min": "15m",
    "30min": "30m",
    "1h": "1h",
    "4h": "4h",
    "1day": "d1",
    "1week": "w1",
    "1month": "mn1",
}


class MarketDataError(Exception):
    pass


def _to_mt5_interval(interval: str) -> str:
    mapped = _INTERVAL_MAP.get(interval)
    if mapped is None:
        raise MarketDataError(
            f"Cannot map interval {interval!r} to MT5 format. "
            f"Supported: {list(_INTERVAL_MAP)}"
        )
    return mapped


def _ms_to_utc_str(ms: int) -> str:
    """UTC ms timestamp → 'YYYY-MM-DD HH:MM:SS' in UTC."""
    return datetime.datetime.fromtimestamp(
        ms / 1000, tz=datetime.timezone.utc
    ).strftime("%Y-%m-%d %H:%M:%S")


def _unwrap_raw(raw, symbol: str, mt5_interval: str) -> list[dict]:
    """
    Normalise the per-timeframe value returned by the bridge.

    The server wraps results in an envelope:
        success → {"status": "success", "count": N, "candles": [...]}
        error   → {"status": "error",   "error": "..."}

    A bare list is also accepted for forward-compatibility.
    Raises MarketDataError on any error payload.
    """
    if isinstance(raw, list):
        return raw

    if isinstance(raw, dict):
        if raw.get("status") == "error" or "error" in raw:
            raise MarketDataError(
                f"MT5 server error for {symbol}/{mt5_interval}: {raw.get('error')}"
            )
        candles = raw.get("candles")
        if candles is None:
            raise MarketDataError(
                f"Unexpected response shape for {symbol}/{mt5_interval}: {raw}"
            )
        return candles

    raise MarketDataError(
        f"Unrecognised response type for {symbol}/{mt5_interval}: {type(raw)}"
    )


def _parse_candles(values: list[dict]) -> list[Candle]:
    """
    Parse raw candle dicts from the MT5 server into Candle objects.

    MT5 always returns UTC timestamps. String-format datetimes are parsed
    as UTC — never as session_tz (that was the old timezone bug).
    """
    candles: list[Candle] = []
    for v in values:
        try:
            raw = v.get("timestamp") or v.get("datetime")
            if raw is None:
                raise KeyError("no 'timestamp' or 'datetime' field")

            if isinstance(raw, int):
                ts_ms = raw
            else:
                # FIX: always UTC — MT5 server contract guarantees UTC strings
                ts_ms = int(
                    datetime.datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
                    .replace(tzinfo=datetime.timezone.utc)
                    .timestamp()
                    * 1000
                )

            candles.append(
                Candle(
                    timestamp=ts_ms,
                    open=float(v["open"]),
                    high=float(v["high"]),
                    low=float(v["low"]),
                    close=float(v["close"]),
                    volume=float(v.get("volume") or 0),
                )
            )
        except (KeyError, ValueError) as exc:
            logger.warning("Skipping malformed candle: %s -- %s", v, exc)
    return candles


class MarketDataClient:
    """
    HTTP client for the local MT5 bridge server.

    All methods are synchronous — wrap in loop.run_in_executor() from async code.

    Parameters
    ──────────
    base_url    — MT5 bridge URL, e.g. "http://localhost:8000"
    metrics_fn  — optional callable(symbol, interval, source, called_at, duration_ms, success, error)
                  injected so this class doesn't import the metrics singleton.
    """

    _RETRY_ATTEMPTS = 8
    _RETRY_BACKOFF = [1.0, 2.0, 4.0, 8.0, 12.0, 20.0]

    def __init__(
        self,
        base_url: str = "http://localhost:8000",
        metrics_fn=None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._metrics_fn = metrics_fn
        self._session = requests.Session()
        adapter = HTTPAdapter(pool_connections=20, pool_maxsize=20)
        self._session.mount("http://", adapter)
        self._session.mount("https://", adapter)
        logger.info("MarketDataClient initialised  base_url=%r", self._base_url)

    def _post(self, path: str, body: dict) -> dict:
        url = f"{self._base_url}{path}"
        symbol = str(body.get("symbols", ["?"])[0])
        interval = str(body.get("timeframes", ["?"])[0])
        called_at = int(time.time() * 1000)
        last_exc: Exception = RuntimeError("no attempts made")

        for attempt in range(1, self._RETRY_ATTEMPTS + 1):
            t0 = time.perf_counter()
            try:
                resp = self._session.post(url, json=body, timeout=1200)
                resp.raise_for_status()
                duration_ms = (time.perf_counter() - t0) * 1000
                if self._metrics_fn:
                    self._metrics_fn(
                        symbol, interval, "local", called_at, duration_ms, True, None
                    )
                return resp.json()
            except requests.RequestException as exc:
                last_exc = exc
                duration_ms = (time.perf_counter() - t0) * 1000
                if self._metrics_fn:
                    self._metrics_fn(
                        symbol,
                        interval,
                        "local",
                        called_at,
                        duration_ms,
                        False,
                        str(exc),
                    )
                if attempt < self._RETRY_ATTEMPTS:
                    wait = self._RETRY_BACKOFF[
                        min(attempt - 1, len(self._RETRY_BACKOFF) - 1)
                    ]
                    logger.warning(
                        "[%s %s] MT5 fetch failed (attempt %d/%d), retrying in %.0fs: %s",
                        symbol,
                        interval,
                        attempt,
                        self._RETRY_ATTEMPTS,
                        wait,
                        exc,
                    )
                    time.sleep(wait)

        raise MarketDataError(
            f"HTTP error on {path} after {self._RETRY_ATTEMPTS} attempts: {last_exc}"
        ) from last_exc

    def fetch_candles(
        self,
        symbol: str,
        interval: str,
        outputsize: int = 200,
        order: str = "ASC",
        allow_gaps: bool = True,
    ) -> list[Candle]:
        """Fetch the most recent `outputsize` bars for a symbol/interval."""
        mt5_interval = _to_mt5_interval(interval)
        response = self._post(
            "/time-series",
            {
                "symbols": [symbol],
                "timeframes": [mt5_interval],
                "limit": outputsize,
                "allow_gaps": allow_gaps,
            },
        )
        raw = response.get(symbol, {}).get(mt5_interval)
        candles = sorted(
            _parse_candles(_unwrap_raw(raw, symbol, mt5_interval)),
            key=lambda c: c.timestamp,
        )
        logger.debug(
            "[%s %s] fetch_candles: %d candles", symbol, interval, len(candles)
        )
        return candles

    def fetch_candles_range(
        self,
        symbol: str,
        interval: str,
        start_ts: int,
        end_ts: Optional[int] = None,
        allow_gaps: bool = True,
    ) -> list[Candle]:
        """Fetch all bars in [start_ts, end_ts] with automatic pagination."""
        from config.settings import interval_to_minutes  # lazy — avoids circular import

        mt5_interval = _to_mt5_interval(interval)
        bar_ms = interval_to_minutes(interval) * 60 * 1000
        end_ts = end_ts or int(time.time() * 1000)
        cursor = start_ts
        all_candles: list[Candle] = []
        seen_ts: set[int] = set()
        batch_num = 0

        logger.info(
            "[%s %s] fetch_candles_range: %s → %s",
            symbol,
            interval,
            _ms_to_utc_str(start_ts),
            _ms_to_utc_str(end_ts),
        )

        while cursor <= end_ts:
            batch_num += 1
            response = self._post(
                "/time-series",
                {
                    "symbols": [symbol],
                    "timeframes": [mt5_interval],
                    "from_date": _ms_to_utc_str(cursor),
                    "to_date": _ms_to_utc_str(end_ts),
                    "allow_gaps": allow_gaps,
                },
            )
            raw = response.get(symbol, {}).get(mt5_interval)
            batch = sorted(
                _parse_candles(_unwrap_raw(raw, symbol, mt5_interval)),
                key=lambda c: c.timestamp,
            )
            if not batch:
                break

            new = [c for c in batch if c.timestamp not in seen_ts]
            if not new:
                break

            for c in new:
                seen_ts.add(c.timestamp)
                if c.timestamp <= end_ts:
                    all_candles.append(c)

            if batch[-1].timestamp >= end_ts:
                break
            cursor = batch[-1].timestamp + bar_ms

        all_candles.sort(key=lambda c: c.timestamp)
        logger.info(
            "[%s %s] fetch_candles_range complete: %d candles  %d batch(es)",
            symbol,
            interval,
            len(all_candles),
            batch_num,
        )
        return all_candles

    def close(self) -> None:
        self._session.close()
