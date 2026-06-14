import asyncio
import sys
from pathlib import Path

# Allow `python -m src` from the signal-manager root
sys.path.insert(0, str(Path(__file__).parents[1]))

from src.config.settings import Settings
from src.app.bootstrap import run, setup_logging


def main() -> None:
    settings = Settings.from_yaml()
    setup_logging(settings.log_level)
    asyncio.run(run(settings))


if __name__ == "__main__":
    main()
