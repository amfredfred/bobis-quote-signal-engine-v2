"""
Losing-streak analysis for backtest results.

Answers three questions:
  1. How bad are the losing streaks (per symbol and combined)?
  2. Are streaks random (i.i.d. coin-flip math) or clustered in time?
  3. What do candidate live mitigation rules do to expectancy and drawdown?

All analysis is in R-units (realized_rr) with flat 1R risk per trade, so results
are independent of compounding balance accounting.

Usage — integrated (after a backtest run):
    report.print_streak_analysis()          # on a BacktestReport
      -- or --
    from app.backtesting.streak_analysis import run
    run(results)                            # list[BacktestResult]

Usage — CLI on saved CSV files:
    venv\\Scripts\\python -m src.app.backtesting.streak_analysis results/3yr-validation/XAUUSD.csv
    venv\\Scripts\\python -m src.app.backtesting.streak_analysis results/3yr-validation/*.csv
"""
from __future__ import annotations

import heapq
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np
import pandas as pd

if TYPE_CHECKING:
    from app.backtesting.backtest import BacktestResult

EPS = 1e-9
MC_ITER = 4_000
RNG = np.random.default_rng(42)


# ── Data ingestion ────────────────────────────────────────────────────────────


def _build_df(results: list[BacktestResult]) -> pd.DataFrame:
    """Convert a list of BacktestResult objects into the analysis DataFrame."""
    rows = []
    for r in results:
        s = r.signal
        entry_ms = s.triggered_at or 0
        close_ms = r.close_ts or entry_ms
        rows.append(
            {
                "symbol": s.symbol,
                "entry_dt": pd.Timestamp(entry_ms, unit="ms", tz="UTC"),
                "close_dt": pd.Timestamp(close_ms, unit="ms", tz="UTC"),
                "R": float(r.realized_rr),
                "outcome": r.outcome.value,
                "zone_attempt": getattr(s, "zone_attempt", None),
                "pattern": (
                    s.rejection_candle.pattern.value
                    if s.rejection_candle
                    else None
                ),
                "wick_ratio": (
                    round(s.rejection_candle.wick_ratio, 3)
                    if s.rejection_candle
                    else None
                ),
            }
        )
    df = pd.DataFrame(rows)
    df["res"] = np.where(df["R"] < -EPS, "L", np.where(df["R"] > EPS, "W", "B"))
    return df.sort_values("close_dt").reset_index(drop=True)


def _load_csv(path: Path) -> pd.DataFrame:
    """Load a saved backtest CSV into the analysis DataFrame."""
    df = pd.read_csv(path)
    for col in ("entry_dt", "close_dt"):
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], utc=True)
    df["R"] = df["realized_rr"].astype(float)
    df["res"] = np.where(df["R"] < -EPS, "L", np.where(df["R"] > EPS, "W", "B"))
    if "symbol" not in df.columns:
        df["symbol"] = path.stem
    return df.sort_values("close_dt").reset_index(drop=True)


# ── Core helpers ──────────────────────────────────────────────────────────────


def _loss_streak_lengths(res_arr) -> list[int]:
    out, cur = [], 0
    for r in res_arr:
        if r == "L":
            cur += 1
        elif cur:
            out.append(cur)
            cur = 0
    if cur:
        out.append(cur)
    return out


def _max_run(bool_arr) -> int:
    a = np.concatenate(([False], bool_arr, [False]))
    d = np.diff(a.astype(np.int8))
    starts = np.where(d == 1)[0]
    if not len(starts):
        return 0
    ends = np.where(d == -1)[0]
    return int((ends - starts).max())


def _max_dd(r_arr) -> float:
    eq = np.cumsum(r_arr)
    peak = np.maximum.accumulate(eq)
    return float((eq - peak).min())


def _mc_null(res_arr, r_arr, n_iter: int = MC_ITER):
    is_loss = (
        res_arr == "L"
    ).to_numpy() if hasattr(res_arr, "to_numpy") else np.asarray(res_arr) == "L"
    r = np.asarray(r_arr, dtype=float)
    idx = np.arange(len(r))
    streak_null = np.empty(n_iter)
    dd_null = np.empty(n_iter)
    for i in range(n_iter):
        RNG.shuffle(idx)
        streak_null[i] = _max_run(is_loss[idx])
        dd_null[i] = _max_dd(r[idx])
    return streak_null, dd_null


# ── Section printers ──────────────────────────────────────────────────────────


def _section(title: str) -> None:
    print(f"\n{'=' * 78}")
    print(title)
    print("=" * 78)


def _streak_report(name: str, sub: pd.DataFrame) -> tuple[int, float]:
    res = sub["res"].to_numpy()
    r = sub["R"].to_numpy()
    n = len(sub)
    wr = (res == "W").mean()
    lr = (res == "L").mean()
    br = (res == "B").mean()
    streaks = _loss_streak_lengths(res)
    obs_max = max(streaks) if streaks else 0
    obs_dd = _max_dd(r)
    streak_null, dd_null = _mc_null(sub["res"], r)
    p_streak = float((streak_null >= obs_max).mean())
    p_dd = float((dd_null <= obs_dd).mean())
    hist = pd.Series(streaks).value_counts().sort_index()
    print(f"\n--- {name} ---")
    print(
        f"n={n}  win={wr:.1%}  loss={lr:.1%}  be={br:.1%}  "
        f"totalR={r.sum():+.1f}  avgR/trade={r.mean():+.4f}"
    )
    print(
        "loss-streak length -> count: "
        + ", ".join(f"{k}:{v}" for k, v in hist.items())
    )
    print(
        f"max consecutive losses: {obs_max}   "
        f"[i.i.d. null: median {np.median(streak_null):.0f}, "
        f"p95 {np.percentile(streak_null, 95):.0f}, "
        f"p(>=obs)={p_streak:.3f}]"
    )
    print(
        f"max drawdown: {obs_dd:+.1f}R        "
        f"[i.i.d. null: median {np.median(dd_null):+.1f}R, "
        f"p5 {np.percentile(dd_null, 5):+.1f}R, "
        f"p(<=obs)={p_dd:.3f}]"
    )
    return obs_max, obs_dd


def _print_dataset(allt: pd.DataFrame) -> None:
    symbols = sorted(allt["symbol"].unique())
    _section("DATASET")
    print(f"period: {allt.entry_dt.min()} .. {allt.close_dt.max()}")
    print(
        f"trades: {len(allt)} total | "
        + " | ".join(f"{s}: {(allt.symbol == s).sum()}" for s in symbols)
    )
    print("\noutcome value counts:")
    if "outcome" in allt.columns:
        print(allt["outcome"].value_counts().to_string())
    print("\nrealized_rr (R) distribution:")
    print(allt["R"].describe().round(3).to_string())


def _print_streaks(allt: pd.DataFrame) -> None:
    symbols = sorted(allt["symbol"].unique())
    _section(
        "STREAKS & DRAWDOWN vs I.I.D. NULL  "
        f"(shuffle test, {MC_ITER} iters)\n"
        "'p(>=obs)' small => streaks LONGER than random => regime clustering"
    )
    for s in symbols:
        _streak_report(s, allt[allt.symbol == s])
    if len(symbols) > 1:
        _streak_report("COMBINED (by close time)", allt)


def _print_dependence(allt: pd.DataFrame) -> None:
    _section("OUTCOME DEPENDENCE — next trade conditioned on current loss run")
    res = allt["res"].to_numpy()
    r = allt["R"].to_numpy()
    base_lr = (res == "L").mean()
    base_avg = r.mean()
    print(f"unconditional: P(loss)={base_lr:.1%}, avg R={base_avg:+.4f}")
    run = 0
    runs_before = np.zeros(len(res), dtype=int)
    for i, x in enumerate(res):
        runs_before[i] = run
        if x == "L":
            run += 1
        elif x == "W":
            run = 0
    for k in range(1, 8):
        mask = runs_before >= k
        if mask.sum() < 30:
            break
        print(
            f"after >={k} consecutive losses: n={mask.sum():5d}  "
            f"P(loss)={(res[mask] == 'L').mean():.1%}  "
            f"avg R={r[mask].mean():+.4f}"
        )


def _print_day_level(allt: pd.DataFrame) -> None:
    _section("DAY LEVEL (combined portfolio, flat 1R)")
    df = allt.copy()
    df["day"] = df.close_dt.dt.date
    daily = df.groupby("day")["R"].sum()
    print(
        f"trading days: {len(daily)}  avg trades/day: {len(allt)/len(daily):.1f}"
    )
    print(
        f"daily R: mean {daily.mean():+.2f}  p5 {daily.quantile(.05):+.2f}  "
        f"min {daily.min():+.2f}  max {daily.max():+.2f}"
    )
    print(f"losing days: {(daily < 0).mean():.1%}")
    day_res = np.where(
        daily.values < -EPS, "L", np.where(daily.values > EPS, "W", "B")
    )
    dstreaks = _loss_streak_lengths(day_res)
    print(f"max consecutive losing DAYS: {max(dstreaks) if dstreaks else 0}")

    eq = daily.cumsum()
    peak = eq.cummax()
    uw = eq - peak
    print(f"max drawdown (daily equity): {uw.min():+.1f}R")

    peak_dates = eq.index[eq >= peak - EPS]
    gaps = np.diff([pd.Timestamp(str(d)) for d in peak_dates])
    if len(gaps):
        print(f"longest time between equity peaks: {max(gaps).days} days")

    r_arr = allt["R"].to_numpy()
    worst20 = pd.Series(r_arr).rolling(20).sum().min()
    worst50 = pd.Series(r_arr).rolling(50).sum().min()
    print(f"worst 20-trade window: {worst20:+.1f}R   worst 50-trade window: {worst50:+.1f}R")

    monthly = (
        allt.set_index("close_dt").groupby(pd.Grouper(freq="ME"))["R"].sum()
    )
    print(
        f"\nmonths: {len(monthly)}  "
        f"losing months: {(monthly < 0).sum()}  "
        f"worst month: {monthly.min():+.1f}R  "
        f"best: {monthly.max():+.1f}R"
    )
    print("monthly R:")
    for d, v in monthly.items():
        bar = "#" * int(abs(v) / 4)
        sign = "-" if v < 0 else "+"
        print(f"  {d:%Y-%m} {v:+7.1f} {sign}{bar}")


def _print_concentration(allt: pd.DataFrame) -> None:
    _section("WHERE LOSSES CONCENTRATE (avg R per trade by bucket)")

    def bucket(name: str, key: str) -> None:
        if key not in allt.columns:
            return
        col = allt[key]
        if col.dtype == object or str(col.dtype).startswith("category"):
            g = allt.groupby(key, observed=True)
        else:
            g = allt.groupby(key, observed=True)
        agg = g.agg(
            n=("R", "size"),
            avgR=("R", "mean"),
            lossrate=("res", lambda x: (x == "L").mean()),
        )
        agg = agg[agg.n >= 20]
        if agg.empty:
            return
        print(f"\n{name}:")
        print(agg.round(3).to_string())

    df = allt.copy()
    df["hour"] = df.entry_dt.dt.hour
    df["dow"] = df.entry_dt.dt.day_name()

    bucket("by entry hour (UTC)", "hour")
    bucket("by day of week", "dow")

    if "zone_attempt" in df.columns and df["zone_attempt"].notna().any():
        bucket("by zone_attempt", "zone_attempt")
    if "pattern" in df.columns and df["pattern"].notna().any():
        bucket("by pattern", "pattern")
    if "wick_ratio" in df.columns and df["wick_ratio"].notna().any():
        df["wick_b"] = pd.cut(
            pd.to_numeric(df["wick_ratio"], errors="coerce"),
            [0, 0.2, 0.4, 0.6, 0.8, 1.01],
            include_lowest=True,
        )
        bucket("by wick_ratio", "wick_b")


def _print_cross_symbol(allt: pd.DataFrame) -> None:
    symbols = sorted(allt["symbol"].unique())
    if len(symbols) < 2:
        return
    _section("CROSS-SYMBOL CORRELATION")
    df = allt.copy()
    df["day"] = df.close_dt.dt.date
    piv = df.pivot_table(index="day", columns="symbol", values="R", aggfunc="sum")
    print("daily R correlation:")
    print(piv.corr().round(3).to_string())

    # P(both down) for each pair
    sym_pairs = [
        (symbols[i], symbols[j])
        for i in range(len(symbols))
        for j in range(i + 1, len(symbols))
    ]
    for s1, s2 in sym_pairs:
        sub = piv[[s1, s2]].dropna()
        if len(sub) < 10:
            continue
        both_neg = ((sub[s1] < 0) & (sub[s2] < 0)).mean()
        print(f"P({s1} day<0 AND {s2} day<0 | both traded): {both_neg:.1%}")


def _print_mitigation(allt: pd.DataFrame) -> None:
    _section(
        "MITIGATION RULE SIMULATIONS (flat 1R baseline; decisions use only\n"
        "trades CLOSED before each new entry — live-realistic)"
    )
    ent = allt.sort_values("entry_dt").reset_index(drop=True)
    e_dt = ent.entry_dt.to_numpy()
    c_dt = ent.close_dt.to_numpy()
    e_day = ent.entry_dt.dt.date.to_numpy()
    R = ent["R"].to_numpy()
    RES = ent["res"].to_numpy()

    def simulate(rule):
        pending: list = []
        day_r: dict = {}
        day_cl: dict = {}
        g_cl = 0
        taken_r: list = []
        skipped = 0
        for i in range(len(ent)):
            now, day = e_dt[i], e_day[i]
            while pending and pending[0][0] <= now:
                _, pr, pres, pday, pmult = heapq.heappop(pending)
                if pmult > 0:
                    day_r[pday] = day_r.get(pday, 0.0) + pr * pmult
                    if pres == "L":
                        day_cl[pday] = day_cl.get(pday, 0) + 1
                        g_cl += 1
                    elif pres == "W":
                        day_cl[pday] = 0
                        g_cl = 0
            mult = rule(
                {
                    "day_r": day_r.get(day, 0.0),
                    "day_cl": day_cl.get(day, 0),
                    "g_cl": g_cl,
                }
            )
            if mult <= 0:
                skipped += 1
            heapq.heappush(pending, (c_dt[i], R[i], RES[i], day, mult))
            taken_r.append(R[i] * mult)
        tr = np.asarray(taken_r)
        eqd = pd.Series(tr).groupby(pd.Series(e_day)).sum()
        monthly_r = (
            pd.Series(tr, index=pd.to_datetime(ent.entry_dt))
            .groupby(pd.Grouper(freq="ME"))
            .sum()
        )
        return dict(
            totalR=tr.sum(),
            maxDD=_max_dd(tr),
            skipped=skipped,
            worst_day=eqd.min(),
            worst_month=monthly_r.min(),
        )

    def sim_eq_filter(thresh: float, half: float = 0.5):
        pending: list = []
        eq = peak_eq = 0.0
        taken: list = []
        for i in range(len(ent)):
            now = e_dt[i]
            while pending and pending[0][0] <= now:
                _, pr, pmult = heapq.heappop(pending)
                eq += pr * pmult
                peak_eq = max(peak_eq, eq)
            mult = half if (peak_eq - eq) > thresh else 1.0
            heapq.heappush(pending, (c_dt[i], R[i], mult))
            taken.append(R[i] * mult)
        tr = np.asarray(taken)
        return tr.sum(), _max_dd(tr)

    rules: dict = {
        "baseline (no rule)":              lambda s: 1.0,
        "daily stop at -2R":               lambda s: 0.0 if s["day_r"] <= -2 else 1.0,
        "daily stop at -3R":               lambda s: 0.0 if s["day_r"] <= -3 else 1.0,
        "daily stop at -4R":               lambda s: 0.0 if s["day_r"] <= -4 else 1.0,
        "stop day after 3 consec losses":  lambda s: 0.0 if s["day_cl"] >= 3 else 1.0,
        "half risk after 2 consec losses": lambda s: 0.5 if s["g_cl"] >= 2 else 1.0,
        "half risk after 3 consec losses": lambda s: 0.5 if s["g_cl"] >= 3 else 1.0,
    }

    print(
        f"{'rule':38s} {'totalR':>8s} {'maxDD':>8s} "
        f"{'worstDay':>9s} {'worstMonth':>11s} {'skipped':>8s}"
    )
    print("-" * 88)
    for name, fn in rules.items():
        m = simulate(fn)
        print(
            f"{name:38s} {m['totalR']:+8.1f} {m['maxDD']:+8.1f} "
            f"{m['worst_day']:+9.1f} {m['worst_month']:+11.1f} {m['skipped']:8d}"
        )

    for th in (5, 8):
        tot, dd = sim_eq_filter(th)
        label = f"half risk while DD > {th}R"
        print(f"{label:38s} {tot:+8.1f} {dd:+8.1f}")

    print(
        "\n(note: maxDD is in R at flat 1R risk; at f% account risk per trade,\n"
        " account DD ~= 1-(1-f)^|DD_R| with compounding — e.g. 25R DD @1% = ~22%)"
    )


# ── Public entry point ────────────────────────────────────────────────────────


def run(results: list | pd.DataFrame, symbol: str | None = None) -> None:
    """
    Run the full streak analysis.

    Accepts either:
      - list[BacktestResult]  — from a live backtest run
      - pd.DataFrame          — pre-built (internal / CLI use)
    """
    if isinstance(results, pd.DataFrame):
        allt = results
    else:
        allt = _build_df(results)

    if symbol and "symbol" not in allt.columns:
        allt = allt.copy()
        allt["symbol"] = symbol

    _print_dataset(allt)
    _print_streaks(allt)
    _print_dependence(allt)
    _print_day_level(allt)
    _print_concentration(allt)
    _print_cross_symbol(allt)
    _print_mitigation(allt)


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser(
        description="Streak analysis on saved backtest CSV files"
    )
    p.add_argument(
        "csvs",
        nargs="+",
        metavar="CSV",
        help="One or more backtest CSV paths (e.g. results/3yr-validation/XAUUSD.csv)",
    )
    args = p.parse_args()

    paths: list[Path] = []
    for path_str in args.csvs:
        path = Path(path_str)
        if path.is_dir():
            paths.extend(sorted(path.glob("*.csv")))
        elif path.exists():
            paths.append(path)
        else:
            print(f"ERROR: path not found: {path}", file=sys.stderr)
            sys.exit(1)

    if not paths:
        print("ERROR: no CSV files found", file=sys.stderr)
        sys.exit(1)

    frames: list[pd.DataFrame] = []
    for path in paths:
        frames.append(_load_csv(path))

    combined = (
        pd.concat(frames, ignore_index=True)
        .sort_values("close_dt")
        .reset_index(drop=True)
    )
    run(combined)
