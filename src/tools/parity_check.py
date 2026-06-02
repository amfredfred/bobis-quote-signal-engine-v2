"""Compare live and backtest parity traces."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any

PRICE_TOLERANCE = 1e-6
MONEY_TOLERANCE = 1e-2

FIELDS = (
    "symbol",
    "timeframe",
    "timestamp",
    "signal",
    "strategy",
    "entry",
    "stop_loss",
    "take_profit",
    "rr",
    "risk_amount",
    "position_size",
    "blocked_reason",
    "trade_outcome",
    "pnl",
)


def load_trace(path: str | Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with Path(path).open(encoding="utf-8") as fh:
        for line_no, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise SystemExit(f"{path}:{line_no}: invalid JSON: {exc}") from exc
    return rows


def _same(a: Any, b: Any, field: str) -> bool:
    if a is None or b is None:
        return a is b
    if isinstance(a, (int, float)) or isinstance(b, (int, float)):
        tol = MONEY_TOLERANCE if field in {"risk_amount", "pnl"} else PRICE_TOLERANCE
        return math.isclose(float(a), float(b), rel_tol=0.0, abs_tol=tol)
    return a == b


def compare_traces(live: list[dict[str, Any]], backtest: list[dict[str, Any]]) -> str | None:
    if not live and not backtest:
        return None
    if not live or not backtest:
        return f"trace length mismatch: live={len(live)} backtest={len(backtest)}"

    live_hash = live[0].get("config_hash")
    backtest_hash = backtest[0].get("config_hash")
    if live_hash != backtest_hash:
        return (
            "config_hash mismatch\n"
            f"Live:     {live_hash}\n"
            f"Backtest: {backtest_hash}"
        )

    if len(live) != len(backtest):
        return f"trace length mismatch: live={len(live)} backtest={len(backtest)}"

    for live_row, backtest_row in zip(live, backtest):
        for field in FIELDS:
            if _same(live_row.get(field), backtest_row.get(field), field):
                continue
            header = (
                "PARITY FAILED\n\n"
                f"Timestamp: {live_row.get('timestamp')}\n"
                f"Symbol: {live_row.get('symbol')}\n"
                f"Timeframe: {live_row.get('timeframe')}\n\n"
            )
            return (
                header +
                f"Field: {field}\n\n"
                f"Live:\n  {field}: {live_row.get(field)}\n\n"
                f"Backtest:\n  {field}: {backtest_row.get(field)}\n\n"
                "Reason:\n  Shared trace field differs."
            )
    return None


def main() -> None:
    parser = argparse.ArgumentParser(description="Compare live and backtest parity traces")
    parser.add_argument("--live", required=True)
    parser.add_argument("--backtest", required=True)
    args = parser.parse_args()

    mismatch = compare_traces(load_trace(args.live), load_trace(args.backtest))
    if mismatch:
        print(mismatch)
        raise SystemExit(1)
    print("PARITY PASSED")


if __name__ == "__main__":
    main()
