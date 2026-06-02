# rba.py

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
    "XAUUSD",
    "US30",
    "US500",
    "US100",
    "JP225",
    "EURUSD",
]

FAILED_STATE_FILE = ".backtest_failed.json"


def _safe_filename(symbol: str) -> str:
    return symbol.replace("/", "")


def _csv_is_fresh(path: Path, max_age_hours: float) -> bool:
    return path.exists() and (time.time() - path.stat().st_mtime) < max_age_hours * 3600


def run_one(symbol, output_dir, extra_args, cache_dir, timeout):
    out_file = output_dir / f"{_safe_filename(symbol)}.csv"

    cmd = [
        sys.executable,
        "-m",
        "src.app.backtesting.backtest",  # FIXED
        "--symbol",
        symbol,
        "--output",
        str(out_file),
        *extra_args,
    ]

    if cache_dir:
        cmd += ["--cache-dir", str(cache_dir)]

    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"

    t0 = time.time()

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
        elapsed = time.time() - t0
        return symbol, result.returncode == 0, elapsed, result.stdout + result.stderr

    except subprocess.TimeoutExpired:
        return symbol, False, time.time() - t0, f"TIMEOUT after {timeout}s"

    except Exception as e:
        return symbol, False, time.time() - t0, str(e)


def run_with_retry(symbol, output_dir, extra_args, cache_dir, timeout, retries):
    for attempt in range(retries + 1):
        symbol, success, elapsed, output = run_one(
            symbol, output_dir, extra_args, cache_dir, timeout
        )
        if success:
            return symbol, success, elapsed, output

        if attempt < retries:
            time.sleep(2**attempt)

    return symbol, False, elapsed, output


def progress(done, total):
    pct = (done / total) * 100
    return f"{done}/{total} ({pct:.1f}%)"


def main():
    p = argparse.ArgumentParser()

    p.add_argument("--workers", type=int, default=min(6, os.cpu_count() or 4))  # FIXED
    p.add_argument("--output-dir", default="results/RBA")
    p.add_argument("--symbols", nargs="+")
    p.add_argument("--resume", action="store_true")
    p.add_argument("--resume-max-age", type=float, default=24.0)
    p.add_argument("--only-failed", action="store_true")
    p.add_argument("--retry", type=int, default=1)
    p.add_argument("--timeout", type=int, default=84600)  # FIXED (30 min)
    p.add_argument("--cache-dir")

    # passthrough
    p.add_argument("--from-date")
    p.add_argument("--to-date")
    p.add_argument("--start-balance", dest="start_balance", type=float, default=None)
    p.add_argument("--risk-percent", dest="risk_percent", type=float, default=None)

    args = p.parse_args()

    extra = []
    if args.from_date:
        extra += ["--from-date", args.from_date]
    if args.to_date:
        extra += ["--to-date", args.to_date]
    if args.start_balance is not None:
        extra += ["--start-balance", str(args.start_balance)]
    if args.risk_percent is not None:
        extra += ["--risk-percent", str(args.risk_percent)]

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    cache_dir = Path(args.cache_dir) if args.cache_dir else None
    if cache_dir:
        cache_dir.mkdir(parents=True, exist_ok=True)

    symbols = args.symbols or ALL_SYMBOLS

    if args.only_failed:
        state = Path(FAILED_STATE_FILE)
        if state.exists():
            failed_last = json.loads(state.read_text())
            symbols = [s for s in symbols if s in failed_last]

    if args.resume:
        symbols = [
            s
            for s in symbols
            if not _csv_is_fresh(
                output_dir / f"{_safe_filename(s)}.csv", args.resume_max_age
            )
        ]

    if not symbols:
        print("Nothing to run.")
        return

    print(f"\nRunning {len(symbols)} symbols with {args.workers} workers\n")

    passed, failed = [], []
    times = []
    total = len(symbols)
    done = 0

    start = time.time()

    try:
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = {
                pool.submit(
                    run_with_retry,
                    s,
                    output_dir,
                    extra,
                    cache_dir,
                    args.timeout,
                    args.retry,
                ): s
                for s in symbols
            }

            for future in as_completed(futures):
                symbol, success, elapsed, output = future.result()

                done += 1
                times.append(elapsed)

                avg = sum(times) / len(times)
                eta = (total - done) * avg / args.workers

                print(
                    f"{'OK' if success else 'FAIL'} "
                    f"{symbol:<8} {elapsed:5.1f}s | {progress(done,total)} | ETA {eta:.0f}s"
                )

                if success:
                    passed.append(symbol)
                else:
                    failed.append(symbol)
                    print(output.splitlines()[-3:])

    except KeyboardInterrupt:
        print("\nInterrupted. Shutting down...")
        return

    if failed:
        Path(FAILED_STATE_FILE).write_text(json.dumps(failed))
    else:
        Path(FAILED_STATE_FILE).unlink(missing_ok=True)

    print("\nRESULT")
    print("Passed:", len(passed))
    print("Failed:", len(failed))
    print("Time  :", round(time.time() - start, 1), "s")


if __name__ == "__main__":
    main()
