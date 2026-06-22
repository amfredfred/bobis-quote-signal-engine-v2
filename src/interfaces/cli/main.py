"""
interfaces/cli/main.py — SignalEngine composition root.

Wires all dependencies and exposes start() / stop() for the manager to call.
The engine no longer has a WebSocket server — it runs in-process inside the
manager, emitting events through EventGateway callbacks.

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
  EventGateway      ←  injected by PipelineManager (callbacks, no network)
"""

from __future__ import annotations

import asyncio
import logging
import sys
import time

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
from interfaces.scheduler import SignalScheduler
from interfaces.events.gateway import EventGateway
from domain.entities.enums import SignalEvent

logger = logging.getLogger("signal_engine.main")


class SignalEngine:
    """Top-level orchestrator. Owned and started by PipelineManager."""

    def __init__(self, settings: Settings, live_trace_out: str | None = None) -> None:
        self._cfg = settings
        self._trace_ctx = ParityTraceWriter(live_trace_out)
        self._trace_writer = self._trace_ctx.__enter__()
        
        # ── Infrastructure ────────────────────────────────────────────────────
        self._metrics = MetricsCollector(settings)
        # MarketDataClient is constructed here but does NOT connect to MT5 yet.
        # mt5.initialize() is deferred to start() → self._md.connect() so that
        # multiple brokers running in-process don't race on the MT5 COM layer.
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
        # SignalService and its listener are wired in start() after MT5 connects.
        self._service: SignalService | None = None

        self._scheduler: SignalScheduler | None = None
        self._gateway: EventGateway | None = None

    def get_metrics(self) -> dict:
        """Return the current metrics snapshot (polled by PipelineManager)."""
        return self._metrics.build_snapshot()

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
        if self._gateway is not None:
            self._gateway.broadcast(event, payload)

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self, event_gateway: EventGateway) -> None:
        self._gateway = event_gateway

        # Connect to MT5 here, not in __init__, so each broker's connection is
        # established sequentially within its own supervised task rather than
        # all brokers racing on mt5.initialize() at construction time.

        self._service = SignalService(
            market_data=self._md,
            settings=self._cfg,
            asset_registry=self._registry,
            session=self._session,
            signal_store=self._store,
            metrics=self._metrics,
        )
        self._service.add_listener(self._on_signal_event)

        await self._service.initialize()

        loop = asyncio.get_running_loop()
        self._scheduler = SignalScheduler(
            loop=loop,
            callback=self._on_candle_close,
            settings=self._cfg,
        )

        symbols = list(self._cfg.manager_symbols)
        if symbols:
            self._scheduler.subscribe("pipeline", symbols)
            # Pre-populate scheduler state so metrics show rows before first tick.
            for sym in symbols:
                self._metrics.update_scheduler_state(sym.upper(), "ltf")
            logger.info("Signal Engine auto-subscribed: %s", symbols)

        pairs_str = "  |  ".join(f"{h}/{l}" for h, l in self._cfg.tf_pairs)
        logger.info(
            "Signal Engine running  broker=%s  TF pairs: [%s]",
            self._cfg.mt5_profile,
            pairs_str,
        )

    async def stop(self) -> None:
        logger.info("[%s] Shutting down…", self._cfg.mt5_profile)
        if self._scheduler is not None:
            self._scheduler.shutdown()
        self._md.close()
        self._metrics.close()
        self._trace_ctx.__exit__(None, None, None)
        logger.info("[%s] Stopped", self._cfg.mt5_profile)


def main() -> None:
    """Entry point declared in pyproject.toml [project.scripts].

    The signal engine runs in-process via the manager — start the manager
    instead:  python -m src  (from signal-engine/manager/)
    """
    
    print(
        "signal-engine runs in-process inside the Signal Manager.\n"
        "Start the manager instead:\n"
        "  cd signal-engine/manager\n"
        "  python -m src",
        file=sys.stderr,
    )
    sys.exit(1)
