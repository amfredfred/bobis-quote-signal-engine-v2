"""
core/pipeline.py — In-process broker pipeline manager.

Replaces EngineServer + ProcessSupervisor.  Each broker runs as its own
SignalEngine asyncio Task instead of a subprocess.  Signal events flow
through EventGateway callbacks — no IPC, no token issues.

Supervision model:
  - One asyncio.Task per broker (independent: one crash can't affect others)
  - Exponential back-off restart: 5s → 10s → 20s → … → 300s
  - Manual restart resets the back-off counter
"""
from __future__ import annotations

import asyncio
import contextvars
import logging
import logging.handlers
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger(__name__)

OnSignal = Callable[[str, dict, str], None]   # (event_str, payload, broker)
OnEvent  = Callable[[str, dict], None]         # (event_str, payload)

_INIT_BACKOFF = 5.0     # seconds before first restart
_MAX_BACKOFF  = 300.0   # cap at 5 minutes
_METRICS_INTERVAL = 5.0

# Each asyncio Task gets its own copy of this var — so per-broker file handlers
# can filter using it without cross-broker contamination.
_current_broker: contextvars.ContextVar[str] = contextvars.ContextVar(
    "pipeline_broker", default=""
)


class _BrokerFilter(logging.Filter):
    """Only passes log records emitted by the named broker's asyncio task."""
    def __init__(self, broker: str) -> None:
        super().__init__()
        self.broker = broker

    def filter(self, record: logging.LogRecord) -> bool:
        return _current_broker.get("") == self.broker


@dataclass
class EngineEntry:
    broker: str
    task: Optional[asyncio.Task] = field(default=None, repr=False)
    health: str = "starting"         # starting | running | error | stopped
    restart_count: int = 0
    last_error: Optional[str] = None
    connected_at: Optional[float] = None
    signals_received: int = 0
    latest_metrics: Optional[dict] = None
    latest_scheduler: list = field(default_factory=list)


class PipelineManager:
    """Owns one in-process SignalEngine per broker."""

    def __init__(
        self,
        signal_engine_path: Path,
        config_path: Path,
        sources: list[str],
        on_signal: OnSignal,
        symbols: tuple[str, ...],
    ) -> None:
        self._engine_path = signal_engine_path
        self._config_path = config_path
        self._sources     = list(sources)
        self._on_signal   = on_signal
        self._symbols     = symbols
        self._entries: dict[str, EngineEntry] = {}

    async def start(self) -> None:
        src = str(self._engine_path / "src")
        if src not in sys.path:
            sys.path.insert(0, src)

        for broker in self._sources:
            entry = EngineEntry(broker=broker)
            self._entries[broker] = entry
            entry.task = asyncio.get_running_loop().create_task(
                self._supervise(broker),
                name=f"pipeline-{broker}",
            )
        logger.info("PipelineManager started: %s", self._sources)

    async def stop(self) -> None:
        tasks = [
            e.task for e in self._entries.values()
            if e.task and not e.task.done()
        ]
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._entries.clear()
        logger.info("PipelineManager stopped")

    async def restart(self, broker: str) -> None:
        """Manual restart — resets back-off and spawns a fresh task."""
        entry = self._entries.get(broker)
        if not entry:
            raise ValueError(f"Unknown broker: {broker!r}")
        if entry.task and not entry.task.done():
            entry.task.cancel()
            try:
                await entry.task
            except (asyncio.CancelledError, Exception):
                pass
        entry.restart_count = 0
        entry.task = asyncio.get_running_loop().create_task(
            self._supervise(broker),
            name=f"pipeline-{broker}",
        )
        logger.info("[%s] Manual restart initiated", broker)

    def get_stats(self) -> dict[str, dict]:
        """Per-broker stats for GatewayServer._build_metrics()."""
        return {
            broker: {
                "connected":        entry.health == "running",
                "connected_since":  entry.connected_at,
                "signals_received": entry.signals_received,
                "latest_metrics":   entry.latest_metrics,
                "scheduler":        entry.latest_scheduler,
            }
            for broker, entry in self._entries.items()
        }

    # ── Supervision ───────────────────────────────────────────────────────────

    async def _supervise(self, broker: str) -> None:
        # Tag this task's context so the per-broker file handler can filter.
        _current_broker.set(broker)
        entry   = self._entries[broker]
        backoff = _INIT_BACKOFF

        while True:
            entry.health    = "starting"
            entry.last_error = None
            try:
                await self._run_engine(broker, entry)
                return   # clean exit (only happens when task is cancelled inside)
            except asyncio.CancelledError:
                entry.health = "stopped"
                logger.info("[%s] Engine stopped", broker)
                return
            except Exception as exc:
                entry.health       = "error"
                entry.last_error   = str(exc)
                entry.connected_at = None
                entry.restart_count += 1
                logger.error(
                    "[%s] Engine crashed (attempt %d): %s",
                    broker, entry.restart_count, exc, exc_info=True,
                )

            logger.info("[%s] Restarting in %.0fs", broker, backoff)
            try:
                await asyncio.sleep(backoff)
            except asyncio.CancelledError:
                entry.health = "stopped"
                return
            backoff = min(backoff * 2, _MAX_BACKOFF)

    async def _run_engine(self, broker: str, entry: EngineEntry) -> None:
        # Lazy imports — sys.path is extended by start()
        from config.settings import Settings
        from interfaces.cli.main import SignalEngine
        from interfaces.events.gateway import EventGateway

        base_dir = self._engine_path / "manager" / broker
        base_dir.mkdir(parents=True, exist_ok=True)

        # Per-broker rotating log file: manager/{broker}/logs/engine.log
        log_path = base_dir / "logs" / "engine.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        _fmt = logging.Formatter(
            "%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        _fh = logging.handlers.RotatingFileHandler(
            log_path, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
        )
        _fh.setFormatter(_fmt)
        _fh.addFilter(_BrokerFilter(broker))
        root_logger = logging.getLogger()
        root_logger.addHandler(_fh)

        settings = Settings.for_broker(
            config_path=self._config_path,
            broker=broker,
            base_dir=base_dir,
            symbols=self._symbols,
        )

        gateway = EventGateway()
        gateway.subscribe(self._make_handler(broker, entry))

        engine = SignalEngine(settings)
        metrics_task: asyncio.Task | None = None
        try:
            await engine.start(event_gateway=gateway)
            entry.health       = "running"
            entry.connected_at = time.time()
            logger.info("[%s] Engine running", broker)

            metrics_task = asyncio.get_running_loop().create_task(
                self._metrics_loop(engine, entry),
                name=f"metrics-{broker}",
            )

            # Park here until this task is cancelled or something raises
            await asyncio.sleep(float("inf"))
        finally:
            if metrics_task and not metrics_task.done():
                metrics_task.cancel()
                try:
                    await metrics_task
                except (asyncio.CancelledError, Exception):
                    pass
            try:
                await engine.stop()
            except Exception as exc:
                logger.warning("[%s] Error stopping engine: %s", broker, exc)
            root_logger.removeHandler(_fh)
            _fh.close()

    async def _metrics_loop(self, engine, entry: EngineEntry) -> None:
        try:
            while True:
                await asyncio.sleep(_METRICS_INTERVAL)
                try:
                    snapshot = engine.get_metrics()
                    entry.latest_metrics = snapshot.get("metrics", snapshot)
                    entry.latest_scheduler = snapshot.get("scheduler", [])
                except Exception as exc:
                    logger.warning("[%s] Metrics error: %s", entry.broker, exc)
        except asyncio.CancelledError:
            pass

    def _make_handler(self, broker: str, entry: EngineEntry) -> Callable:
        on_signal = self._on_signal

        def _handler(event, payload: dict) -> None:
            event_str = event.value if hasattr(event, "value") else str(event)
            if event_str == "signal.triggered":
                entry.signals_received += 1
            on_signal(event_str, payload, broker)

        return _handler
