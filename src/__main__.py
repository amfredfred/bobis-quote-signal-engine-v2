"""
Module entry point — allows `python -m src` from the signal-engine root.

Adds src/ itself to sys.path so bare package imports (from config.settings ...)
resolve correctly, then delegates to the CLI entry point.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from interfaces.cli.main import main  # noqa: E402

main()
