"""
interfaces/cli/main.py — application entry point.

Wires all dependencies in one place (composition root) and starts the engine.

Dependency graph (all injected, no module-level singletons):
  Settings
    ↓
  MarketDataClient  ←  Settings.local_base_url
  AssetRegistry     ←  Settings
  SignalStore       ←  Settings.session_dir / "signals.db"
  SessionStore      ←  Settings.session_dir, live_dir, session_tz
  SessionCoordinator ← SignalStore + SessionStore + Settings
  SignalService     ←  all of the above
  SignalScheduler   ←  loop + callback
  WebSocketServer   ←  SignalScheduler + SignalService + MarketDataClient + Settings
"""

from __future__ import annotations

import asyncio
import logging
import logging.handlers
import signal as os_signal
import sys
from pathlib import Path

from config.settings import Settings, interval_to_minutes
from infrastructure.data_providers.market_data import MarketDataClient
from infrastructure.persistence.signal_store import SignalStore
from infrastructure.persistence.session_store import SessionStore
from infrastructure.observability.metrics import MetricsCollector
from domain.assets.profiles import AssetRegistry
from app.session.coordinator import SessionCoordinator
from app.services.signal_service import SignalService
from interfaces.ws.scheduler import SignalScheduler, WatchMode
from interfaces.ws.server import WebSocketServer
from domain.entities.enums import SignalEvent

# ── Logging ───────────────────────────────────────────────────────────────────

def _setup_logging(log_level: str, log_dir: str) -> None:
    fmt  = logging.Formatter(
        "%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    level = getattr(logging, log_level.upper(), logging.INFO)
    root  = logging.getLogger()
    root.setLevel(level)

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    root.addHandler(sh)

    if log_dir:
        log_path = Path(log_dir)
        log_path.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(
            log_path / "signal_engine.log",
            maxBytes   = 10 * 1024 * 1024,
            backupCount = 5,
            encoding   = "utf-8",
        )
        fh.setFormatter(fmt)
        root.addHandler(fh)


logger = logging.getLogger("signal_engine.main")


# ── Engine ────────────────────────────────────────────────────────────────────

class SignalEngine:
    """Top-level orchestrator. Owns all components and their lifecycle."""

    def __init__(self, settings: Settings) -> None:
        self._cfg = settings

        # ── Infrastructure ────────────────────────────────────────────────────
        self._md = MarketDataClient(base_url=settings.local_base_url)

        db_path      = settings.session_dir / "signals.db"
        live_dir     = settings.base_dir / "results" / "live"
        self._store  = SignalStore(db_path)
        self._sstore = SessionStore(settings.session_dir, live_dir, settings.session_tz)
        self._metrics = MetricsCollector(settings)

        # ── Domain / app ──────────────────────────────────────────────────────
        self._registry = AssetRegistry(settings)
        self._session  = SessionCoordinator(self._store, self._sstore, settings)
        self._service = SignalService(
            market_data=self._md,
            settings=settings,
            asset_registry=self._registry,
            session=self._session,
            signal_store=self._store,
            metrics=self._metrics,
        )
        self._service.add_listener(self._on_signal_event)

        # Scheduler and WS server are created in start() once the loop is running
        self._scheduler: SignalScheduler
        self._ws:        WebSocketServer

    # ── Callbacks ─────────────────────────────────────────────────────────────

    async def _on_candle_close(self, symbol: str) -> None:
        mode = self._scheduler.get_mode(symbol)
        if mode == WatchMode.HTF_WATCH:
            await self._htf_tick(symbol)
        else:
            await self._ltf_tick(symbol)

    async def _htf_tick(self, symbol: str) -> None:
        try:
            loop = asyncio.get_running_loop()
            for htf_interval, ltf_interval in self._cfg.tf_pairs:
                htf = await loop.run_in_executor(
                    None,
                    lambda iv=htf_interval: self._md.fetch_candles(
                        symbol, iv, self._cfg.htf_outputsize
                    ),
                )
                if len(htf) < 10:
                    continue
                self._service.update_htf_cache(symbol, htf_interval, ltf_interval, htf)
                ltf = await loop.run_in_executor(
                    None,
                    lambda ts=htf[0].timestamp, iv=ltf_interval: self._md.fetch_candles_range(symbol, iv, ts),
                )
                if ltf:
                    self._service.update_ltf_cache(symbol, htf_interval, ltf_interval, ltf)

                if self._service.has_armed_zones(symbol) or self._service.has_active_htf_zones(symbol):
                    self._scheduler.set_symbol_mode(symbol, WatchMode.LTF_WATCH)
                    await self._ltf_tick(symbol)
        except Exception as exc:
            logger.error("[%s] HTF tick error: %s", symbol, exc, exc_info=True)

    async def _ltf_tick(self, symbol: str) -> None:
        try:
            min_ltf   = min(interval_to_minutes(ltf) for _, ltf in self._cfg.tf_pairs)
            now_ms    = self._cfg.now_ms()
            ltf_ms    = min_ltf * 60 * 1000
            fired_at  = (now_ms // ltf_ms) * ltf_ms - ltf_ms
            await self._service.analyze(symbol, fired_at=fired_at)
            await self._service.update_watchlist(symbol)

            armed    = self._service.has_armed_zones(symbol)
            has_active_zone = self._service.has_active_htf_zones(symbol)
            has_open = any(s.symbol == symbol for s in self._service.get_active_signals())
            if not (armed or has_open or has_active_zone):
                self._scheduler.set_symbol_mode(symbol, WatchMode.HTF_WATCH)
                logger.info("[%s] No armed zones/signals — reverting to HTF_WATCH", symbol)
        except Exception as exc:
            logger.error("[%s] LTF tick error: %s", symbol, exc, exc_info=True)

    def _on_signal_event(self, event: SignalEvent, payload: dict) -> None:
        self._ws.broadcast(event, payload)

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        self._scheduler = SignalScheduler(loop=loop, callback=self._on_candle_close, settings=self._cfg)
        self._ws = WebSocketServer(
            scheduler=self._scheduler,
            service=self._service,
            market_data=self._md,
            settings=self._cfg,
            metrics=self._metrics,
        )
        await self._ws.start(self._cfg.ws_host, self._cfg.ws_port)
        pairs_str = "  |  ".join(f"{h}/{l}" for h, l in self._cfg.tf_pairs)
        logger.info("Signal Engine running  ws://%s:%d  TF pairs: [%s]",
                    self._cfg.ws_host, self._cfg.ws_port, pairs_str)

    async def stop(self) -> None:
        logger.info("Shutting down Signal Engine…")
        self._scheduler.shutdown()
        await self._ws.stop()
        self._md.close()
        logger.info("Signal Engine stopped")


# ── Main ──────────────────────────────────────────────────────────────────────

async def _main() -> None:
    settings = Settings.from_env()
    _setup_logging(settings.log_level, settings.log_dir)

    engine = SignalEngine(settings)
    await engine.start()

    loop       = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def _handle_signal() -> None:
        logger.info("Shutdown signal received")
        stop_event.set()

    for sig in (os_signal.SIGINT, os_signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _handle_signal)
        except NotImplementedError:
            os_signal.signal(sig, lambda _s, _f: loop.call_soon_threadsafe(_handle_signal))

    await stop_event.wait()
    await engine.stop()


if __name__ == "__main__":
    asyncio.run(_main())


def main() -> None:
    """
    Synchronous entry point — required by [project.scripts] in pyproject.toml.
    setuptools entry points must be plain callables; asyncio.run() bridges to
    the async implementation.
    """
    asyncio.run(_main())
