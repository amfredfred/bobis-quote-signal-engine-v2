"""
app/bootstrap.py — Wires all components and owns the event loop.
"""

from __future__ import annotations

import asyncio
import logging
import signal as os_signal
import sys
from pathlib import Path

from src.config.settings import Settings
from src.core.consensus import ConsensusEngine
from src.core.router import SignalRouter
from src.server.engine_server import EngineServer
from src.server.gateway_server import GatewayServer
from src.supervisor import ProcessSupervisor

logger = logging.getLogger(__name__)


def setup_logging(level: str) -> None:
    fmt = logging.Formatter(
        "%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    root = logging.getLogger()
    root.setLevel(getattr(logging, level, logging.INFO))
    if sys.stdout and not root.handlers:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        h = logging.StreamHandler(sys.stdout)
        h.setFormatter(fmt)
        root.addHandler(h)


async def run(settings: Settings) -> None:
    manager_url = f"ws://{settings.engine_host}:{settings.engine_port}"

    # ── Build components ──────────────────────────────────────────────────────
    gateway   = GatewayServer(
        host=settings.gateway_host,
        port=settings.gateway_port,
        supported_symbols=settings.symbols,
        secret=settings.gateway_secret,
    )
    consensus = ConsensusEngine(window_ms=settings.consensus_window_ms)
    router    = SignalRouter(consensus=consensus, gateway=gateway)
    engine_sv = EngineServer(
        host=settings.engine_host,
        port=settings.engine_port,
        token=settings.engine_token,
        on_signal=router.on_signal,
    )

    # Wire per-broker stats from engine_sv into the gateway metrics snapshot.
    gateway.set_stats_provider(engine_sv.broker_stats)

    supervisor: ProcessSupervisor | None = None
    if settings.sources and settings.signal_engine_path:
        supervisor = ProcessSupervisor(
            sources=list(settings.sources),
            engine_path=Path(settings.signal_engine_path),
            engine_config_name=settings.signal_engine_config,
            manager_url=manager_url,
            manager_token=settings.engine_token,
            manager_symbols=list(settings.symbols),
        )
    else:
        if not settings.sources:
            logger.warning(
                "No sources configured — no signal engines will be spawned. "
                "Add `sources:` to config.yaml."
            )
        if not settings.signal_engine_path:
            logger.warning(
                "signal_engine.path is not set — cannot spawn engines. "
                "Add `signal_engine.path:` to config.yaml."
            )

    # ── Start ─────────────────────────────────────────────────────────────────
    await gateway.start()
    await engine_sv.start()
    router.start()

    # Supervisor starts after the engine server is listening so workers can
    # immediately connect on their first launch.
    if supervisor:
        await supervisor.start()

    logger.info(
        "Signal Manager running  sources=%s  engines=ws://%s:%d  gateway=ws://%s:%d  "
        "consensus_window=%dms",
        list(settings.sources),
        settings.engine_host, settings.engine_port,
        settings.gateway_host, settings.gateway_port,
        settings.consensus_window_ms,
    )

    # ── Wait for shutdown ─────────────────────────────────────────────────────
    loop       = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def _shutdown() -> None:
        logger.info("Shutdown signal received")
        stop_event.set()

    for sig in (os_signal.SIGINT, os_signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _shutdown)
        except NotImplementedError:
            os_signal.signal(sig, lambda _s, _f: loop.call_soon_threadsafe(_shutdown))

    await stop_event.wait()

    # ── Stop ──────────────────────────────────────────────────────────────────
    logger.info("Signal Manager stopping")
    if supervisor:
        await supervisor.stop()
    router.stop()
    await engine_sv.stop()
    await gateway.stop()
    logger.info("Signal Manager stopped")
