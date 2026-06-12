from __future__ import annotations

import sys
from unittest.mock import patch

from app.backtesting import rba


def test_rba_forwards_risk_percent(tmp_path) -> None:
    argv = [
        "rba.py",
        "--symbol",
        "XAUUSD",
        "--from-date",
        "2022-01-01",
        "--start-balance",
        "5000",
        "--risk-percent",
        "0.25",
        "--output-dir",
        str(tmp_path),
    ]

    with (
        patch.object(sys, "argv", argv),
        patch("app.backtesting.rba.run_with_retry") as run,
    ):
        run.return_value = ("XAUUSD", True, 0.1, "")
        rba.main()

    extra_args = run.call_args.args[2]
    assert "--risk-percent" in extra_args
    assert extra_args[extra_args.index("--risk-percent") + 1] == "0.25"
