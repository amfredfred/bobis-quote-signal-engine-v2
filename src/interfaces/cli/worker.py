"""
interfaces/cli/worker.py — Worker entry point for ProcessSupervisor
"""

import asyncio
import logging
import os
import sys
from pathlib import Path

from config.settings import Settings
from interfaces.events.gateway import EventGateway
from interfaces.cli.main import SignalEngine
import logging

logger = logging.getLogger("signal_engine.work")

async def run_worker() -> None:
    # Read environment variables injected by manager
    broker = os.getenv("MT5_USE")
    manager_url = os.getenv("MANAGER_URL")
    manager_token = os.getenv("MANAGER_TOKEN")
    manager_symbols = os.getenv("MANAGER_SYMBOLS", "").split(",")

    if not broker or not manager_url:
        print(
            "Error: Missing MANAGER_URL or MT5_USE environment variables",
            file=sys.stderr,
        )
        sys.exit(1)

    # Load settings (will pick up MT5_USE, etc.)
    settings = Settings.for_broker(  # Make sure this method exists
        config_path=Path(os.getenv("USE_CONFIG")),
        broker=broker,
        base_dir=Path.cwd(),
        symbols=[s.strip() for s in manager_symbols if s.strip()],
    )

    logger = logging.getLogger(__name__)
    logger.info(f"Starting worker for broker: {broker}")

    gateway = EventGateway()
    engine = SignalEngine(settings)

    try:
        await engine.start(event_gateway=gateway)
        logger.info(f"Worker {broker} started successfully")
        await asyncio.Event().wait()  # Keep running
    except Exception as e:
        logger.error(f"Worker {broker} crashed: {e}", exc_info=True)
        sys.exit(1)
    finally:
        await engine.stop()


def main() -> None:
    asyncio.run(run_worker())


if __name__ == "__main__":
    main()
