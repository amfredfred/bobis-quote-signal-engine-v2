"""
grid_search.py — fast parameter grid search over min_rr × max_rr.

Runs all (min_rr, max_rr) combos across every supported symbol in parallel.
Shared --cache-dir means MT5 data is fetched once and reused across all runs.

Usage:
    py grid_search.py --from-date 2026-01-01
    py grid_search.py --from-date 2025-01-01 --to-date 2025-12-31
"""

from __future__ import annotations

import argparse
import csv
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from itertools import product
from pathlib import Path

from domain.assets.profiles import SUPPORTED_SYMBOLS

# ── Grid definition ────────────────────────────────────────────────────────────

MIN_RR_VALUES = [5.0, 6.0, 7.0, 8.0, 10.0, 12.0, 14.0]
MAX_RR_VALUES = [2.0, 2.5, 3.0]

SYMBOLS = sorted(SUPPORTED_SYMBOLS)

# ── Helpers ────────────────────────────────────────────────────────────────────

WIN_OUTCOMES  = {"WIN_FULL", "WIN_PARTIAL", "WIN", "TP1_HIT", "TP2_HIT"}
LOSS_OUTCOMES = {"LOSS", "SL_HIT"}


def _combo_key(min_rr: float, max_rr: float) -> str:
    return f"min{min_rr:.1f}_max{max_rr:.1f}"


def _parse_csv(path: Path) -> dict | None:
    """Return {trades, wins, losses, win_rate, profit_factor} or None if empty."""
    if not path.exists():
        return None
    try:
        with open(path, newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
    except Exception:
        return None

    if not rows:
        return None

    wins   = [r for r in rows if r.get("outcome", "").upper() in WIN_OUTCOMES]
    losses = [r for r in rows if r.get("outcome", "").upper() in LOSS_OUTCOMES]

    gross_win  = sum(float(r["realized_rr"]) for r in wins)
    gross_loss = sum(abs(float(r["realized_rr"])) for r in losses)

    n = len(rows)
    pf = (gross_win / gross_loss) if gross_loss > 0 else float("inf")
    wr = len(wins) / n if n > 0 else 0.0

    return {
        "trades": n,
        "wins":   len(wins),
        "losses": len(losses),
        "win_rate": round(wr * 100, 1),
        "profit_factor": round(pf, 2),
    }


def run_one(symbol: str, min_rr: float, max_rr: float,
            out_file: Path,
            extra: list[str], timeout: int) -> tuple[str, float, float, bool, str]:
    cmd = [
        sys.executable, "-m", "src.app.backtesting.backtest",
        "--symbol",  symbol,
        "--output",  str(out_file),
        "--min-rr",  str(min_rr),
        "--max-rr",  str(max_rr),
        *extra,
    ]
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
        ok = r.returncode == 0
        msg = (r.stdout + r.stderr).strip().splitlines()[-1] if not ok else ""
        return symbol, min_rr, max_rr, ok, msg
    except subprocess.TimeoutExpired:
        return symbol, min_rr, max_rr, False, "TIMEOUT"
    except Exception as e:
        return symbol, min_rr, max_rr, False, str(e)


# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--from-date", required=False)
    p.add_argument("--parse-only", action="store_true", help="Skip runs, just re-parse existing CSVs")
    p.add_argument("--to-date",   default=None)
    p.add_argument("--workers",   type=int, default=min(8, os.cpu_count() or 4))
    p.add_argument("--timeout",   type=int, default=1800)
    p.add_argument("--results-dir", default="results/grid")
    args = p.parse_args()

    results_root = Path(args.results_dir)
    results_root.mkdir(parents=True, exist_ok=True)

    combos = list(product(MIN_RR_VALUES, MAX_RR_VALUES))

    if not args.parse_only:
        if not args.from_date:
            print("ERROR: --from-date required unless --parse-only is set")
            sys.exit(1)

        extra = ["--from-date", args.from_date]
        if args.to_date:
            extra += ["--to-date", args.to_date]

        total = len(combos) * len(SYMBOLS)
        print(f"\nGrid search: {len(MIN_RR_VALUES)} min_rr × {len(MAX_RR_VALUES)} max_rr"
              f" = {len(combos)} combos × {len(SYMBOLS)} symbols = {total} runs")
        print(f"Workers: {args.workers}\n")

        tasks = []
        for min_rr, max_rr in combos:
            combo_dir = results_root / _combo_key(min_rr, max_rr)
            combo_dir.mkdir(parents=True, exist_ok=True)
            for sym in SYMBOLS:
                out_file = combo_dir / f"{sym}.csv"
                tasks.append((sym, min_rr, max_rr, out_file))

        done = 0
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = {
                pool.submit(run_one, sym, mn, mx, out, extra, args.timeout): (sym, mn, mx)
                for sym, mn, mx, out in tasks
            }
            for fut in as_completed(futures):
                sym, mn, mx, ok, msg = fut.result()
                done += 1
                status = "OK  " if ok else "FAIL"
                key = _combo_key(mn, mx)
                print(f"  {status} {sym:<8} {key}  [{done}/{total}]"
                      + (f"  {msg}" if not ok else ""))
    else:
        print("\n[parse-only] Re-parsing existing CSVs...\n")

    # ── Parse results ──────────────────────────────────────────────────────────
    print("\n" + "=" * 70)
    print(f"{'COMBO':<22} {'SYM':<8} {'TRADES':>6} {'WIN%':>6} {'PF':>6}  STATUS")
    print("=" * 70)

    # Collect per-combo aggregates
    combo_summary: dict[str, dict] = {}

    for min_rr, max_rr in combos:
        key = _combo_key(min_rr, max_rr)
        combo_dir = results_root / key
        sym_results = []

        for sym in SYMBOLS:
            out_file = combo_dir / f"{sym}.csv"
            parsed = _parse_csv(out_file)
            if parsed is None:
                print(f"  {key:<22} {sym:<8} {'—':>6} {'—':>6} {'—':>6}  NO DATA")
                continue

            pf_ok = "✓" if parsed["profit_factor"] >= 2.5 else "✗"
            print(f"  {key:<22} {sym:<8} {parsed['trades']:>6} "
                  f"{parsed['win_rate']:>5.1f}% {parsed['profit_factor']:>6.2f}  {pf_ok}")
            sym_results.append(parsed)

        if sym_results:
            avg_pf = sum(r["profit_factor"] for r in sym_results) / len(sym_results)
            avg_wr = sum(r["win_rate"] for r in sym_results) / len(sym_results)
            total_trades = sum(r["trades"] for r in sym_results)
            passing = sum(1 for r in sym_results if r["profit_factor"] >= 2.5)
            combo_summary[key] = {
                "min_rr": min_rr,
                "max_rr": max_rr,
                "avg_pf": round(avg_pf, 2),
                "avg_wr": round(avg_wr, 1),
                "total_trades": total_trades,
                "pairs_passing": passing,
                "total_pairs": len(sym_results),
            }
        print()

    # ── Ranked summary ─────────────────────────────────────────────────────────
    print("=" * 70)
    print("RANKED BY: pairs passing PF≥2.5, then avg profit factor\n")
    print(f"  {'COMBO':<22} {'PAIRS✓':>7} {'AVG_PF':>7} {'AVG_WR':>7} {'TRADES':>7}")
    print("  " + "-" * 56)

    ranked = sorted(
        combo_summary.values(),
        key=lambda x: (x["pairs_passing"], x["avg_pf"]),
        reverse=True,
    )
    for r in ranked:
        key = _combo_key(r["min_rr"], r["max_rr"])
        print(f"  {key:<22} {r['pairs_passing']}/{r['total_pairs']:>4}   "
              f"{r['avg_pf']:>6.2f}  {r['avg_wr']:>6.1f}%  {r['total_trades']:>6}")

    print()
    if ranked:
        best = ranked[0]
        print(f"  BEST → min_rr={best['min_rr']}  max_rr={best['max_rr']}"
              f"  ({best['pairs_passing']}/{best['total_pairs']} pairs ≥ 2.5 PF,"
              f" avg PF {best['avg_pf']})\n")


if __name__ == "__main__":
    main()
