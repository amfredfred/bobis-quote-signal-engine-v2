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
from src.core.pipeline import PipelineManager
from src.core.router import SignalRouter
from src.server.gateway_server import GatewayServer

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
    if not settings.signal_engine_path:
        logger.error(
            "signal_engine.path is not set — cannot run pipeline engines. "
            "Add signal_engine.path to config.yaml."
        )
        return
    if not settings.sources:
        logger.warning("No sources configured — no signal engines will start.")

    engine_path = Path(settings.signal_engine_path)
    config_path = engine_path / settings.signal_engine_config

    # ── Build components ──────────────────────────────────────────────────────
    gateway   = GatewayServer(
        host=settings.gateway_host,
        port=settings.gateway_port,
        supported_symbols=settings.symbols,
        secret=settings.gateway_secret,
    )
    consensus = ConsensusEngine(window_ms=settings.consensus_window_ms)
    router    = SignalRouter(consensus=consensus, gateway=gateway)
    pipeline  = PipelineManager(
        signal_engine_path=engine_path,
        config_path=config_path,
        sources=list(settings.sources),
        on_signal=router.on_signal,
        symbols=settings.symbols,
    )

    gateway.set_stats_provider(pipeline.get_stats)

    # ── Start ─────────────────────────────────────────────────────────────────
    await gateway.start()
    router.start()
    await pipeline.start()

    logger.info(
        "Signal Manager running  sources=%s  gateway=ws://%s:%d  "
        "consensus_window=%dms",
        list(settings.sources),
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
    await pipeline.stop()
    router.stop()
    await gateway.stop()
    logger.info("Signal Manager stopped")
