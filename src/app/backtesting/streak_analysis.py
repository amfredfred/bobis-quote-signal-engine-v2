"""Losing-streak analysis for RBA 42-month backtest.

Answers three questions:
1. How bad are the losing streaks (per symbol and as a combined portfolio)?
2. Are streaks random (i.i.d. coin-flip math) or clustered in time (regime-driven)?
3. What do candidate live mitigation rules actually do to expectancy and drawdown?

All analysis is in R-units (realized_rr) with flat 1R risk per trade, so results
are independent of the backtest's compounding balance accounting.
"""
import heapq
from pathlib import Path

import numpy as np
import pandas as pd

DIR = Path(__file__).parent
SYMBOLS = ["US500", "US100", "XAUUSD"]
EPS = 1e-9
MC_ITER = 4000
RNG = np.random.default_rng(42)

# ---------------------------------------------------------------- load
frames = []
for s in SYMBOLS:
    df = pd.read_csv(DIR / f"{s}.csv")
    df["symbol"] = s
    frames.append(df)
allt = pd.concat(frames, ignore_index=True)
for col in ("entry_dt", "close_dt"):
    allt[col] = pd.to_datetime(allt[col])
allt["R"] = allt["realized_rr"].astype(float)
allt["res"] = np.where(allt["R"] < -EPS, "L", np.where(allt["R"] > EPS, "W", "B"))
allt = allt.sort_values("close_dt").reset_index(drop=True)

print("=" * 78)
print("DATASET")
print("=" * 78)
print(f"period: {allt.entry_dt.min()} .. {allt.close_dt.max()}")
print(f"trades: {len(allt)} total | " + " | ".join(
    f"{s}: {(allt.symbol == s).sum()}" for s in SYMBOLS))
print("\noutcome value counts:")
print(allt.outcome.value_counts().to_string())
print("\nrealized_rr distribution:")
print(allt.R.describe().round(3).to_string())
neq = (allt.realized_rr - allt.executed_rr).abs() > 1e-6
print(f"rows where executed_rr != realized_rr: {neq.sum()}")

# ---------------------------------------------------------------- helpers
def loss_streak_lengths(res_arr):
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

def max_run(bool_arr):
    a = np.concatenate(([False], bool_arr, [False]))
    d = np.diff(a.astype(np.int8))
    starts = np.where(d == 1)[0]
    if not len(starts):
        return 0
    ends = np.where(d == -1)[0]
    return int((ends - starts).max())

def max_dd(r_arr):
    eq = np.cumsum(r_arr)
    peak = np.maximum.accumulate(eq)
    return float((eq - peak).min())

def mc_null(res_arr, r_arr, n_iter=MC_ITER):
    """Shuffle outcome order: null distribution of max streak & max DD if i.i.d."""
    is_loss = (res_arr == "L").to_numpy() if hasattr(res_arr, "to_numpy") else np.asarray(res_arr) == "L"
    r = np.asarray(r_arr, dtype=float)
    idx = np.arange(len(r))
    streak_null = np.empty(n_iter)
    dd_null = np.empty(n_iter)
    for i in range(n_iter):
        RNG.shuffle(idx)
        streak_null[i] = max_run(is_loss[idx])
        dd_null[i] = max_dd(r[idx])
    return streak_null, dd_null

def streak_report(name, sub):
    res = sub["res"].to_numpy()
    r = sub["R"].to_numpy()
    n = len(sub)
    wr = (res == "W").mean()
    lr = (res == "L").mean()
    br = (res == "B").mean()
    streaks = loss_streak_lengths(res)
    obs_max = max(streaks) if streaks else 0
    obs_dd = max_dd(r)
    streak_null, dd_null = mc_null(sub["res"], r)
    p_streak = float((streak_null >= obs_max).mean())
    p_dd = float((dd_null <= obs_dd).mean())
    hist = pd.Series(streaks).value_counts().sort_index()
    print(f"\n--- {name} ---")
    print(f"n={n}  win={wr:.1%}  loss={lr:.1%}  be={br:.1%}  "
          f"totalR={r.sum():+.1f}  avgR/trade={r.mean():+.4f}")
    print("loss-streak length -> count: "
          + ", ".join(f"{k}:{v}" for k, v in hist.items()))
    print(f"max consecutive losses: {obs_max}   "
          f"[i.i.d. null: median {np.median(streak_null):.0f}, "
          f"p95 {np.percentile(streak_null, 95):.0f}, p(>=obs)={p_streak:.3f}]")
    print(f"max drawdown: {obs_dd:+.1f}R        "
          f"[i.i.d. null: median {np.median(dd_null):+.1f}R, "
          f"p5 {np.percentile(dd_null, 5):+.1f}R, p(<=obs)={p_dd:.3f}]")
    return obs_max, obs_dd

# ---------------------------------------------------------------- per symbol + combined
print("\n" + "=" * 78)
print("STREAKS & DRAWDOWN vs I.I.D. NULL  (shuffle test, %d iters)" % MC_ITER)
print("'p(>=obs)' small => streaks LONGER than luck => clustering/regime")
print("=" * 78)
for s in SYMBOLS:
    streak_report(s, allt[allt.symbol == s])
streak_report("COMBINED (by close time)", allt)

# ---------------------------------------------------------------- conditional dependence
print("\n" + "=" * 78)
print("OUTCOME DEPENDENCE — next trade conditioned on current loss run (combined)")
print("=" * 78)
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
    print(f"after >={k} consecutive losses: n={mask.sum():5d}  "
          f"P(loss)={ (res[mask] == 'L').mean():.1%}  avg R={r[mask].mean():+.4f}")

# ---------------------------------------------------------------- daily level
print("\n" + "=" * 78)
print("DAY LEVEL (combined portfolio, flat 1R)")
print("=" * 78)
allt["day"] = allt.close_dt.dt.date
daily = allt.groupby("day")["R"].sum()
ddays = pd.Series(daily.values, index=pd.to_datetime(daily.index))
print(f"trading days: {len(daily)}  avg trades/day: {len(allt)/len(daily):.1f}")
print(f"daily R: mean {daily.mean():+.2f}  p5 {daily.quantile(.05):+.2f}  "
      f"min {daily.min():+.2f}  max {daily.max():+.2f}")
print(f"losing days: {(daily < 0).mean():.1%}")
day_res = np.where(daily.values < -EPS, "L", np.where(daily.values > EPS, "W", "B"))
dstreaks = loss_streak_lengths(day_res)
print(f"max consecutive losing DAYS: {max(dstreaks)}")
eq = daily.cumsum()
peak = eq.cummax()
uw = eq - peak
print(f"max drawdown (daily eq): {uw.min():+.1f}R")
# longest underwater stretch
peak_dates = eq.index[eq >= peak - EPS]
gaps = np.diff([pd.Timestamp(d) for d in peak_dates])
if len(gaps):
    print(f"longest time between equity peaks: {max(gaps).days} days")
worst20 = pd.Series(r).rolling(20).sum().min()
worst50 = pd.Series(r).rolling(50).sum().min()
print(f"worst 20-trade window: {worst20:+.1f}R   worst 50-trade window: {worst50:+.1f}R")

monthly = allt.set_index("close_dt").groupby(pd.Grouper(freq="ME"))["R"].sum()
print(f"\nmonths: {len(monthly)}  losing months: {(monthly < 0).sum()}  "
      f"worst month: {monthly.min():+.1f}R  best: {monthly.max():+.1f}R")
print("monthly R:")
for d, v in monthly.items():
    bar = "#" * int(abs(v) / 4)
    print(f"  {d:%Y-%m} {v:+7.1f} {'-' if v < 0 else '+'}{bar}")

# ---------------------------------------------------------------- clustering by condition
print("\n" + "=" * 78)
print("WHERE LOSSES CONCENTRATE (combined; avg R per trade by bucket)")
print("=" * 78)
def bucket(name, key):
    g = allt.groupby(key, observed=True).agg(n=("R", "size"), avgR=("R", "mean"),
                                             lossrate=("res", lambda x: (x == "L").mean()))
    g = g[g.n >= 30]
    print(f"\n{name}:")
    print(g.round(3).to_string())

allt["hour"] = allt.entry_dt.dt.hour
allt["dow"] = allt.entry_dt.dt.day_name()
bucket("by entry hour", "hour")
bucket("by day of week", "dow")
bucket("by zone_attempt", "zone_attempt")
bucket("by pattern", "pattern")
allt["wick_b"] = pd.cut(pd.to_numeric(allt.wick_ratio, errors="coerce"),
                        [0, .2, .4, .6, .8, 1.01], include_lowest=True)
bucket("by wick_ratio", "wick_b")

# ---------------------------------------------------------------- cross-symbol
print("\n" + "=" * 78)
print("CROSS-SYMBOL CORRELATION & CONCURRENCY")
print("=" * 78)
piv = allt.pivot_table(index="day", columns="symbol", values="R", aggfunc="sum")
print("daily R correlation:")
print(piv.corr().round(3).to_string())
both_idx = piv[["US500", "US100"]].dropna()
both_neg = ((both_idx.US500 < 0) & (both_idx.US100 < 0)).mean()
print(f"\nP(US500 day<0 AND US100 day<0 | both traded): {both_neg:.1%}")
events = sorted([(t, +1) for t in allt.entry_dt] + [(t, -1) for t in allt.close_dt])
cur = mx = 0
for _, dlt in events:
    cur += dlt
    mx = max(mx, cur)
print(f"max concurrent open trades (all symbols): {mx}")

# ---------------------------------------------------------------- mitigation sims
print("\n" + "=" * 78)
print("MITIGATION RULE SIMULATIONS (flat 1R baseline, decisions use only")
print("trades CLOSED before each new entry — live-realistic)")
print("=" * 78)
ent = allt.sort_values("entry_dt").reset_index(drop=True)
e_dt = ent.entry_dt.to_numpy()
c_dt = ent.close_dt.to_numpy()
e_day = ent.entry_dt.dt.date.to_numpy()
R = ent.R.to_numpy()
RES = ent.res.to_numpy()

def simulate(rule):
    """rule(state) -> risk multiplier for next trade (0 = skip).
    state: dict(day_r, day_consec_losses, global_consec_losses)"""
    pending = []  # (close_dt, R, res, day, mult)
    day_r = {}
    day_cl = {}
    g_cl = 0
    taken_r = []
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
        mult = rule({"day_r": day_r.get(day, 0.0),
                     "day_cl": day_cl.get(day, 0),
                     "g_cl": g_cl})
        if mult <= 0:
            skipped += 1
        heapq.heappush(pending, (c_dt[i], R[i], RES[i], day, mult))
        taken_r.append(R[i] * mult)
    tr = np.asarray(taken_r)
    traded = tr[np.abs(tr) > 0] if False else tr
    eqd = pd.Series(tr).groupby(pd.Series(e_day)).sum()
    return dict(totalR=tr.sum(), maxDD=max_dd(tr), skipped=skipped,
                worst_day=eqd.min(),
                worst_month=pd.Series(tr, index=pd.to_datetime(ent.entry_dt))
                .groupby(pd.Grouper(freq="ME")).sum().min())

rules = {
    "baseline (no rule)":            lambda s: 1.0,
    "daily stop at -2R":             lambda s: 0.0 if s["day_r"] <= -2 else 1.0,
    "daily stop at -3R":             lambda s: 0.0 if s["day_r"] <= -3 else 1.0,
    "daily stop at -4R":             lambda s: 0.0 if s["day_r"] <= -4 else 1.0,
    "stop day after 3 consec losses":lambda s: 0.0 if s["day_cl"] >= 3 else 1.0,
    "half risk after 2 consec losses":lambda s: 0.5 if s["g_cl"] >= 2 else 1.0,
    "half risk after 3 consec losses":lambda s: 0.5 if s["g_cl"] >= 3 else 1.0,
    "half risk in drawdown>5R":      None,  # handled below
}
del rules["half risk in drawdown>5R"]

print(f"{'rule':35s} {'totalR':>8s} {'maxDD':>8s} {'worstDay':>9s} "
      f"{'worstMonth':>11s} {'skipped':>8s}")
for name, fn in rules.items():
    m = simulate(fn)
    print(f"{name:35s} {m['totalR']:+8.1f} {m['maxDD']:+8.1f} "
          f"{m['worst_day']:+9.1f} {m['worst_month']:+11.1f} {m['skipped']:8d}")

# equity-curve-based: half risk while equity below peak by > 5R (uses closed trades only)
def sim_eq_filter(thresh, half=0.5):
    pending = []
    eq = peak_eq = 0.0
    taken = []
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
    return tr.sum(), max_dd(tr)

for th in (5, 8):
    tot, dd = sim_eq_filter(th)
    print(f"{'half risk while DD > %dR' % th:35s} {tot:+8.1f} {dd:+8.1f}")
print("\n(note: maxDD here is in R at FLAT risk; at f%% account risk per trade,")
print(" account DD ~= 1-(1-f)^|DD_R| with compounding, e.g. 25R DD @1% = ~22%)")
