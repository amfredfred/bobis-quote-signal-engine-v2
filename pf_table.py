"""
pf_table.py — cross-period PF comparison table per symbol × min_rr combo.

Usage:
    venv\Scripts\python pf_table.py
"""

from __future__ import annotations

import csv
from itertools import product
from pathlib import Path

PERIODS = {
    "2026 H1": Path("results/grid/fundecnext"),
    "2022 H1": Path("results/grid-2022/fundecnext"),
}

SYMBOLS = ["XAUUSD", "US100", "EURUSD", "GBPUSD", "USDJPY"]
MIN_RR_VALUES = [5.0, 7.0, 8.0, 10.0, 12.0, 14.0]
MAX_RR = 3.0

WIN_OUTCOMES  = {"WIN_FULL", "WIN_PARTIAL", "WIN", "TP1_HIT", "TP2_HIT"}
LOSS_OUTCOMES = {"LOSS", "SL_HIT"}


def combo_key(min_rr: float, max_rr: float) -> str:
    return f"min{min_rr:.1f}_max{max_rr:.1f}"


def parse_pf(path: Path) -> float | None:
    if not path.exists():
        return None
    try:
        with open(path, newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
    except Exception:
        return None
    if not rows:
        return None
    wins  = [r for r in rows if r.get("outcome", "").upper() in WIN_OUTCOMES]
    loss  = [r for r in rows if r.get("outcome", "").upper() in LOSS_OUTCOMES]
    gw = sum(float(r["realized_rr"]) for r in wins)
    gl = sum(abs(float(r["realized_rr"])) for r in loss)
    return round(gw / gl, 2) if gl > 0 else None


def parse_trades(path: Path) -> int | None:
    if not path.exists():
        return None
    try:
        with open(path, newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        return len(rows)
    except Exception:
        return None


def main() -> None:
    col_w = 10

    for period_name, grid_dir in PERIODS.items():
        print(f"\n{'=' * 70}")
        print(f"  PF TABLE — {period_name}")
        print(f"{'=' * 70}")

        # Header
        header = f"  {'Symbol':<8}" + "".join(f"  min{int(m):>2}" .ljust(col_w) for m in MIN_RR_VALUES)
        print(header)
        print("  " + "-" * (8 + col_w * len(MIN_RR_VALUES)))

        for sym in SYMBOLS:
            row = f"  {sym:<8}"
            for min_rr in MIN_RR_VALUES:
                key = combo_key(min_rr, MAX_RR)
                csv_path = grid_dir / key / f"{sym}.csv"
                pf = parse_pf(csv_path)
                if pf is None:
                    cell = "  —"
                else:
                    marker = "✓" if pf >= 2.0 else "✗"
                    cell = f"  {pf:.2f}{marker}"
                row += cell.ljust(col_w)
            print(row)

    # Trade count tables
    for period_name, grid_dir in PERIODS.items():
        trading_days = 116 if "2026" in period_name else 130
        print(f"\n{'=' * 70}")
        print(f"  TRADE COUNT TABLE — {period_name}  ({trading_days} trading days)")
        print(f"{'=' * 70}")

        header = f"  {'Symbol':<8}" + "".join(f"  min{int(m):>2}".ljust(col_w) for m in MIN_RR_VALUES)
        print(header)
        print("  " + "-" * (8 + col_w * len(MIN_RR_VALUES)))

        totals = {m: 0 for m in MIN_RR_VALUES}
        for sym in SYMBOLS:
            row = f"  {sym:<8}"
            for min_rr in MIN_RR_VALUES:
                key = combo_key(min_rr, MAX_RR)
                n = parse_trades(grid_dir / key / f"{sym}.csv")
                cell = f"  {n}" if n is not None else "  —"
                row += cell.ljust(col_w)
                if n:
                    totals[min_rr] += n
            print(row)

        # Portfolio total row
        total_row = f"  {'TOTAL':<8}"
        for min_rr in MIN_RR_VALUES:
            avg = totals[min_rr] / trading_days
            total_row += f"  {totals[min_rr]}({avg:.1f}/d)".ljust(col_w + 2)
        print("  " + "-" * (8 + col_w * len(MIN_RR_VALUES)))
        print(total_row)

    # Cross-period consistency summary
    print(f"\n{'=' * 70}")
    print("  CONSISTENCY — both periods ≥ 2.0 PF  (min_rr=8, max_rr=3.0)")
    print(f"{'=' * 70}")
    print(f"  {'Symbol':<8} {'2026 PF':>8} {'2022 PF':>8}  {'Consistent?':>12}")
    print("  " + "-" * 42)

    for sym in SYMBOLS:
        pf_2026 = parse_pf(PERIODS["2026 H1"] / combo_key(8.0, MAX_RR) / f"{sym}.csv")
        pf_2022 = parse_pf(PERIODS["2022 H1"] / combo_key(8.0, MAX_RR) / f"{sym}.csv")
        both_ok = (pf_2026 or 0) >= 2.0 and (pf_2022 or 0) >= 2.0
        flag = "✓  YES" if both_ok else "✗  NO"
        p26 = f"{pf_2026:.2f}" if pf_2026 else "—"
        p22 = f"{pf_2022:.2f}" if pf_2022 else "—"
        print(f"  {sym:<8} {p26:>8} {p22:>8}  {flag:>12}")

    print()


if __name__ == "__main__":
    main()
