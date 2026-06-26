"""
analyze_grid.py — portfolio-level analytics on grid search results.

Reads from per-pair optimal combo folders and reports:
  - Days with zero trades across the entire portfolio
  - Daily trade count distribution
  - Per-pair trade frequency
"""

from __future__ import annotations

import csv
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

import argparse

_p = argparse.ArgumentParser()
_p.add_argument("--results-dir", default="results/grid")
_args = _p.parse_args()

GRID_DIR = Path(_args.results_dir)

# Optimal combo per pair (from grid search analysis)
PAIR_COMBO = {
    "XAUUSD": "min12.0_max3.0",
    "XAGUSD":  "min12.0_max3.0",
}

WIN_OUTCOMES  = {"WIN_FULL", "WIN_PARTIAL", "WIN", "TP1_HIT", "TP2_HIT"}
LOSS_OUTCOMES = {"LOSS", "SL_HIT"}


def load_trades(symbol: str, combo: str) -> list[dict]:
    path = GRID_DIR / combo / f"{symbol}.csv"
    if not path.exists():
        print(f"  WARNING: {path} not found")
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def trade_date(row: dict) -> date:
    return date.fromisoformat(row["entry_dt"][:10])


def main() -> None:
    # Load all trades per pair
    all_trades: list[dict] = []
    pair_trades: dict[str, list[dict]] = {}

    for symbol, combo in PAIR_COMBO.items():
        trades = load_trades(symbol, combo)
        pair_trades[symbol] = trades
        all_trades.extend(trades)

    if not all_trades:
        print("No trades found.")
        return

    # Date range
    all_dates = [trade_date(r) for r in all_trades]
    start_date = min(all_dates)
    end_date   = max(all_dates)
    total_days = (end_date - start_date).days + 1

    # All calendar days in range
    all_calendar = {start_date + timedelta(days=i) for i in range(total_days)}

    # Days that had at least one trade (any pair)
    active_days = {trade_date(r) for r in all_trades}
    zero_days   = all_calendar - active_days

    # Weekends (Sat=5, Sun=6) — markets closed
    weekends = {d for d in all_calendar if d.weekday() >= 5}
    trading_days = all_calendar - weekends
    zero_trading_days = zero_days - weekends

    # Daily trade count
    daily_counts: dict[date, int] = defaultdict(int)
    for r in all_trades:
        daily_counts[trade_date(r)] += 1

    counts = [daily_counts[d] for d in trading_days if d in active_days]
    avg_trades = sum(counts) / len(counts) if counts else 0

    # ── Report ─────────────────────────────────────────────────────────────────
    print("\n" + "=" * 55)
    print("  PORTFOLIO TRADE FREQUENCY ANALYSIS")
    print("=" * 55)
    print(f"  Period          : {start_date}  →  {end_date}")
    print(f"  Calendar days   : {total_days}")
    print(f"  Weekends        : {len(weekends)}")
    print(f"  Trading days    : {len(trading_days)}")
    print()
    print(f"  Days with trade : {len(active_days - weekends)}"
          f"  ({(len(active_days - weekends)/len(trading_days))*100:.1f}%)")
    print(f"  Zero-trade days : {len(zero_trading_days)}"
          f"  ({(len(zero_trading_days)/len(trading_days))*100:.1f}%)")
    print(f"  Avg trades/day  : {avg_trades:.1f}  (active days only)")
    print()

    # Distribution of daily trade count
    print("  Daily trade count distribution (trading days with trades):")
    dist: dict[int, int] = defaultdict(int)
    for c in counts:
        dist[c] += 1
    for k in sorted(dist):
        bar = "█" * min(k, 30)
        print(f"    {k:2d} trade(s): {dist[k]:3d} days  {bar}")

    # Per-pair breakdown
    print()
    print("  Per-pair trade frequency:")
    print(f"  {'Symbol':<8} {'Trades':>7} {'Active days':>12} {'Avg/day':>8}")
    print("  " + "-" * 40)
    for symbol, trades in pair_trades.items():
        if not trades:
            continue
        days = {trade_date(r) for r in trades}
        avg = len(trades) / len(trading_days)
        print(f"  {symbol:<8} {len(trades):>7} {len(days):>12} {avg:>8.2f}")

    # Lowest trade days — bottom 10
    active_trading_days = sorted(
        [(d, daily_counts[d]) for d in trading_days if d in active_days],
        key=lambda x: x[1]
    )
    print(f"  Lowest trade days (bottom 10):")
    print(f"  {'Date':<12} {'Day':<10} {'Trades':>6}  Pairs active")
    print("  " + "-" * 50)
    for d, count in active_trading_days[:10]:
        pairs_on_day = [sym for sym, trades in pair_trades.items()
                        if any(trade_date(r) == d for r in trades)]
        print(f"  {str(d):<12} {d.strftime('%A'):<10} {count:>6}  {', '.join(pairs_on_day)}")

    print()


if __name__ == "__main__":
    main()
