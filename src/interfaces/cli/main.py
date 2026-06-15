"""
interfaces/cli/main.py — application entry point.

Wires all dependencies in one place (composition root) and starts the engine.

Dependency graph (all injected, no module-level singletons):
  Settings
    ↓
  MarketDataClient  ←  Settings MT5 terminal fields
  AssetRegistry     ←  Settings
  SignalStore       ←  Settings.session_dir / "signals.db"
  SessionStore      ←  Settings.session_dir, live_dir
  SessionCoordinator ← SignalStore + SessionStore + Settings
  SignalService     ←  all of the above
  SignalScheduler   ←  loop + callback
  WebSocketServer   ←  SignalScheduler + SignalService + MarketDataClient + Settings

WatchMode / dual-cadence removed: the engine now runs every symbol at LTF
cadence unconditionally. Direct MT5 access makes the old API-cost optimisation
unnecessary. _htf_tick, has_armed_zones, has_active_htf_zones, and
the mode-switching guards in _ltf_tick are all gone.
"""

from __future__ import annotations

import asyncio
import argparse
import logging
import logging.handlers
import signal as os_signal
import sys
import time
from pathlib import Path

from config.settings import Settings, interval_to_minutes
from infrastructure.data_providers.market_data import MarketDataClient
from infrastructure.persistence.signal_store import SignalStore
from infrastructure.persistence.session_store import SessionStore
from infrastructure.observability.metrics import MetricsCollector
from domain.assets.profiles import AssetRegistry
from domain.entities.trade import TradeSignal
from app.engine.parity_trace import ParityTraceWriter, trace_from_signal
from app.session.coordinator import SessionCoordinator
from app.services.signal_service import SignalService
from interfaces.ws.scheduler import SignalScheduler
from interfaces.ws.server import WebSocketServer
from interfaces.ws.manager_client import ManagerClient
from domain.entities.enums import SignalEvent

# ── Logging ───────────────────────────────────────────────────────────────────


def _setup_logging(log_level: str, log_dir: str) -> None:
    fmt = logging.Formatter(
        "%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    level = getattr(logging, log_level.upper(), logging.INFO)
    root = logging.getLogger()
    root.setLevel(level)

    if sys.stdout is not None:
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
            maxBytes=10 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
        fh.setFormatter(fmt)
        root.addHandler(fh)


logger = logging.getLogger("signal_engine.main")


# ── Engine ────────────────────────────────────────────────────────────────────


class SignalEngine:
    """Top-level orchestrator. Owns all components and their lifecycle."""

    def __init__(self, settings: Settings, live_trace_out: str | None = None) -> None:
        self._cfg = settings
        self._trace_ctx = ParityTraceWriter(live_trace_out)
        self._trace_writer = self._trace_ctx.__enter__()

        # ── Infrastructure ────────────────────────────────────────────────────
        self._metrics = MetricsCollector(settings)
        self._md = MarketDataClient.from_settings(
            settings, metrics_fn=self._metrics.on_api_call
        )

        db_path = settings.session_dir / "signals.db"
        live_dir = settings.base_dir / "results" / "live"
        self._store = SignalStore(db_path)
        self._sstore = SessionStore(settings.session_dir, live_dir)

        # ── Domain / app ──────────────────────────────────────────────────────
        self._registry = AssetRegistry(settings)
        self._session = SessionCoordinator(self._store, self._sstore, settings)
        self._service = SignalService(
            market_data=self._md,
            settings=settings,
            asset_registry=self._registry,
            session=self._session,
            signal_store=self._store,
            metrics=self._metrics,
        )
        self._service.add_listener(self._on_signal_event)

        # Scheduler and broadcaster are created in start() once the loop is running
        self._scheduler: SignalScheduler
        self._ws: WebSocketServer | ManagerClient
        self._manager_log_handler: logging.Handler | None = None

    # ── Tick ──────────────────────────────────────────────────────────────────

    async def _on_candle_close(self, symbol: str) -> None:
        started = time.perf_counter()
        analysis_close = 0
        try:
            min_ltf = min(interval_to_minutes(ltf) for _, ltf in self._cfg.tf_pairs)
            now_ms = self._md.now_ms(symbol)
            ltf_ms = min_ltf * 60 * 1000
            analysis_close = (now_ms // ltf_ms) * ltf_ms
            logger.info(
                "[%s] Candle-close tick now=%s analysis_close=%s ltf_ms=%s",
                symbol,
                self._cfg.dt_ms(now_ms),
                self._cfg.dt_ms(analysis_close),
                ltf_ms,
            )
            # update_watchlist MUST run before analyze so that any positions
            # closed on the previous bar release their direction lock before
            # the new-signal scan runs.  If analyze runs first, a Z2 re-entry
            # on the same zone is blocked by the still-open lock even though
            # the preceding trade already hit SL.
            await self._service.update_watchlist(symbol)
            await self._service.analyze(symbol, fired_at=analysis_close)
        except Exception as exc:
            self._metrics.increment("scanner.tick_errors")
            self._metrics.record_error("signal_engine.main", "ERROR", str(exc))
            logger.error("[%s] Tick error: %s", symbol, exc, exc_info=True)
        finally:
            if analysis_close:
                duration_ms = (time.perf_counter() - started) * 1000
                self._metrics.record_tick(symbol, "ltf", analysis_close, duration_ms)
                self._metrics.update_scheduler_state(
                    symbol, "ltf", last_fired_at=analysis_close
                )

    def _on_signal_event(self, event: SignalEvent, payload: dict) -> None:
        if self._trace_writer and payload.get("signal"):
            try:
                signal = TradeSignal.from_dict(payload["signal"])
                self._trace_writer.write(
                    trace_from_signal(
                        mode="live",
                        signal=signal,
                        cfg=self._cfg,
                        outcome=signal.outcome,
                    )
                )
            except Exception as exc:
                logger.warning("Failed to write live parity trace: %s", exc)
        self._ws.broadcast(event, payload)

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        await self._service.initialize()

        loop = asyncio.get_running_loop()
        self._scheduler = SignalScheduler(
            loop=loop,
            callback=self._on_candle_close,
            settings=self._cfg,
        )
        pairs_str = "  |  ".join(f"{h}/{l}" for h, l in self._cfg.tf_pairs)

        if self._cfg.manager_mode == "worker":
            self._ws = ManagerClient(
                url=self._cfg.manager_url,
                token=self._cfg.manager_token,
                broker=self._cfg.mt5_profile,
            )
            await self._ws.start()
            self._ws.start_metrics(self._metrics.build_snapshot)
            self._manager_log_handler = self._ws.create_log_handler(
                getattr(logging, self._cfg.log_level.upper(), logging.INFO)
            )
            logging.getLogger().addHandler(self._manager_log_handler)
            # Auto-subscribe configured symbols so the engine starts analysing
            # without waiting for a downstream client to subscribe.
            symbols = list(self._cfg.manager_symbols)
            if symbols:
                self._scheduler.subscribe("manager", symbols)
                logger.info("Signal Engine (worker) auto-subscribed: %s", symbols)
            logger.info(
                "Signal Engine running  mode=worker  manager=%s  broker=%s  TF pairs: [%s]",
                self._cfg.manager_url,
                self._cfg.mt5_profile,
                pairs_str,
            )
        else:
            self._ws = WebSocketServer(
                scheduler=self._scheduler,
                service=self._service,
                market_data=self._md,
                settings=self._cfg,
                metrics=self._metrics,
            )
            await self._ws.start(self._cfg.ws_host, self._cfg.ws_port)
            logger.info(
                "Signal Engine running  mode=standalone  ws://%s:%d  TF pairs: [%s]",
                self._cfg.ws_host,
                self._cfg.ws_port,
                pairs_str,
            )

    async def stop(self) -> None:
        logger.info("Shutting down Signal Engine…")
        if self._manager_log_handler:
            logging.getLogger().removeHandler(self._manager_log_handler)
            self._manager_log_handler.close()
            self._manager_log_handler = None
        self._scheduler.shutdown()
        await self._ws.stop()
        self._md.close()
        self._metrics.close()
        self._trace_ctx.__exit__(None, None, None)
        logger.info("Signal Engine stopped")


# ── Main ──────────────────────────────────────────────────────────────────────


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the live signal engine")
    parser.add_argument(
        "--live-trace-out",
        help="Write deterministic live parity trace JSONL to this path",
    )
    return parser.parse_args()


async def _main() -> None:
    args = _parse_args()
    settings = Settings.from_env()
    _setup_logging(settings.log_level, settings.log_dir)

    engine = SignalEngine(settings, live_trace_out=args.live_trace_out)
    await engine.start()

    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def _handle_signal() -> None:
        logger.info("Shutdown signal received")
        stop_event.set()

    for sig in (os_signal.SIGINT, os_signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _handle_signal)
        except NotImplementedError:
            os_signal.signal(
                sig, lambda _s, _f: loop.call_soon_threadsafe(_handle_signal)
            )

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
