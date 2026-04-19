"""
run_backtest_all.py — run backtests for all symbols in parallel.

Speed improvements vs original:
  • --resume        skip symbols that already have a fresh CSV (huge on reruns)
  • --cache-dir     fetched OHLCV data is saved locally; reused on next run
  • --retry N       auto-retry failed symbols up to N times (handles flaky API)
  • Fixed timeout   was 30000s (~8 hrs); now correctly 1800s (30 min)
  • Progress bar    live ETA so you know when it'll finish
  • --only-failed   re-run only the symbols that failed last time

Usage:
    python run_backtest_all.py
    python run_backtest_all.py --workers 8
    python run_backtest_all.py --resume                   # skip already-done CSVs
    python run_backtest_all.py --only-failed              # re-run last failures only
    python run_backtest_all.py --output-dir results/run_01
    python run_backtest_all.py --symbols EURUSD GBPUSD XAUUSD
    python run_backtest_all.py --no-session-filter --no-trend-filter
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ALL_SYMBOLS = [
    "XAUUSD",  # Gold
    "EURUSD",  # Major
    "GBPUSD",  # Major
    "USDJPY",  # Major
    "USDCHF",  # Major
    "AUDUSD",  # Major
    "USDCAD",  # Major
    "NZDUSD",  # Major
    "US500",  # Nasdaq
    "US30",  # Dow
    "US100",  # NASDAQ-100
    "BTCUSD",  # Only if you can handle crypto risk
]

# ── Terminal colours ───────────────────────────────────────────────────────────
GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
CYAN = "\033[96m"
BOLD = "\033[1m"
DIM = "\033[2m"
RESET = "\033[0m"

# ── State file for --only-failed ───────────────────────────────────────────────
FAILED_STATE_FILE = ".backtest_failed.json"


def _safe_filename(symbol: str) -> str:
    return symbol.replace("/", "")


def _csv_is_fresh(path: Path, max_age_hours: float = 24.0) -> bool:
    """Return True if CSV exists and is newer than max_age_hours."""
    if not path.exists():
        return False
    age = time.time() - path.stat().st_mtime
    return age < max_age_hours * 3600


def run_one(
    symbol: str,
    output_dir: Path,
    extra_args: list[str],
    cache_dir: Path | None,
    timeout: int,
) -> tuple[str, bool, float, str]:
    """Run a single backtest subprocess. Returns (symbol, success, elapsed, output)."""
    out_file = output_dir / f"{_safe_filename(symbol)}.csv"

    cmd = [
        sys.executable,
        "-m",
        "app.backtesting.backtest",
        "--symbol",
        symbol,
        "--output",
        str(out_file),
        *extra_args,
    ]

    # Pass cache dir to backtest module if it supports --cache-dir
    if cache_dir is not None:
        cmd += ["--cache-dir", str(cache_dir)]

    # Force UTF-8 on Windows
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"

    t0 = time.time()
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=timeout,
            env=env,
        )
        elapsed = time.time() - t0
        output = result.stdout + result.stderr
        success = result.returncode == 0
        return symbol, success, elapsed, output
    except subprocess.TimeoutExpired:
        return symbol, False, time.time() - t0, f"TIMEOUT after {timeout}s"
    except Exception as e:
        return symbol, False, time.time() - t0, str(e)


def run_with_retry(
    symbol: str,
    output_dir: Path,
    extra_args: list[str],
    cache_dir: Path | None,
    timeout: int,
    retries: int,
) -> tuple[str, bool, float, str]:
    """Wrap run_one with automatic retry on failure."""
    for attempt in range(retries + 1):
        symbol, success, elapsed, output = run_one(
            symbol, output_dir, extra_args, cache_dir, timeout
        )
        if success:
            return symbol, success, elapsed, output
        if attempt < retries:
            wait = 2**attempt  # exponential backoff: 1s, 2s, 4s
            time.sleep(wait)
    return symbol, False, elapsed, output


def progress_bar(done: int, total: int, width: int = 30) -> str:
    filled = int(width * done / total)
    bar = "█" * filled + "░" * (width - filled)
    pct = 100 * done / total
    return f"[{bar}] {pct:5.1f}%  {done}/{total}"


def main() -> None:
    p = argparse.ArgumentParser(
        description="Run backtests for all symbols in parallel",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--workers", type=int, default=10, help="Parallel workers (default: 7)"
    )
    p.add_argument(
        "--output-dir", default="results/all", help="Directory for CSV results"
    )
    p.add_argument(
        "--symbols", nargs="+", default=None, help="Subset of symbols to run"
    )
    p.add_argument(
        "--resume", action="store_true", help="Skip symbols with a fresh CSV already"
    )
    p.add_argument(
        "--resume-max-age",
        type=float,
        default=24.0,
        help="Max CSV age in hours to consider fresh (default: 24)",
    )
    p.add_argument(
        "--only-failed",
        action="store_true",
        help="Re-run only symbols that failed last time",
    )
    p.add_argument(
        "--retry",
        type=int,
        default=1,
        help="Auto-retry failed symbols N times (default: 1)",
    )
    p.add_argument(
        "--timeout",
        type=int,
        default=100000,
        help="Per-symbol timeout in seconds (default: 60000 = 16hmin)",
    )
    p.add_argument(
        "--cache-dir", default=None, help="Directory to cache fetched OHLCV data"
    )

    # Pass-through flags forwarded directly to backtest.py
    p.add_argument("--no-session-filter", action="store_true")
    p.add_argument("--no-trend-filter", action="store_true")
    p.add_argument("--no-breakeven", action="store_true")
    p.add_argument("--no-invalidation", action="store_true")
    p.add_argument("--min-rr", type=float, default=None)
    p.add_argument("--max-rr", type=float, default=None)
    p.add_argument("--max-wick", type=float, default=None)
    p.add_argument("--htf-lookback", type=int, default=None)
    p.add_argument(
        "--from-date",
        dest="from_date",
        metavar="YYYY-MM-DD",
        default=None,
        help="Backtest start date in UTC, e.g. 2020-01-01 (forwarded to backtest.py)",
    )
    p.add_argument(
        "--to-date",
        dest="to_date",
        metavar="YYYY-MM-DD",
        default=None,
        help="Backtest end date in UTC, e.g. 2024-12-31 (forwarded to backtest.py)",
    )

    args = p.parse_args()

    # ── Build extra args to forward ────────────────────────────────────────────
    extra: list[str] = []
    if args.no_session_filter:
        extra.append("--no-session-filter")
    if args.no_trend_filter:
        extra.append("--no-trend-filter")
    if args.no_breakeven:
        extra.append("--no-breakeven")
    if args.no_invalidation:
        extra.append("--no-invalidation")
    if args.min_rr is not None:
        extra += ["--min-rr", str(args.min_rr)]
    if args.max_rr is not None:
        extra += ["--max-rr", str(args.max_rr)]
    if args.max_wick is not None:
        extra += ["--max-wick", str(args.max_wick)]
    if args.htf_lookback is not None:
        extra += ["--htf-lookback", str(args.htf_lookback)]
    if args.from_date is not None:
        extra += ["--from-date", args.from_date]
    if args.to_date is not None:
        extra += ["--to-date", args.to_date]

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    cache_dir = Path(args.cache_dir) if args.cache_dir else None
    if cache_dir:
        cache_dir.mkdir(parents=True, exist_ok=True)

    # ── Symbol selection ───────────────────────────────────────────────────────
    symbols = args.symbols or ALL_SYMBOLS

    if args.only_failed:
        state_path = Path(FAILED_STATE_FILE)
        if state_path.exists():
            failed_last = json.loads(state_path.read_text())
            symbols = [s for s in symbols if s in failed_last]
            print(
                f"{YELLOW}  --only-failed: re-running {len(symbols)} symbols from last failure{RESET}"
            )
        else:
            print(f"{YELLOW}  --only-failed: no state file found, running all{RESET}")

    if args.resume:
        before = len(symbols)
        symbols = [
            s
            for s in symbols
            if not _csv_is_fresh(
                output_dir / f"{_safe_filename(s)}.csv",
                max_age_hours=args.resume_max_age,
            )
        ]
        skipped = before - len(symbols)
        if skipped:
            print(
                f"{GREEN}  --resume: skipping {skipped} already-complete symbols{RESET}"
            )

    if not symbols:
        print(f"{GREEN}  All symbols already complete. Nothing to do.{RESET}")
        sys.exit(0)

    # ── Header ─────────────────────────────────────────────────────────────────
    print(f"\n{BOLD}{'='*64}{RESET}")
    print(
        f"{BOLD}  BATCH BACKTEST  ·  {len(symbols)} symbols  ·  {args.workers} workers{RESET}"
    )
    print(f"  Output  → {output_dir}/")
    if cache_dir:
        print(f"  Cache   → {cache_dir}/  (data reused across runs)")
    if extra:
        print(f"  Flags   → {' '.join(extra)}")
    print(f"  Timeout → {args.timeout}s per symbol  ·  Retry → {args.retry}x")
    print(f"{'='*64}\n")

    t_start = time.time()
    passed = []
    failed = []
    done = 0
    total = len(symbols)
    times: list[float] = []

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {
            pool.submit(
                run_with_retry,
                sym,
                output_dir,
                extra,
                cache_dir,
                args.timeout,
                args.retry,
            ): sym
            for sym in symbols
        }
        for future in as_completed(futures):
            symbol, success, elapsed, output = future.result()
            done += 1
            times.append(elapsed)

            avg = sum(times) / len(times)
            remaining = (total - done) * avg / args.workers
            eta = f"ETA ~{remaining:.0f}s" if done < total else "done"
            bar = progress_bar(done, total)

            status = f"{GREEN}✓{RESET}" if success else f"{RED}✗{RESET}"
            print(
                f"  {status} {CYAN}{symbol:<10}{RESET}  {elapsed:5.1f}s  {DIM}{bar}  {eta}{RESET}"
            )

            if not success:
                tail = [l for l in output.strip().splitlines() if l.strip()][-4:]
                for line in tail:
                    print(f"         {DIM}{line}{RESET}")
                failed.append(symbol)
            else:
                passed.append(symbol)

    # ── Persist failed list for --only-failed next time ────────────────────────
    state_path = Path(FAILED_STATE_FILE)
    if failed:
        state_path.write_text(json.dumps(failed, indent=2))
    elif state_path.exists():
        state_path.unlink()  # clean up if all passed

    # ── Summary ────────────────────────────────────────────────────────────────
    elapsed_total = time.time() - t_start
    print(f"\n{BOLD}{'='*64}{RESET}")
    print(f"{BOLD}  RESULTS{RESET}")
    print(f"  {GREEN}Passed : {len(passed)}{RESET}")
    print(f"  {RED}Failed : {len(failed)}{RESET}")
    if failed:
        print(f"\n  {RED}Failed symbols:{RESET}")
        for s in failed:
            print(f"    • {s}")
        print(f"\n  {YELLOW}Tip: re-run with --only-failed to retry just these{RESET}")
    print(f"\n  Total time : {elapsed_total:.1f}s")
    print(f"  CSVs saved → {output_dir}/")
    print(f"{BOLD}{'='*64}{RESET}\n")

    sys.exit(0 if not failed else 1)


if __name__ == "__main__":
    main()
