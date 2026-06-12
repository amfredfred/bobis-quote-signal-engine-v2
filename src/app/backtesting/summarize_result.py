"""
summarise_results.py — summarise all backtest CSVs + generate HTML report.

Usage:
    python summarise_results.py                          # scans results/
    python summarise_results.py results/run_01           # specific folder
    python summarise_results.py results/run_01 --sort r  # sort by total R
    python summarise_results.py results/ --no-html       # terminal only
    python summarise_results.py results/ --no-browser    # html but don't open
"""

import csv
import json
import math
import sys
import webbrowser
from collections import defaultdict
from datetime import datetime
from pathlib import Path

# ── Args ───────────────────────────────────────────────────────────────────────
args = sys.argv[1:]
sort_by = "pair"
results_dir = Path("results")
open_browser = True
gen_html = True

i = 0
while i < len(args):
    if args[i] == "--sort" and i + 1 < len(args):
        sort_by = args[i + 1]
        i += 2
    elif args[i] == "--no-browser":
        open_browser = False
        i += 1
    elif args[i] == "--no-html":
        gen_html = False
        i += 1
    else:
        results_dir = Path(args[i])
        i += 1

# ── Discover CSVs ──────────────────────────────────────────────────────────────
csv_files = sorted(results_dir.rglob("*.csv"))
if not csv_files:
    print(f"\n  No CSV files found in {results_dir}/\n")
    sys.exit(0)

# ── Per-pair stats ─────────────────────────────────────────────────────────────
rows_by_pair: list[dict] = []

TRADE_COLUMNS = [
    "id", "symbol", "direction", "entry_dt", "close_dt",
    "entry", "sl", "tp1", "tp2", "rr", "outcome", "realized_rr",
    "hit_entry_after_tp1", "htf_interval", "ltf_interval", "pattern",
    "wick_ratio", "htf_high", "htf_low", "tp_level", "ltf_high", "ltf_low",
    "balance_before", "risk_amount", "pnl", "balance_after",
    "peak_balance_after", "drawdown_after", "drawdown_pct_after",
    "theoretical_rr", "executed_rr",
    "raw_entry_price", "executed_entry_price", "raw_exit_price",
    "executed_exit_price",
]

NUMERIC_TRADE_FIELDS = {
    "entry", "sl", "tp1", "tp2", "rr", "realized_rr", "wick_ratio",
    "htf_high", "htf_low", "tp_level", "ltf_high", "ltf_low",
    "balance_before", "risk_amount", "pnl", "balance_after",
    "peak_balance_after", "drawdown_after", "drawdown_pct_after",
    "theoretical_rr", "executed_rr",
    "raw_entry_price", "executed_entry_price", "raw_exit_price",
    "executed_exit_price",
}


def _maybe_float(value):
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return value

for path in csv_files:
    symbol = path.stem
    try:
        raw_all = list(csv.DictReader(open(path, encoding="utf-8")))
    except Exception:
        continue
    if not raw_all:
        continue
    has_tf = "htf_interval" in (raw_all[0] if raw_all else {})
    if has_tf:
        tf_groups: dict[tuple, list] = {}
        for r in raw_all:
            key = (r.get("htf_interval", ""), r.get("ltf_interval", ""))
            tf_groups.setdefault(key, []).append(r)
    else:
        tf_groups = {("", ""): raw_all}

    for (htf_iv, ltf_iv), raw in tf_groups.items():
        if not raw:
            continue
        tf_label = f" [{htf_iv}/{ltf_iv}]" if htf_iv else ""
        pair = f"{symbol}{tf_label}"

        wins = [r for r in raw if r["outcome"] == "WIN_FULL"]
        losses = [r for r in raw if r["outcome"] == "LOSS"]
        bes = [r for r in raw if r["outcome"] == "BREAKEVEN"]
        closed = wins + losses + bes

        total_r = sum(float(r["realized_rr"]) for r in raw)
        wr = 100 * len(wins) / len(closed) if closed else 0.0
        be_rate = 100 * len(bes) / len(closed) if closed else 0.0
        loss_rate = 100 * len(losses) / len(closed) if closed else 0.0
        expectancy = total_r / len(raw)
        pf_num = sum(float(r["realized_rr"]) for r in wins + bes)
        pf_den = abs(sum(float(r["realized_rr"]) for r in losses))
        pf = pf_num / pf_den if pf_den else None

        equity = []
        running = 0.0
        for r in raw:
            running += float(r["realized_rr"])
            equity.append(round(running, 3))

        # Max Drawdown (R-based)
        if equity:
            peak = equity[0]
            max_dd = max(0.0, peak - equity[0])
            for v in equity:
                if v > peak:
                    peak = v
                dd = peak - v
                if dd > max_dd:
                    max_dd = dd
        else:
            max_dd = 0.0

        # Dollar-based account simulation (uses per-trade fields when available)
        _has_dollar = "balance_after" in (raw[0] if raw else {})
        if _has_dollar:
            _bal_series = [float(r["balance_after"]) for r in raw]
            _start_bal = float(raw[0].get("balance_before", 5000))
            _final_bal = _bal_series[-1] if _bal_series else _start_bal
            _net_pnl = _final_bal - _start_bal
            _net_pnl_pct = (_net_pnl / _start_bal * 100) if _start_bal > 0 else 0.0
            _equity_dollar = [_start_bal] + _bal_series
            _peak_d = _start_bal
            _max_dd_dollar = 0.0
            _max_dd_dollar_pct = 0.0
            for _v in _equity_dollar:
                if _v > _peak_d:
                    _peak_d = _v
                _dd = _peak_d - _v
                _dd_pct = (_dd / _peak_d * 100) if _peak_d > 0 else 0.0
                if _dd > _max_dd_dollar:
                    _max_dd_dollar = _dd
                if _dd_pct > _max_dd_dollar_pct:
                    _max_dd_dollar_pct = _dd_pct
            _pnls = [float(r.get("pnl", 0)) for r in raw]
            _gross_profit = sum(p for p in _pnls if p > 0)
            _gross_loss = abs(sum(p for p in _pnls if p < 0))
            _pf_dollar = _gross_profit / _gross_loss if _gross_loss > 0 else None
            dollar_stats = {
                "start_balance": round(_start_bal, 2),
                "final_balance": round(_final_bal, 2),
                "net_pnl": round(_net_pnl, 2),
                "net_pnl_pct": round(_net_pnl_pct, 4),
                "max_drawdown_dollar": round(_max_dd_dollar, 2),
                "max_drawdown_dollar_pct": round(_max_dd_dollar_pct, 4),
                "equity_dollar": [round(v, 2) for v in _equity_dollar],
                "gross_profit": round(_gross_profit, 2),
                "gross_loss": round(_gross_loss, 2),
                "profit_factor_dollar": round(_pf_dollar, 4) if _pf_dollar is not None else None,
            }
        else:
            dollar_stats = None

        max_win_streak = max_loss_streak = cur_win = cur_loss = 0
        for r in raw:
            if r["outcome"] == "WIN_FULL":
                cur_win += 1
                cur_loss = 0
            elif r["outcome"] == "LOSS":
                cur_loss += 1
                cur_win = 0
            else:
                cur_win = cur_loss = 0
            max_win_streak = max(max_win_streak, cur_win)
            max_loss_streak = max(max_loss_streak, cur_loss)

        rr_vals = [float(r["realized_rr"]) for r in raw]
        best_rr = max(rr_vals)
        worst_rr = min(rr_vals)

        def rolling_stats(trades, n):
            t = trades[-n:]
            if not t:
                return {"wr": 0, "be_rate": 0, "loss_rate": 0, "exp": 0, "trades": 0}
            w = sum(1 for x in t if x["outcome"] == "WIN_FULL")
            b = sum(1 for x in t if x["outcome"] == "BREAKEVEN")
            l = sum(1 for x in t if x["outcome"] == "LOSS")
            cl = w + b + l
            wr2 = 100 * w / cl if cl else 0
            ber2 = 100 * b / cl if cl else 0
            lr2 = 100 * l / cl if cl else 0
            exp = sum(float(x["realized_rr"]) for x in t) / len(t)
            return {
                "wr": round(wr2, 1),
                "be_rate": round(ber2, 1),
                "loss_rate": round(lr2, 1),
                "exp": round(exp, 3),
                "trades": len(t),
            }

        rolling = {
            "r10": rolling_stats(raw, 10),
            "r20": rolling_stats(raw, 20),
            "r50": rolling_stats(raw, 50),
            "all": {
                "wr": round(wr, 1),
                "be_rate": round(be_rate, 1),
                "loss_rate": round(loss_rate, 1),
                "exp": round(expectancy, 3),
                "trades": len(raw),
            },
        }

        # Partial close tracking
        tp1_hits = [r for r in raw if float(r.get("realized_rr", 0)) > 0 and r["outcome"] != "BREAKEVEN"]
        hit_entry_after_tp1 = [r for r in raw if r.get("hit_entry_after_tp1", "False") == "True"]
        
        partial_stats = {
            "tp1_hits": len(tp1_hits),
            "entry_retrace_hits": len(hit_entry_after_tp1),
            "retrace_rate": round(100 * len(hit_entry_after_tp1) / len(tp1_hits), 1) if tp1_hits else 0,
        }

        be_hold_times = []
        win_hold_times = []
        loss_hold_times = []
        for r in raw:
            try:
                e = datetime.strptime(r["entry_dt"][:16], "%Y-%m-%d %H:%M")
                c = datetime.strptime(r["close_dt"][:16], "%Y-%m-%d %H:%M")
                mins = (c - e).total_seconds() / 60
                if r["outcome"] == "WIN_FULL":
                    win_hold_times.append(mins)
                elif r["outcome"] == "LOSS":
                    loss_hold_times.append(mins)
                else:
                    be_hold_times.append(mins)
            except Exception:
                continue

        avg_hold_min = round(
            sum(win_hold_times + loss_hold_times + be_hold_times)
            / max(len(win_hold_times + loss_hold_times + be_hold_times), 1),
            1,
        )
        avg_win_hold = round(sum(win_hold_times) / max(len(win_hold_times), 1), 1)
        avg_loss_hold = round(sum(loss_hold_times) / max(len(loss_hold_times), 1), 1)
        avg_be_hold = round(sum(be_hold_times) / max(len(be_hold_times), 1), 1)

        hour_stats: dict = {}
        for r in raw:
            try:
                h = int(r.get("entry_dt", "")[11:13])
            except Exception:
                continue
            if h not in hour_stats:
                hour_stats[h] = {"w": 0, "l": 0, "b": 0, "r": 0.0}
            if r["outcome"] == "WIN_FULL":
                hour_stats[h]["w"] += 1
            elif r["outcome"] == "LOSS":
                hour_stats[h]["l"] += 1
            else:
                hour_stats[h]["b"] += 1
            hour_stats[h]["r"] += float(r["realized_rr"])

        scored_hours = [
            (h, s) for h, s in hour_stats.items() if (s["w"] + s["l"] + s["b"]) >= 2
        ]
        scored_hours.sort(key=lambda x: x[1]["r"], reverse=True)
        best_hours = scored_hours[:3]
        worst_hours = scored_hours[-3:][::-1]

        pattern_stats: dict = {}
        pattern_retrace: dict = {}
        for r in raw:
            pat = r.get("pattern", "UNKNOWN") or "UNKNOWN"
            if pat not in pattern_stats:
                pattern_stats[pat] = {"w": 0, "l": 0, "b": 0, "r": 0.0, "trades": 0}
                pattern_retrace[pat] = {"tp1_hits": 0, "retraces": 0}
            
            if r["outcome"] == "WIN_FULL":
                pattern_stats[pat]["w"] += 1
            elif r["outcome"] == "LOSS":
                pattern_stats[pat]["l"] += 1
            else:
                pattern_stats[pat]["b"] += 1
            pattern_stats[pat]["r"] += float(r["realized_rr"])
            pattern_stats[pat]["trades"] += 1
            
            # Track retrace for this pattern
            if float(r.get("realized_rr", 0)) > 0 and r["outcome"] != "BREAKEVEN":
                pattern_retrace[pat]["tp1_hits"] += 1
                if r.get("hit_entry_after_tp1", "False") == "True":
                    pattern_retrace[pat]["retraces"] += 1

        dow_stats: dict = {}
        DOW_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
        for r in raw:
            try:
                dt = datetime.strptime(r.get("entry_dt", "")[:10], "%Y-%m-%d")
                d = dt.weekday()
            except Exception:
                continue
            if d not in dow_stats:
                dow_stats[d] = {"w": 0, "l": 0, "b": 0, "r": 0.0, "trades": 0}
            if r["outcome"] == "WIN_FULL":
                dow_stats[d]["w"] += 1
            elif r["outcome"] == "LOSS":
                dow_stats[d]["l"] += 1
            else:
                dow_stats[d]["b"] += 1
            dow_stats[d]["r"] += float(r["realized_rr"])
            dow_stats[d]["trades"] += 1

        rr_buckets = {"<-1": 0, "-1": 0, "0": 0, "1-2": 0, "2-3": 0, ">3": 0}
        for v in rr_vals:
            if v < -1:
                rr_buckets["<-1"] += 1
            elif v == -1.0:
                rr_buckets["-1"] += 1
            elif v == 0.0:
                rr_buckets["0"] += 1
            elif v <= 2.0:
                rr_buckets["1-2"] += 1
            elif v <= 3.0:
                rr_buckets["2-3"] += 1
            else:
                rr_buckets[">3"] += 1

        monthly: dict = {}
        for r in raw:
            try:
                ym = r.get("entry_dt", "")[:7]
                if not ym:
                    continue
            except Exception:
                continue
            if ym not in monthly:
                monthly[ym] = {"w": 0, "l": 0, "b": 0, "r": 0.0, "trades": 0}
            if r["outcome"] == "WIN_FULL":
                monthly[ym]["w"] += 1
            elif r["outcome"] == "LOSS":
                monthly[ym]["l"] += 1
            else:
                monthly[ym]["b"] += 1
            monthly[ym]["r"] += float(r["realized_rr"])
            monthly[ym]["trades"] += 1

        def monthly_score(m_data, total_trades):
            if not total_trades:
                return 0
            w = m_data["w"]
            l = m_data["l"]
            b = m_data["b"]
            r = m_data["r"]
            t = m_data["trades"]
            cl = w + l + b
            wr_s = min(40, max(0, (w / cl * 100 - 30) / 40 * 40)) if cl else 0
            exp_s = min(40, max(0, (r / t + 0.3) / 1.3 * 40)) if t else 0
            dd_s = 20 if r >= 0 else max(0, 20 + r * 4)
            return round(wr_s + exp_s + dd_s)

        daily: dict = {}
        for r in raw:
            try:
                date = r.get("entry_dt", "")[:10]
                if not date:
                    continue
            except Exception:
                continue
            if date not in daily:
                daily[date] = {"r": 0.0, "w": 0, "l": 0, "b": 0}
            daily[date]["r"] += float(r["realized_rr"])
            if r["outcome"] == "WIN_FULL":
                daily[date]["w"] += 1
            elif r["outcome"] == "LOSS":
                daily[date]["l"] += 1
            else:
                daily[date]["b"] += 1

        longs = [r for r in raw if r["direction"] == "LONG"]
        shorts = [r for r in raw if r["direction"] == "SHORT"]
        long_wr = (
            100 * len([r for r in longs if r["outcome"] == "WIN_FULL"]) / len(longs)
            if longs
            else 0
        )
        short_wr = (
            100 * len([r for r in shorts if r["outcome"] == "WIN_FULL"]) / len(shorts)
            if shorts
            else 0
        )
        long_r = sum(float(r["realized_rr"]) for r in longs)
        short_r = sum(float(r["realized_rr"]) for r in shorts)

        wr_frac = (len(wins) / len(closed)) if closed else 0.5
        loss_frac = 1 - wr_frac
        expected_max_loss_streak = 0
        if loss_frac > 0 and loss_frac < 1:
            n = len(raw)
            expected_max_loss_streak = (
                round(math.log(n) / (-math.log(loss_frac))) if n > 0 else 0
            )

        streak_alert = max_loss_streak > expected_max_loss_streak * 2

        raw_trades = []
        for idx_r, r in enumerate(raw):
            hold_m = None
            try:
                e = datetime.strptime(r["entry_dt"][:16], "%Y-%m-%d %H:%M")
                c = datetime.strptime(r["close_dt"][:16], "%Y-%m-%d %H:%M")
                hold_m = round((c - e).total_seconds() / 60, 0)
            except Exception:
                pass
            trade = dict(r)
            for col in TRADE_COLUMNS:
                trade.setdefault(col, "")
            for col in NUMERIC_TRADE_FIELDS:
                trade[col] = _maybe_float(trade.get(col))
            trade["hit_entry_after_tp1"] = (
                str(trade.get("hit_entry_after_tp1", "False")).lower() == "true"
            )
            trade.update(
                {
                    "i": idx_r,
                    "hold_min": hold_m,
                    "pair": pair,
                    "symbol": trade.get("symbol") or symbol,
                    "htf_interval": trade.get("htf_interval") or htf_iv,
                    "ltf_interval": trade.get("ltf_interval") or ltf_iv,
                    "tf_pair": f"{htf_iv}/{ltf_iv}" if htf_iv else "",
                    "realized_rr": float(trade.get("realized_rr") or 0.0),
                }
            )
            raw_trades.append(
                trade
            )

        rows_by_pair.append(
            {
                "pair": pair,
                "symbol": symbol,
                "htf_interval": htf_iv,
                "ltf_interval": ltf_iv,
                "tf_pair": f"{htf_iv}/{ltf_iv}" if htf_iv else "",
                "trades": len(raw),
                "wins": len(wins),
                "losses": len(losses),
                "bes": len(bes),
                "closed": len(closed),
                "wr": wr,
                "be_rate": be_rate,
                "loss_rate": loss_rate,
                "total_r": total_r,
                "exp": expectancy,
                "pf": round(pf, 3) if pf is not None else None,
                "max_dd": max_dd,
                "max_win_streak": max_win_streak,
                "max_loss_streak": max_loss_streak,
                "expected_max_loss_streak": int(expected_max_loss_streak),
                "streak_alert": streak_alert,
                "best_rr": best_rr,
                "worst_rr": worst_rr,
                "equity": equity,
                "partial_stats": partial_stats,
                "hour_stats": {str(k): v for k, v in hour_stats.items()},
                "best_hours": [(str(h), s) for h, s in best_hours],
                "worst_hours": [(str(h), s) for h, s in worst_hours],
                "monthly": {
                    k: {
                        "w": v["w"],
                        "l": v["l"],
                        "b": v["b"],
                        "r": round(v["r"], 3),
                        "trades": v["trades"],
                        "wr": (
                            round(100 * v["w"] / (v["w"] + v["l"] + v["b"]), 1)
                            if (v["w"] + v["l"] + v["b"])
                            else 0
                        ),
                        "be_rate": (
                            round(100 * v["b"] / (v["w"] + v["l"] + v["b"]), 1)
                            if (v["w"] + v["l"] + v["b"])
                            else 0
                        ),
                        "loss_rate": (
                            round(100 * v["l"] / (v["w"] + v["l"] + v["b"]), 1)
                            if (v["w"] + v["l"] + v["b"])
                            else 0
                        ),
                        "score": monthly_score(v, len(raw)),
                    }
                    for k, v in sorted(monthly.items())
                },
                "daily": {
                    k: {"r": round(v["r"], 3), "w": v["w"], "l": v["l"], "b": v["b"]}
                    for k, v in sorted(daily.items())
                },
                "long_trades": len(longs),
                "short_trades": len(shorts),
                "long_wins": len([r for r in longs if r["outcome"] == "WIN_FULL"]),
                "short_wins": len([r for r in shorts if r["outcome"] == "WIN_FULL"]),
                "long_r": round(long_r, 3),
                "short_r": round(short_r, 3),
                "long_wr": round(long_wr, 1),
                "short_wr": round(short_wr, 1),
                "pattern_stats": {
                    k: {
                        "w": v["w"],
                        "l": v["l"],
                        "b": v["b"],
                        "r": round(v["r"], 3),
                        "trades": v["trades"],
                        "wr": (
                            round(100 * v["w"] / (v["w"] + v["l"] + v["b"]), 1)
                            if (v["w"] + v["l"] + v["b"])
                            else 0
                        ),
                        "be_rate": (
                            round(100 * v["b"] / (v["w"] + v["l"] + v["b"]), 1)
                            if (v["w"] + v["l"] + v["b"])
                            else 0
                        ),
                        "loss_rate": (
                            round(100 * v["l"] / (v["w"] + v["l"] + v["b"]), 1)
                            if (v["w"] + v["l"] + v["b"])
                            else 0
                        ),
                        "retrace_rate": round(100 * pattern_retrace.get(k, {}).get("retraces", 0) / max(pattern_retrace.get(k, {}).get("tp1_hits", 1), 1), 1),
                    }
                    for k, v in sorted(
                        pattern_stats.items(), key=lambda x: x[1]["r"], reverse=True
                    )
                },
                "dow_stats": {
                    str(k): {
                        "name": DOW_NAMES[k],
                        "w": v["w"],
                        "l": v["l"],
                        "b": v["b"],
                        "r": round(v["r"], 3),
                        "trades": v["trades"],
                        "wr": (
                            round(100 * v["w"] / (v["w"] + v["l"] + v["b"]), 1)
                            if (v["w"] + v["l"] + v["b"])
                            else 0
                        ),
                        "be_rate": (
                            round(100 * v["b"] / (v["w"] + v["l"] + v["b"]), 1)
                            if (v["w"] + v["l"] + v["b"])
                            else 0
                        ),
                        "loss_rate": (
                            round(100 * v["l"] / (v["w"] + v["l"] + v["b"]), 1)
                            if (v["w"] + v["l"] + v["b"])
                            else 0
                        ),
                    }
                    for k, v in sorted(dow_stats.items())
                },
                "rr_buckets": rr_buckets,
                "avg_hold_min": avg_hold_min,
                "avg_win_hold": avg_win_hold,
                "avg_loss_hold": avg_loss_hold,
                "avg_be_hold": avg_be_hold,
                "rolling": rolling,
                "raw_trades": raw_trades,
                "dollar_stats": dollar_stats,
            }
        )

if not rows_by_pair:
    print(f"\n  No signals found.\n")
    sys.exit(0)

sort_key = {
    "pair": lambda x: x["pair"],
    "r": lambda x: x["total_r"],
    "wr": lambda x: x["wr"],
    "trades": lambda x: x["trades"],
    "pf": lambda x: (x["pf"] if x["pf"] is not None else 9999),
    "exp": lambda x: x["exp"],
}.get(sort_by, lambda x: x["pair"])
rows_by_pair.sort(key=sort_key, reverse=(sort_by != "pair"))

combined_monthly: dict = defaultdict(
    lambda: {"r": 0.0, "trades": 0, "w": 0, "l": 0, "b": 0}
)
combined_daily: dict = defaultdict(
    lambda: {"r": 0.0, "w": 0, "l": 0, "b": 0, "pairs": []}
)
tf_pair_stats: dict = defaultdict(
    lambda: {"r": 0.0, "trades": 0, "w": 0, "l": 0, "b": 0, "equity": []}
)
all_flat = []

_pair_label: dict[tuple, str] = {
    (d["symbol"], d["htf_interval"], d["ltf_interval"]): d["pair"] for d in rows_by_pair
}

for path in sorted(results_dir.rglob("*.csv")):
    try:
        for r in csv.DictReader(open(path, encoding="utf-8")):
            sym = path.stem
            htf = r.get("htf_interval", "")
            ltf = r.get("ltf_interval", "")
            tf_label = f" [{htf}/{ltf}]" if htf else ""
            pair_lbl = _pair_label.get((sym, htf, ltf), f"{sym}{tf_label}")
            tf_key = f"{htf}/{ltf}" if htf else sym
            r["_pair"] = pair_lbl
            all_flat.append(r)
            ym = r.get("entry_dt", "")[:7]
            date = r.get("entry_dt", "")[:10]
            rr = float(r["realized_rr"])
            if ym:
                combined_monthly[ym]["r"] += rr
                combined_monthly[ym]["trades"] += 1
                if r["outcome"] == "WIN_FULL":
                    combined_monthly[ym]["w"] += 1
                elif r["outcome"] == "LOSS":
                    combined_monthly[ym]["l"] += 1
                else:
                    combined_monthly[ym]["b"] += 1
            if date:
                combined_daily[date]["r"] += rr
                if r["outcome"] == "WIN_FULL":
                    combined_daily[date]["w"] += 1
                elif r["outcome"] == "LOSS":
                    combined_daily[date]["l"] += 1
                else:
                    combined_daily[date]["b"] += 1
                combined_daily[date]["pairs"].append(
                    {
                        "pair": pair_lbl,
                        "dir": r.get("direction", ""),
                        "outcome": r["outcome"],
                        "rr": round(rr, 3),
                        "entry": r.get("entry_dt", "")[11:16],
                    }
                )
            tf_pair_stats[tf_key]["r"] += rr
            tf_pair_stats[tf_key]["trades"] += 1
            if r["outcome"] == "WIN_FULL":
                tf_pair_stats[tf_key]["w"] += 1
            elif r["outcome"] == "LOSS":
                tf_pair_stats[tf_key]["l"] += 1
            else:
                tf_pair_stats[tf_key]["b"] += 1
    except Exception:
        pass

all_flat.sort(key=lambda r: r.get("entry_dt", ""))
combined_eq = []
running = 0.0
for r in all_flat:
    running += float(r["realized_rr"])
    combined_eq.append(round(running, 3))

if combined_eq:
    _cpeak = combined_eq[0]
    grand_max_dd = 0.0
    for _v in combined_eq:
        if _v > _cpeak:
            _cpeak = _v
        _dd = _cpeak - _v
        if _dd > grand_max_dd:
            grand_max_dd = _dd
    grand_max_dd = round(grand_max_dd, 3)
else:
    grand_max_dd = 0.0

sorted_months = sorted(combined_monthly.keys())
cumulative = 0.0
monthly_cumulative = []
for ym in sorted_months:
    m = combined_monthly[ym]
    cumulative += m["r"]
    cl = m["w"] + m["l"] + m["b"]
    monthly_cumulative.append(
        {
            "ym": ym,
            "r": round(m["r"], 3),
            "cum_r": round(cumulative, 3),
            "trades": m["trades"],
            "w": m["w"],
            "l": m["l"],
            "b": m["b"],
            "wr": round(100 * m["w"] / cl, 1) if cl else 0,
            "be_rate": round(100 * m["b"] / cl, 1) if cl else 0,
            "loss_rate": round(100 * m["l"] / cl, 1) if cl else 0,
        }
    )

pairs_list = [d["pair"] for d in rows_by_pair]
pair_idx = {p: i for i, p in enumerate(pairs_list)}
n_pairs = len(pairs_list)
corr_days = defaultdict(lambda: defaultdict(list))
for path in sorted(results_dir.rglob("*.csv")):
    try:
        for r in csv.DictReader(open(path, encoding="utf-8")):
            date = r.get("entry_dt", "")[:10]
            if date:
                sym = path.stem
                htf = r.get("htf_interval", "")
                ltf = r.get("ltf_interval", "")
                tf_label = f" [{htf}/{ltf}]" if htf else ""
                lbl = _pair_label.get((sym, htf, ltf), f"{sym}{tf_label}")
                corr_days[date][lbl].append(r["outcome"])
    except Exception:
        pass

coloss = [[0] * n_pairs for _ in range(n_pairs)]
cowin = [[0] * n_pairs for _ in range(n_pairs)]
codays = [[0] * n_pairs for _ in range(n_pairs)]
for date, pd in corr_days.items():
    pairs_on_day = list(pd.keys())
    for i_p, p1 in enumerate(pairs_on_day):
        for p2 in pairs_on_day[i_p + 1 :]:
            if p1 not in pair_idx or p2 not in pair_idx:
                continue
            ia, ib = pair_idx[p1], pair_idx[p2]
            codays[ia][ib] += 1
            codays[ib][ia] += 1
            p1_loss = any(o == "LOSS" for o in pd[p1])
            p2_loss = any(o == "LOSS" for o in pd[p2])
            p1_win = any(o == "WIN_FULL" for o in pd[p1])
            p2_win = any(o == "WIN_FULL" for o in pd[p2])
            if p1_loss and p2_loss:
                coloss[ia][ib] += 1
                coloss[ib][ia] += 1
            if p1_win and p2_win:
                cowin[ia][ib] += 1
                cowin[ib][ia] += 1

corr_matrix = []
for i in range(n_pairs):
    row = []
    for j in range(n_pairs):
        if i == j:
            row.append(
                {"days": 0, "coloss": 0, "cowin": 0, "loss_pct": None, "win_pct": None}
            )
        else:
            d = codays[i][j]
            row.append(
                {
                    "days": d,
                    "coloss": coloss[i][j],
                    "cowin": cowin[i][j],
                    "loss_pct": round(coloss[i][j] / d * 100, 0) if d else None,
                    "win_pct": round(cowin[i][j] / d * 100, 0) if d else None,
                }
            )
    corr_matrix.append(row)

# ── Terminal table ─────────────────────────────────────────────────────────────
_has_dollar_any = any(d["dollar_stats"] is not None for d in rows_by_pair)

print(f"\n  Results: {results_dir}/  ({len(rows_by_pair)} pairs)\n")
if _has_dollar_any:
    print(
        f"  {'PAIR':<12}  {'TRADES':>6}  {'W':>4}  {'BE':>4}  {'L':>4}  "
        f"{'WR%':>5}  {'TOTAL_R':>8}  {'EXPECT':>8}  {'PF':>6}  {'MAX_DD_R':>8}  "
        f"{'NET_PNL':>10}  {'NET%':>7}  {'DD$':>10}  {'DD%':>6}  {'L-STK':>5}  {'RETRACE':>7}"
    )
    print(f"  {'-'*135}")
else:
    print(
        f"  {'PAIR':<12}  {'TRADES':>6}  {'W':>4}  {'BE':>4}  {'L':>4}  "
        f"{'WR%':>5}  {'BE%':>5}  {'L%':>5}  {'TOTAL_R':>8}  {'EXPECT':>8}  {'PF':>6}  {'MAX_DD':>7}  {'L-STK':>5}  {'RETRACE':>7}"
    )
    print(f"  {'-'*110}")

grand_trades = grand_wins = grand_losses = grand_bes = 0
grand_r = 0.0
grand_tp1_hits = 0
grand_retraces = 0

for d in rows_by_pair:
    pf_str = f"{d['pf']:>6.2f}" if d["pf"] is not None else "   inf"
    alert = " ⚠" if d["streak_alert"] else ""
    retrace_str = f"{d['partial_stats']['retrace_rate']:>5.0f}%" if d['partial_stats']['tp1_hits'] > 0 else "   N/A"
    if _has_dollar_any and d["dollar_stats"]:
        ds = d["dollar_stats"]
        net_sign = "+" if ds["net_pnl"] >= 0 else ""
        dd_sign = "-" if ds["max_drawdown_dollar"] > 0 else " "
        print(
            f"  {d['pair']:<12}  {d['trades']:>6}  {d['wins']:>4}  {d['bes']:>4}  {d['losses']:>4}  "
            f"{d['wr']:>4.0f}%  "
            f"{d['total_r']:>+7.2f}R  {d['exp']:>+7.3f}R  "
            f"{pf_str}  {d['max_dd']:>7.2f}R  "
            f"{net_sign}${abs(ds['net_pnl']):>8,.2f}  "
            f"{net_sign}{abs(ds['net_pnl_pct']):>5.2f}%  "
            f"{dd_sign}${abs(ds['max_drawdown_dollar']):>8,.2f}  "
            f"{ds['max_drawdown_dollar_pct']:>5.2f}%  "
            f"{d['max_loss_streak']:>5}{alert}  {retrace_str}"
        )
    else:
        print(
            f"  {d['pair']:<12}  {d['trades']:>6}  {d['wins']:>4}  {d['bes']:>4}  {d['losses']:>4}  "
            f"{d['wr']:>4.0f}%  {d['be_rate']:>4.0f}%  {d['loss_rate']:>4.0f}%  "
            f"{d['total_r']:>+7.2f}R  {d['exp']:>+7.3f}R  "
            f"{pf_str}  {d['max_dd']:>6.2f}R  {d['max_loss_streak']:>5}{alert}  {retrace_str}"
        )
    grand_trades += d["trades"]
    grand_r += d["total_r"]
    grand_wins += d["wins"]
    grand_losses += d["losses"]
    grand_bes += d["bes"]
    grand_tp1_hits += d['partial_stats']['tp1_hits']
    grand_retraces += d['partial_stats']['entry_retrace_hits']

grand_closed = grand_wins + grand_losses + grand_bes
grand_wr = 100 * grand_wins / grand_closed if grand_closed else 0.0
grand_be_rate = 100 * grand_bes / grand_closed if grand_closed else 0.0
grand_loss_rate = 100 * grand_losses / grand_closed if grand_closed else 0.0
grand_exp = grand_r / grand_trades if grand_trades else 0.0
grand_retrace_rate = round(100 * grand_retraces / max(grand_tp1_hits, 1), 1)

_all_win_rr = [
    t["realized_rr"]
    for d in rows_by_pair
    for t in d["raw_trades"]
    if t["outcome"] == "WIN_FULL"
]
_all_loss_rr = [
    abs(t["realized_rr"])
    for d in rows_by_pair
    for t in d["raw_trades"]
    if t["outcome"] == "LOSS"
]
grand_avg_win = round(sum(_all_win_rr) / len(_all_win_rr), 3) if _all_win_rr else 1.5
grand_avg_loss = (
    round(sum(_all_loss_rr) / len(_all_loss_rr), 3) if _all_loss_rr else 1.0
)
grand_pf = round(sum(_all_win_rr) / sum(_all_loss_rr), 3) if _all_loss_rr else None

grand_pf_str = f"{grand_pf:>6.2f}" if grand_pf is not None else "   inf"

# Grand dollar totals (summed across pairs when available)
grand_dollar_stats = None
if _has_dollar_any:
    _ds_list = [d["dollar_stats"] for d in rows_by_pair if d["dollar_stats"]]
    if _ds_list:
        grand_net_pnl = sum(ds["net_pnl"] for ds in _ds_list)
        grand_max_dd_dollar = max(ds["max_drawdown_dollar"] for ds in _ds_list)
        grand_max_dd_dollar_pct = max(ds["max_drawdown_dollar_pct"] for ds in _ds_list)
        grand_dollar_stats = {
            "net_pnl": grand_net_pnl,
            "max_drawdown_dollar": grand_max_dd_dollar,
            "max_drawdown_dollar_pct": grand_max_dd_dollar_pct,
        }

if _has_dollar_any:
    print(f"  {'-'*135}")
    gds = grand_dollar_stats or {}
    g_net = gds.get("net_pnl", 0.0)
    g_net_sign = "+" if g_net >= 0 else ""
    g_dd = gds.get("max_drawdown_dollar", 0.0)
    g_dd_pct = gds.get("max_drawdown_dollar_pct", 0.0)
    print(
        f"  {'COMBINED':<12}  {grand_trades:>6}  {grand_wins:>4}  {grand_bes:>4}  {grand_losses:>4}  "
        f"{grand_wr:>4.0f}%  "
        f"{grand_r:>+7.2f}R  {grand_exp:>+7.3f}R  {grand_pf_str}  {'':>8}  "
        f"{g_net_sign}${abs(g_net):>8,.2f}  {'':>7}  "
        f"-${g_dd:>8,.2f}  {g_dd_pct:>5.2f}%  {'':>5}  {grand_retrace_rate:>5.0f}%"
    )
else:
    print(f"  {'-'*110}")
    print(
        f"  {'COMBINED':<12}  {grand_trades:>6}  {grand_wins:>4}  {grand_bes:>4}  {grand_losses:>4}  "
        f"{grand_wr:>4.0f}%  {grand_be_rate:>4.0f}%  {grand_loss_rate:>4.0f}%  "
        f"{grand_r:>+7.2f}R  {grand_exp:>+7.3f}R  {grand_pf_str}  {'':>7}  {grand_retrace_rate:>5.0f}%"
    )
print(f"\n  Sorted by: {sort_by}  |  --sort [pair|r|wr|trades|pf|exp]")
print(f"  Retrace % = % of TP1 hits that revisited entry before TP2\n")

if not gen_html:
    sys.exit(0)

# ── Serialize ──────────────────────────────────────────────────────────────────
data_json = json.dumps(
    {
        "pairs": rows_by_pair,
        "trade_columns": TRADE_COLUMNS,
        "combined_eq": combined_eq,
        "monthly_cumulative": monthly_cumulative,
        "combined_daily": {
            k: {
                "r": round(v["r"], 3),
                "w": v["w"],
                "l": v["l"],
                "b": v["b"],
                "pairs": v["pairs"],
            }
            for k, v in sorted(combined_daily.items())
        },
        "tf_pair_stats": {
            tf: {
                "tf_pair": tf,
                "trades": s["trades"],
                "w": s["w"],
                "l": s["l"],
                "b": s["b"],
                "r": round(s["r"], 3),
                "wr": (
                    round(100 * s["w"] / (s["w"] + s["l"] + s["b"]), 1)
                    if (s["w"] + s["l"] + s["b"])
                    else 0
                ),
                "be_rate": (
                    round(100 * s["b"] / (s["w"] + s["l"] + s["b"]), 1)
                    if (s["w"] + s["l"] + s["b"])
                    else 0
                ),
                "loss_rate": (
                    round(100 * s["l"] / (s["w"] + s["l"] + s["b"]), 1)
                    if (s["w"] + s["l"] + s["b"])
                    else 0
                ),
                "exp": round(s["r"] / s["trades"], 3) if s["trades"] else 0,
            }
            for tf, s in sorted(
                tf_pair_stats.items(), key=lambda x: x[1]["r"], reverse=True
            )
        },
        "generated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source": str(results_dir),
        "pairs_list": pairs_list,
        "corr_matrix": corr_matrix,
        "grand": {
            "trades": grand_trades,
            "wins": grand_wins,
            "losses": grand_losses,
            "bes": grand_bes,
            "total_r": round(grand_r, 3),
            "wr": round(grand_wr, 1),
            "be_rate": round(grand_be_rate, 1),
            "loss_rate": round(grand_loss_rate, 1),
            "exp": round(grand_exp, 3),
            "pf": round(grand_pf, 3) if grand_pf is not None else None,
            "avg_win": grand_avg_win,
            "avg_loss": grand_avg_loss,
            "max_dd": grand_max_dd,
            "retrace_rate": grand_retrace_rate,
            "tp1_hits": grand_tp1_hits,
            "dollar_stats": grand_dollar_stats,
        },
    },
    default=str,
)

# Note: The HTML_TEMPLATE string is extremely long.
# I'll provide the complete HTML template separately or you can use the one from the previous response
# and just add the partial close tab which I already provided in the previous answer.

print(f"\n  Data serialized, generating HTML report...")
print(f"  Grand retrace rate: {grand_retrace_rate}% ({grand_retraces}/{grand_tp1_hits} TP1 hits)")


# ── HTML TEMPLATE ──────────────────────────────────────────────────────────────
HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Backtest Report</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
<style>
:root{
  --bg:#f0f2f5; --card:#ffffff; --card2:#f7f8fa; --brd:#dde1e9; --brd2:#c4c9d6;
  --win:#0d9e5c; --win-bg:#e8f8f1; --win-brd:#b6e8d0;
  --loss:#d63b3b; --loss-bg:#fdeaea; --loss-brd:#f5b8b8;
  --be:#b07d00; --be-bg:#fef9e7;
  --acc:#4f6ef7; --acc2:#0e8fca; --acc3:#7c5cbf;
  --txt:#1a1f2e; --sub:#5a6480; --dim:#9aa0b4;
  --ff:'Inter',system-ui,sans-serif;
}
*{margin:0;padding:0;box-sizing:border-box;}
html{font-size:15px;}
body{background:var(--bg);color:var(--txt);font-family:var(--ff);min-height:100vh;}
::-webkit-scrollbar{width:7px;height:7px;}
::-webkit-scrollbar-track{background:var(--bg);}
::-webkit-scrollbar-thumb{background:var(--brd2);border-radius:4px;}

body.dark{
  --bg:#0f1117; --card:#1a1f2e; --card2:#22283a; --brd:#2d3350; --brd2:#3d4470;
  --txt:#e8eaf0; --sub:#8891b4; --dim:#5a6480;
}
body.dark #hdr,body.dark #nav{background:#1a1f2e;}
body.dark #tz-bar{background:#0a0d14;border-color:#1a1f2e;}
body.dark tbody tr:hover td{background:#22283a;}
body.dark .cbox,body.dark .hw-card,body.dark .cal-month,
body.dark .sess-card,body.dark .sdc-row,body.dark .tbl-wrap{background:var(--card);}

#tz-bar{
  background:#1a1f2e; padding:10px 36px;
  display:flex;align-items:center;gap:14px;flex-wrap:wrap;
  border-bottom:2px solid #2d3350;
}
#tz-bar label{font-size:12px;font-weight:700;color:#9aa0b4;text-transform:uppercase;letter-spacing:.1em;white-space:nowrap;}
#tz-select{
  background:#2d3350;color:#e8eaf0;border:1px solid #3d4470;
  border-radius:8px;padding:7px 14px;font-family:var(--ff);font-size:13px;font-weight:600;cursor:pointer;outline:none;
}
#tz-select:hover{border-color:#4f6ef7;}
.tz-badge{background:#2d3350;border:1px solid #3d4470;border-radius:6px;padding:4px 10px;
  font-size:12px;font-weight:700;color:#e8eaf0;display:flex;align-items:center;gap:6px;}
.tz-dot{width:8px;height:8px;border-radius:50%;background:#4f6ef7;}
#tz-info{font-size:12px;color:#6a7090;margin-left:auto;}
#dark-toggle{
  background:#2d3350;border:1px solid #3d4470;color:#9aa0b4;
  border-radius:8px;padding:7px 14px;font-family:var(--ff);font-size:13px;font-weight:600;
  cursor:pointer;white-space:nowrap;
}
#dark-toggle:hover{border-color:#4f6ef7;color:#e8eaf0;}

#hdr{
  padding:28px 36px 24px;border-bottom:1px solid var(--brd);
  display:flex;align-items:flex-start;justify-content:space-between;flex-wrap:wrap;gap:20px;
  background:#ffffff;box-shadow:0 1px 4px rgba(0,0,0,.06);
}
#hdr h1{font-size:24px;font-weight:800;color:var(--txt);letter-spacing:-.4px;}
#hdr .meta{font-size:13px;color:var(--sub);margin-top:5px;}
.kpis{display:flex;gap:10px;flex-wrap:wrap;}
.kpi{background:var(--card2);border:1px solid var(--brd);border-radius:10px;
  padding:14px 20px;text-align:right;min-width:100px;transition:border-color .2s,box-shadow .2s;}
.kpi:hover{border-color:var(--brd2);box-shadow:0 2px 8px rgba(0,0,0,.07);}
.kpi .kv{font-size:22px;font-weight:800;line-height:1;}
.kpi .kl{font-size:11px;font-weight:600;color:var(--sub);text-transform:uppercase;letter-spacing:.1em;margin-top:5px;}

#nav{
  display:flex;border-bottom:2px solid var(--brd);background:#fff;
  overflow-x:auto;padding:0 36px;position:sticky;top:0;z-index:100;
  box-shadow:0 1px 4px rgba(0,0,0,.05);
}
#nav::-webkit-scrollbar{display:none;}
.ntab{
  padding:15px 18px;font-size:13px;font-weight:600;color:var(--sub);
  border:none;background:none;cursor:pointer;
  border-bottom:3px solid transparent;margin-bottom:-2px;white-space:nowrap;transition:color .15s;
}
.ntab:hover{color:var(--txt);}
.ntab.on{color:var(--acc);border-bottom-color:var(--acc);}

.view{display:none;padding:28px 36px 80px;}
.view.on{display:block;}

.sh{
  font-size:11px;font-weight:700;letter-spacing:.14em;text-transform:uppercase;
  color:var(--sub);margin-bottom:14px;padding-bottom:8px;border-bottom:2px solid var(--brd);
}

.tbl-wrap{background:var(--card);border:1px solid var(--brd);border-radius:12px;
  overflow:hidden;margin-bottom:28px;overflow-x:auto;box-shadow:0 1px 4px rgba(0,0,0,.05);}
table{width:100%;border-collapse:collapse;min-width:780px;}
thead th{
  padding:13px 16px;text-align:left;font-size:11px;font-weight:700;
  color:var(--sub);text-transform:uppercase;letter-spacing:.1em;
  border-bottom:2px solid var(--brd);background:var(--card2);white-space:nowrap;cursor:pointer;
}
thead th:hover{color:var(--txt);}
thead th.sort-asc::after{content:' ↑';}
thead th.sort-desc::after{content:' ↓';}
tbody td{
  padding:12px 16px;border-bottom:1px solid var(--brd);font-size:13px;
  vertical-align:middle;white-space:nowrap;color:var(--txt);
}
tbody tr:last-child td{border-bottom:none;}
tbody tr:hover td{background:#f4f6ff;}
.badge{display:inline-block;padding:4px 10px;border-radius:6px;font-size:12px;font-weight:700;}
.badge.g{background:var(--win-bg);color:var(--win);border:1px solid var(--win-brd);}
.badge.r{background:var(--loss-bg);color:var(--loss);border:1px solid var(--loss-brd);}
.badge.y{background:var(--be-bg);color:var(--be);border:1px solid #f0d060;}
/* rate trio: compact inline WR/BE/L% display */
.rate-trio{display:inline-flex;gap:4px;align-items:center;flex-wrap:nowrap;}
.rate-trio .rb{display:inline-block;padding:3px 7px;border-radius:5px;font-size:11px;font-weight:700;white-space:nowrap;}
.rb-w{background:var(--win-bg);color:var(--win);border:1px solid var(--win-brd);}
.rb-b{background:var(--be-bg);color:var(--be);border:1px solid #f0d060;}
.rb-l{background:var(--loss-bg);color:var(--loss);border:1px solid var(--loss-brd);}

.grid2{display:grid;grid-template-columns:repeat(auto-fill,minmax(480px,1fr));gap:18px;margin-bottom:28px;}
.cbox{background:var(--card);border:1px solid var(--brd);border-radius:12px;
  padding:20px;box-shadow:0 1px 4px rgba(0,0,0,.04);}
.ctitle{font-size:12px;font-weight:700;color:var(--sub);text-transform:uppercase;
  letter-spacing:.12em;margin-bottom:14px;display:flex;align-items:center;gap:12px;}
.ctitle span{color:var(--txt);font-size:16px;font-weight:700;letter-spacing:0;}

.sess-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:16px;margin-bottom:28px;}
.sess-card{
  background:var(--card);border:2px solid var(--brd);border-radius:14px;
  padding:20px 22px;position:relative;overflow:hidden;
  transition:border-color .2s,box-shadow .2s,transform .15s;
  box-shadow:0 1px 4px rgba(0,0,0,.05);
}
.sess-card:hover{transform:translateY(-2px);box-shadow:0 6px 20px rgba(0,0,0,.1);}
.sess-card .sc-bg{position:absolute;top:0;right:0;width:80px;height:80px;
  border-radius:0 14px 0 80px;opacity:.08;}
.sess-name{font-size:18px;font-weight:800;letter-spacing:-.3px;margin-bottom:2px;}
.sess-time{font-size:12px;font-weight:600;color:var(--sub);margin-bottom:14px;}
.sess-r{font-size:32px;font-weight:800;line-height:1;margin-bottom:6px;}
.sess-meta{font-size:13px;color:var(--sub);margin-bottom:10px;}
.sess-bar-track{background:var(--brd);border-radius:4px;height:6px;margin-bottom:12px;}
.sess-bar-fill{height:6px;border-radius:4px;transition:width .4s ease;}
.sess-counts{display:flex;gap:14px;font-size:13px;font-weight:700;}

.hw-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(340px,1fr));gap:18px;margin-bottom:28px;}
.hw-card{background:var(--card);border:1px solid var(--brd);border-radius:12px;padding:20px;box-shadow:0 1px 4px rgba(0,0,0,.04);}
.hw-title{font-size:12px;font-weight:700;letter-spacing:.14em;text-transform:uppercase;margin-bottom:14px;}
.hw-row{display:flex;align-items:center;gap:14px;padding:12px 14px;border-radius:10px;margin-bottom:8px;border:1px solid transparent;transition:border-color .2s;}
.hw-row:hover{border-color:var(--brd2);}
.hw-hour{font-size:30px;font-weight:800;width:72px;line-height:1;}
.hw-hour small{display:block;font-size:11px;font-weight:500;opacity:.6;margin-top:2px;}
.hw-stats{flex:1;}
.hw-r{font-size:19px;font-weight:700;line-height:1;}
.hw-meta{font-size:12px;color:var(--sub);margin-top:4px;}
.hw-bar{height:5px;border-radius:3px;margin-top:8px;}

.month-row{
  display:grid;grid-template-columns:90px 110px 130px 70px 1fr 60px 1fr;
  align-items:center;gap:8px;padding:12px 16px;
  border-bottom:1px solid var(--brd);font-size:14px;
}
.month-row:last-child{border-bottom:none;}
.month-row:hover{background:#f4f6ff;}
.month-bar-pos{height:10px;background:var(--win);border-radius:3px;opacity:.7;}
.month-bar-neg{height:10px;background:var(--loss);border-radius:3px;opacity:.7;}

.score-badge{
  display:inline-flex;align-items:center;justify-content:center;
  width:36px;height:36px;border-radius:50%;font-size:11px;font-weight:800;
  border:2px solid;
}

.hgrid{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:28px;}
.hcell{
  width:54px;height:54px;border-radius:10px;display:flex;flex-direction:column;
  align-items:center;justify-content:center;cursor:default;
  transition:transform .12s,box-shadow .12s;border:1px solid var(--brd);
}
.hcell:hover{transform:scale(1.14);z-index:5;box-shadow:0 4px 12px rgba(0,0,0,.12);}
.hh{font-size:12px;font-weight:700;}
.hr{font-size:9px;margin-top:2px;}

.cal-wrap{margin-bottom:24px;}
.cal-months{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:16px;}
.cal-month{background:var(--card);border:1px solid var(--brd);border-radius:12px;padding:16px;box-shadow:0 1px 3px rgba(0,0,0,.04);}
.cal-month-title{font-size:13px;font-weight:700;color:var(--txt);letter-spacing:.02em;margin-bottom:12px;}
.cal-dow{display:grid;grid-template-columns:repeat(7,1fr);gap:3px;margin-bottom:4px;}
.cal-dow span{font-size:10px;color:var(--sub);text-align:center;padding:2px;font-weight:600;}
.cal-days{display:grid;grid-template-columns:repeat(7,1fr);gap:3px;}
.cal-day{border-radius:6px;display:flex;flex-direction:column;align-items:center;justify-content:center;min-height:36px;padding:3px 2px;cursor:default;transition:transform .1s,box-shadow .1s;}
.cal-day.has-trades{cursor:pointer;}
.cal-day.has-trades:hover{transform:scale(1.08);z-index:5;box-shadow:0 3px 10px rgba(0,0,0,.14);}
.cal-day .dn{font-size:11px;font-weight:700;line-height:1;}
.cal-day .dr{font-size:9px;font-weight:700;line-height:1.3;}
.cal-day .dt{font-size:8px;opacity:.7;line-height:1.2;}
.cal-empty{min-height:36px;}
.cal-detail{background:var(--card);border:1px solid var(--brd2);border-radius:12px;padding:18px 20px;margin-bottom:20px;min-height:60px;box-shadow:0 2px 8px rgba(0,0,0,.06);}

.roll-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:12px;margin-bottom:24px;}
.roll-cell{background:var(--card);border:1px solid var(--brd);border-radius:10px;padding:16px;text-align:center;box-shadow:0 1px 3px rgba(0,0,0,.04);}
.roll-label{font-size:11px;font-weight:700;color:var(--sub);text-transform:uppercase;letter-spacing:.1em;margin-bottom:10px;}
.roll-wr{font-size:26px;font-weight:800;line-height:1;}
.roll-exp{font-size:13px;font-weight:600;margin-top:4px;}
.roll-n{font-size:11px;color:var(--sub);margin-top:3px;}
.roll-rates{display:flex;gap:4px;justify-content:center;margin-top:8px;flex-wrap:wrap;}

.dpanel{background:var(--card);border:1px solid var(--brd);border-radius:12px;padding:24px;margin-bottom:24px;box-shadow:0 1px 4px rgba(0,0,0,.05);}
.dpair{font-size:26px;font-weight:800;color:var(--txt);margin-bottom:18px;letter-spacing:-.5px;}
.dkpis{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:22px;}
.dkpi{background:var(--card2);border:1px solid var(--brd);border-radius:10px;padding:12px 18px;}
.dkpi .v{font-size:20px;font-weight:800;line-height:1;}
.dkpi .l{font-size:10px;font-weight:600;color:var(--sub);text-transform:uppercase;letter-spacing:.1em;margin-top:5px;}
.pbtns{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:22px;}
.pbtn{padding:7px 15px;border-radius:8px;border:1px solid var(--brd);background:var(--card2);color:var(--sub);font-family:var(--ff);font-size:13px;font-weight:600;cursor:pointer;transition:all .15s;}
.pbtn:hover{color:var(--txt);border-color:var(--brd2);background:#fff;}
.pbtn.sel{border-color:var(--acc);background:#eef1fe;color:var(--acc);}

.pat-row{
  display:grid;grid-template-columns:200px 70px 1fr 100px 90px 1fr;
  align-items:center;gap:8px;padding:12px 16px;
  border-bottom:1px solid var(--brd);font-size:14px;
}
.pat-row:last-child{border-bottom:none;}
.pat-row:hover{background:#f4f6ff;}

.dow-grid{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:28px;}
.dow-cell{flex:1;min-width:100px;background:var(--card);border:1px solid var(--brd);border-radius:10px;padding:16px 12px;text-align:center;transition:border-color .2s,box-shadow .2s;box-shadow:0 1px 3px rgba(0,0,0,.04);}
.dow-cell:hover{border-color:var(--brd2);box-shadow:0 4px 10px rgba(0,0,0,.08);transform:translateY(-2px);}
.dow-name{font-size:12px;font-weight:700;color:var(--sub);text-transform:uppercase;letter-spacing:.1em;margin-bottom:10px;}
.dow-r{font-size:22px;font-weight:800;line-height:1;}
.dow-meta{font-size:12px;color:var(--sub);margin-top:5px;}

.rr-dist{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:28px;}
.rr-bucket{flex:1;min-width:70px;background:var(--card);border:1px solid var(--brd);border-radius:10px;padding:14px 10px;text-align:center;box-shadow:0 1px 3px rgba(0,0,0,.04);}
.rr-bucket .bv{font-size:24px;font-weight:800;line-height:1;}
.rr-bucket .bl{font-size:11px;color:var(--sub);margin-top:5px;text-transform:uppercase;letter-spacing:.08em;font-weight:600;}

.corr-table{border-collapse:collapse;font-size:12px;}
.corr-table th{padding:8px 10px;font-size:11px;font-weight:700;color:var(--sub);text-transform:uppercase;letter-spacing:.08em;white-space:nowrap;}
.corr-table td{padding:6px 8px;text-align:center;border:1px solid var(--brd);min-width:54px;font-weight:700;}

.sdc-row{background:var(--card);border:1px solid var(--brd);border-radius:12px;padding:18px 20px;margin-bottom:12px;box-shadow:0 1px 4px rgba(0,0,0,.04);transition:border-color .2s,box-shadow .2s;}
.sdc-row:hover{border-color:var(--brd2);box-shadow:0 3px 10px rgba(0,0,0,.08);}
.sdc-header{display:flex;align-items:center;gap:16px;margin-bottom:14px;flex-wrap:wrap;}
.sdc-date{font-size:15px;font-weight:800;color:var(--txt);min-width:110px;}
.sdc-net{font-size:20px;font-weight:800;line-height:1;}
.sdc-counts{display:flex;gap:10px;font-size:13px;font-weight:700;}
.sdc-pills{display:flex;flex-wrap:wrap;gap:6px;}
.sdc-pill{display:inline-flex;align-items:center;gap:6px;border-radius:8px;padding:6px 12px;font-size:13px;font-weight:600;border:1px solid transparent;}
.sdc-pill.win{background:var(--win-bg);color:var(--win);border-color:var(--win-brd);}
.sdc-pill.loss{background:var(--loss-bg);color:var(--loss);border-color:var(--loss-brd);}
.sdc-pill.be{background:var(--be-bg);color:var(--be);border-color:#f0d060;}
.sdc-streak-badge{display:inline-flex;align-items:center;gap:5px;background:var(--loss-bg);color:var(--loss);border:1px solid var(--loss-brd);border-radius:8px;padding:4px 10px;font-size:12px;font-weight:700;margin-left:auto;}

#tlog-filters{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:16px;align-items:center;}
#tlog-search{
  flex:1;min-width:180px;max-width:260px;
  background:var(--card);border:1px solid var(--brd);border-radius:8px;
  padding:8px 14px;font-family:var(--ff);font-size:13px;color:var(--txt);outline:none;
}
#tlog-search:focus{border-color:var(--acc);}
.fsel{
  background:var(--card);border:1px solid var(--brd);border-radius:8px;
  padding:8px 12px;font-family:var(--ff);font-size:13px;color:var(--txt);outline:none;cursor:pointer;
}
.fsel:focus{border-color:var(--acc);}
#tlog-count{font-size:13px;color:var(--sub);margin-left:auto;}
#export-btn{
  background:var(--acc);color:#fff;border:none;border-radius:8px;
  padding:8px 16px;font-family:var(--ff);font-size:13px;font-weight:700;cursor:pointer;
  transition:opacity .15s;
}
#export-btn:hover{opacity:.85;}
#tlog-table tbody td{cursor:default;}
#tlog-table tbody tr:hover td{background:#f4f6ff;}

.ror-grid{display:grid;grid-template-columns:1fr 1fr;gap:24px;margin-bottom:28px;}
@media(max-width:700px){.ror-grid{grid-template-columns:1fr;}}
.ror-inputs{background:var(--card);border:1px solid var(--brd);border-radius:12px;padding:24px;box-shadow:0 1px 4px rgba(0,0,0,.04);}
.ror-result{background:var(--card);border:1px solid var(--brd);border-radius:12px;padding:24px;box-shadow:0 1px 4px rgba(0,0,0,.04);display:flex;flex-direction:column;align-items:center;justify-content:center;text-align:center;}
.ror-label{font-size:12px;font-weight:700;color:var(--sub);text-transform:uppercase;letter-spacing:.1em;margin-bottom:8px;margin-top:16px;}
.ror-label:first-child{margin-top:0;}
.ror-input{
  width:100%;background:var(--card2);border:1px solid var(--brd);border-radius:8px;
  padding:10px 14px;font-family:var(--ff);font-size:15px;font-weight:700;color:var(--txt);outline:none;
}
.ror-input:focus{border-color:var(--acc);}
.ror-big{font-size:64px;font-weight:800;line-height:1;margin-bottom:8px;}
.ror-sub{font-size:14px;color:var(--sub);}
.ror-table{width:100%;border-collapse:collapse;margin-top:16px;font-size:13px;}
.ror-table th{padding:8px 12px;font-size:10px;font-weight:700;color:var(--sub);text-transform:uppercase;letter-spacing:.1em;border-bottom:2px solid var(--brd);text-align:left;}
.ror-table td{padding:9px 12px;border-bottom:1px solid var(--brd);}

.streak-alert{
  display:flex;align-items:flex-start;gap:12px;
  background:var(--loss-bg);border:1px solid var(--loss-brd);border-radius:10px;
  padding:14px 16px;margin-bottom:12px;
}
.streak-alert-icon{font-size:20px;flex-shrink:0;}
.streak-ok{
  display:flex;align-items:flex-start;gap:12px;
  background:var(--win-bg);border:1px solid var(--win-brd);border-radius:10px;
  padding:14px 16px;margin-bottom:12px;
}

.notes-area{
  width:100%;min-height:120px;background:var(--card2);border:1px solid var(--brd);
  border-radius:10px;padding:14px;font-family:var(--ff);font-size:14px;color:var(--txt);
  outline:none;resize:vertical;transition:border-color .2s;
}
.notes-area:focus{border-color:var(--acc);}
.notes-saved{font-size:12px;color:var(--win);font-weight:600;opacity:0;transition:opacity .3s;}
.notes-saved.show{opacity:1;}
</style>
</head>
<body>

<div id="tz-bar">
  <label>⏱ Timezone</label>
  <select id="tz-select">
    <optgroup label="UTC"><option value="0">UTC +0:00 (UTC)</option></optgroup>
    <optgroup label="Americas">
      <option value="-5">UTC −5:00 (New York EST)</option>
      <option value="-4">UTC −4:00 (New York EDT)</option>
      <option value="-6">UTC −6:00 (Chicago CST)</option>
      <option value="-3">UTC −3:00 (São Paulo BRT)</option>
    </optgroup>
    <optgroup label="Europe / Africa">
      <option value="0">UTC +0:00 (London GMT)</option>
      <option value="1">UTC +1:00 (London BST / Paris CET)</option>
      <option value="2">UTC +2:00 (Paris CEST / Johannesburg SAST)</option>
      <option value="3">UTC +3:00 (Moscow MSK / Nairobi EAT)</option>
    </optgroup>
    <optgroup label="Middle East / Asia">
      <option value="3.5">UTC +3:30 (Tehran IRST)</option>
      <option value="4">UTC +4:00 (Dubai GST)</option>
      <option value="5">UTC +5:00 (Karachi PKT)</option>
      <option value="5.5">UTC +5:30 (India IST)</option>
      <option value="6">UTC +6:00 (Dhaka BST)</option>
      <option value="7">UTC +7:00 (Bangkok ICT)</option>
      <option value="8">UTC +8:00 (Singapore / Hong Kong)</option>
      <option value="9">UTC +9:00 (Tokyo JST / Seoul KST)</option>
    </optgroup>
    <optgroup label="Pacific">
      <option value="10">UTC +10:00 (Sydney AEST)</option>
      <option value="11">UTC +11:00 (Sydney AEDT)</option>
      <option value="12">UTC +12:00 (Auckland NZST)</option>
      <option value="13">UTC +13:00 (Auckland NZDT)</option>
    </optgroup>
  </select>
  <div class="tz-badge"><div class="tz-dot"></div><span id="tz-label">UTC +0:00</span></div>
  <button id="dark-toggle">🌙 Dark Mode</button>
  <div id="tz-info">Hours &amp; sessions re-calculated on change</div>
</div>

<div id="hdr">
  <div>
    <h1>LST BACKTEST REPORT</h1>
    <div class="meta" id="meta-line"></div>
  </div>
  <div class="kpis" id="kpis"></div>
</div>

<div id="nav">
  <button class="ntab on" data-v="overview">Overview</button>
  <button class="ntab"    data-v="tfpairs">TF Pairs</button>
  <button class="ntab"    data-v="monthly">Monthly</button>
  <button class="ntab"    data-v="calendar">Calendar</button>
  <button class="ntab"    data-v="equity">Equity</button>
  <button class="ntab"    data-v="sessions">Sessions</button>
  <button class="ntab"    data-v="hours">Hours</button>
  <button class="ntab"    data-v="patterns">Patterns</button>
  <button class="ntab"    data-v="tradelog">Trade Log</button>
  <button class="ntab"    data-v="correlation">Correlation</button>
  <button class="ntab"    data-v="risk">Risk Calc</button>
  <button class="ntab"    data-v="sameday">Same-Day</button>
  <button class="ntab"    data-v="detail">Pair Detail</button>
</div>

<div id="v-overview"    class="view on"></div>
<div id="v-tfpairs"     class="view"></div>
<div id="v-monthly"     class="view"></div>
<div id="v-calendar"    class="view"></div>
<div id="v-equity"      class="view"></div>
<div id="v-sessions"    class="view"></div>
<div id="v-hours"       class="view"></div>
<div id="v-patterns"    class="view"></div>
<div id="v-tradelog"    class="view"></div>
<div id="v-correlation" class="view"></div>
<div id="v-risk"        class="view"></div>
<div id="v-sameday"     class="view"></div>
<div id="v-detail"      class="view"></div>

<div id="cal-tip" style="display:none;position:fixed;z-index:999;background:var(--card);border:1px solid var(--brd2);border-radius:12px;padding:16px 20px;box-shadow:0 8px 28px rgba(0,0,0,.13);min-width:210px;pointer-events:none;font-size:14px;line-height:1.6"></div>

<script>
const RAW  = __DATA_JSON__;
const D    = RAW.pairs;
const G    = RAW.grand;
const CEQ  = RAW.combined_eq;
const MC   = RAW.monthly_cumulative;
const CD   = RAW.combined_daily;
const PL   = RAW.pairs_list;
const CM   = RAW.corr_matrix;
const TFS  = RAW.tf_pair_stats || {};
const TRADE_COLUMNS = RAW.trade_columns || [];
const MONTHS=['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
const WIN='#0d9e5c',LOSS='#d63b3b',BE='#b07d00',ACC='#4f6ef7',ACC2='#0e8fca',ACC3='#7c5cbf';
const SUB='#5a6480',DIM='#9aa0b4',BRD='#dde1e9';

function tradeVal(t,col){
  const v=t[col];
  return v===null||v===undefined||v===''?'—':v;
}
function fmtTradeVal(t,col){
  const v=tradeVal(t,col);
  if(v==='—') return v;
  if(col==='hit_entry_after_tp1') return v===true||String(v).toLowerCase()==='true'?'true':'false';
  if(['entry','sl','tp1','tp2','htf_high','htf_low','tp_level','ltf_high','ltf_low','raw_entry_price','executed_entry_price','raw_exit_price','executed_exit_price'].includes(col)){
    const n=Number(v); return Number.isFinite(n)?n.toFixed(5):v;
  }
  if(['rr','realized_rr','theoretical_rr','executed_rr','wick_ratio','drawdown_pct_after'].includes(col)){
    const n=Number(v); return Number.isFinite(n)?n.toFixed(4):v;
  }
  if(['balance_before','risk_amount','pnl','balance_after','peak_balance_after','drawdown_after'].includes(col)){
    const n=Number(v); return Number.isFinite(n)?n.toFixed(2):v;
  }
  return v;
}
function tradeCellStyle(t,col){
  if(col==='direction') return `color:${t.direction==='LONG'?WIN:LOSS};font-weight:700`;
  if(col==='outcome') return 'font-weight:700';
  if(['realized_rr','executed_rr','theoretical_rr','pnl'].includes(col)){
    const n=Number(t[col]||0); return `color:${n>=0?WIN:LOSS};font-weight:700`;
  }
  if(['entry_dt','close_dt','pattern','tf_pair','htf_interval','ltf_interval'].includes(col)) return `color:${SUB}`;
  return '';
}
function money(v){
  const n=Number(v||0);
  const sign=n>=0?'+':'-';
  return `${sign}$${Math.abs(n).toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:2})}`;
}
function pct(v){
  const n=Number(v||0);
  return `${n>=0?'+':''}${n.toFixed(2)}%`;
}

// ── TZ + SESSION ──────────────────────────────────────────────────────────────
let TZ_OFFSET = 0;
const SESSION_DEFS = [
  { name:'Sydney',   emoji:'🦘', color:'#7c5cbf', start:21, end:6,  wrap:true  },
  { name:'Tokyo',    emoji:'🗼', color:'#0e8fca', start:0,  end:9,  wrap:false },
  { name:'London',   emoji:'💂', color:'#0d9e5c', start:8,  end:17, wrap:false },
  { name:'New York', emoji:'🗽', color:'#d63b3b', start:13, end:22, wrap:false },
  { name:'Overlap',  emoji:'⚡', color:'#e08c00', start:13, end:17, wrap:false },
];

function shiftHour(utcH, offset) { return ((utcH + Math.round(offset)) % 24 + 24) % 24; }
function getLocalHour(dtStr, offset) {
  if (!dtStr || dtStr.length < 13) return null;
  try {
    const utcH = parseInt(dtStr.substring(11,13));
    const utcM = parseInt(dtStr.substring(14,16)||'0');
    const total = utcH*60 + utcM + Math.round(offset*60);
    return ((Math.floor(total/60))%24+24)%24;
  } catch(e){ return null; }
}
function classifySessions(lh) {
  const out=[];
  SESSION_DEFS.forEach(s=>{
    const inS = s.wrap ? (lh>=s.start||lh<s.end) : (lh>=s.start&&lh<s.end);
    if(inS) out.push(s.name);
  });
  return out.length ? out : ['Off-Hours'];
}
function computeSessionStats(offset) {
  const m={}; SESSION_DEFS.forEach(s=>m[s.name]={w:0,l:0,b:0,r:0,trades:0});
  m['Off-Hours']={w:0,l:0,b:0,r:0,trades:0};
  D.forEach(pair=>(pair.raw_trades||[]).forEach(t=>{
    const lh=getLocalHour(t.entry_dt,offset); if(lh===null) return;
    const sn=classifySessions(lh);
    const isOvlp=sn.includes('London')&&sn.includes('New York');
    const primary=isOvlp?'Overlap':sn[0];
    const s=m[primary]; if(!s) return;
    s.trades++; s.r+=t.realized_rr;
    if(t.outcome==='WIN_FULL')s.w++; else if(t.outcome==='LOSS')s.l++; else s.b++;
  }));
  return m;
}
function computeHourStats(offset) {
  const agg={};
  D.forEach(pair=>(pair.raw_trades||[]).forEach(t=>{
    const lh=getLocalHour(t.entry_dt,offset); if(lh===null) return;
    if(!agg[lh]) agg[lh]={w:0,l:0,b:0,r:0};
    agg[lh].r+=t.realized_rr;
    if(t.outcome==='WIN_FULL')agg[lh].w++; else if(t.outcome==='LOSS')agg[lh].l++; else agg[lh].b++;
  }));
  return agg;
}

// ── HELPERS ───────────────────────────────────────────────────────────────────
const pfStr = v => v==null?'∞':v.toFixed(2);
const rspan = (v,d=2) => `<span style="color:${v>=0?WIN:LOSS};font-weight:700">${v>=0?'+':''}${v.toFixed(d)}R</span>`;
const badge = wr => `<span class="badge ${wr>=50?'g':wr>=35?'y':'r'}">${wr.toFixed(0)}%</span>`;

// Compact three-badge trio: WR% / BE% / L%
function rateTrio(wr, ber, lr) {
  return `<div class="rate-trio">
    <span class="rb rb-w">W ${wr.toFixed(0)}%</span>
    <span class="rb rb-b">BE ${ber.toFixed(0)}%</span>
    <span class="rb rb-l">L ${lr.toFixed(0)}%</span>
  </div>`;
}
// Compute rates from counts
function ratesFromCounts(w, b, l) {
  const cl = w + b + l || 1;
  return { wr: 100*w/cl, ber: 100*b/cl, lr: 100*l/cl };
}

function holdStr(m) {
  if(m==null) return '—';
  return m>=60 ? Math.round(m/60)+'h' : Math.round(m)+'m';
}
function scoreBadge(score) {
  const col = score>=70?WIN : score>=45?BE : LOSS;
  return `<div class="score-badge" style="color:${col};border-color:${col};font-size:${score>=100?9:11}px">${score}</div>`;
}

// ── HEADER ────────────────────────────────────────────────────────────────────
document.getElementById('meta-line').textContent=`${RAW.source}  ·  ${RAW.generated}`;
[
  [G.total_r>=0?'+'+G.total_r.toFixed(2)+'R':G.total_r.toFixed(2)+'R','Total R',G.total_r>=0?WIN:LOSS],
  [G.wr.toFixed(1)+'%','Win Rate',G.wr>=50?WIN:LOSS],
  [G.be_rate.toFixed(1)+'%','BE Rate',BE],
  [G.loss_rate.toFixed(1)+'%','Loss Rate',LOSS],
  [pfStr(G.pf),'Prof Factor',G.pf==null||G.pf>=1?WIN:LOSS],
  [(G.exp>=0?'+':'')+G.exp.toFixed(3)+'R','Expect/T',G.exp>=0?WIN:LOSS],
  [G.trades,'Trades',ACC],[D.length,'Pairs',ACC2],[MC.length,'Months',ACC3],
  ['-'+G.max_dd.toFixed(2)+'R','Max DD',LOSS],
].forEach(([v,l,c])=>{
  const el=document.createElement('div'); el.className='kpi';
  el.innerHTML=`<div class="kv" style="color:${c}">${v}</div><div class="kl">${l}</div>`;
  document.getElementById('kpis').appendChild(el);
});

// ── TZ SELECTOR ───────────────────────────────────────────────────────────────
const tzSel=document.getElementById('tz-select');
const tzLbl=document.getElementById('tz-label');
tzSel.addEventListener('change',()=>{
  TZ_OFFSET=parseFloat(tzSel.value);
  tzLbl.textContent=tzSel.options[tzSel.selectedIndex].text.split('(')[0].trim();
  renderSessions(); renderHours();
  if(document.getElementById('v-detail').classList.contains('on')) renderDetail(_curDetail);
});

// ── DARK MODE ─────────────────────────────────────────────────────────────────
const darkBtn=document.getElementById('dark-toggle');
darkBtn.addEventListener('click',()=>{
  document.body.classList.toggle('dark');
  darkBtn.textContent=document.body.classList.contains('dark')?'☀️ Light Mode':'🌙 Dark Mode';
  try{ localStorage.setItem('lst_dark', document.body.classList.contains('dark')?'1':'0'); } catch(e){}
});
try{ if(localStorage.getItem('lst_dark')==='1'){ document.body.classList.add('dark'); darkBtn.textContent='☀️ Light Mode'; } }catch(e){}

// ── NAV ───────────────────────────────────────────────────────────────────────
let _curDetail=0;
document.querySelectorAll('.ntab').forEach(t=>t.addEventListener('click',()=>{
  document.querySelectorAll('.ntab').forEach(x=>x.classList.remove('on'));
  document.querySelectorAll('.view').forEach(x=>x.classList.remove('on'));
  t.classList.add('on');
  document.getElementById('v-'+t.dataset.v).classList.add('on');
}));

// ════════════════════════════════════════════════════════════════════════
// OVERVIEW
// ════════════════════════════════════════════════════════════════════════
(function(){
  const el=document.getElementById('v-overview');
  el.innerHTML=`
  <div class="sh">Streak Alerts</div>
  <div id="streak-alerts" style="margin-bottom:24px"></div>
  <div class="sh">All Pairs — click any row to open detail</div>
  <div class="tbl-wrap"><table id="ov-table">
    <thead><tr>
      <th data-col="pair">Pair</th><th data-col="symbol">Symbol</th><th data-col="tf_pair">TF</th><th data-col="trades">Trades</th>
      <th data-col="wins">W</th><th data-col="bes">BE</th><th data-col="losses">L</th>
      <th data-col="wr">W%</th><th data-col="be_rate">BE%</th><th data-col="loss_rate">L%</th>
      <th data-col="total_r">Total R</th>
      <th data-col="net_pnl">Net PnL</th><th data-col="net_pnl_pct">Net %</th><th data-col="max_dd_dollar">DD $</th>
      <th data-col="exp">Expect/T</th><th data-col="pf">PF</th>
      <th data-col="max_dd">Max DD</th><th data-col="best_rr">Best</th>
      <th data-col="worst_rr">Worst</th><th data-col="avg_hold_min">Avg Hold</th>
      <th data-col="max_loss_streak">L Streak</th>
    </tr></thead>
    <tbody id="ov-body"></tbody>
  </table></div>
  <div class="sh">Combined Equity Curve</div>
  <div class="cbox" style="margin-bottom:28px"><canvas id="cv-combined" height="200"></canvas></div>
  <div class="sh">Combined RR Distribution</div>
  <div id="rr-dist-all" class="rr-dist"></div>`;

  const sa=document.getElementById('streak-alerts');
  let hasAlerts=false;
  D.forEach(d=>{
    if(d.streak_alert){
      hasAlerts=true;
      sa.innerHTML+=`<div class="streak-alert">
        <div class="streak-alert-icon">⚠️</div>
        <div>
          <div style="font-weight:800;font-size:14px;color:var(--loss)">${d.pair} — Unusual Loss Streak</div>
          <div style="font-size:13px;color:var(--sub);margin-top:3px">
            Max observed streak: <strong>${d.max_loss_streak}</strong> ·
            Statistically expected max: <strong>~${d.expected_max_loss_streak}</strong> for WR ${d.wr.toFixed(0)}%.<br>
            This streak is >2× expected — consider reviewing whether the edge is still intact.
          </div>
        </div>
      </div>`;
    }
  });
  if(!hasAlerts){
    sa.innerHTML=`<div class="streak-ok"><div class="streak-alert-icon">✅</div>
      <div style="font-weight:700;color:var(--win);font-size:14px">No unusual loss streaks detected across all pairs.</div></div>`;
  }

  let sortCol='pair', sortDir=1;
  function renderTable(){
    const sorted=[...D].sort((a,b)=>{
      let av=a[sortCol]??0, bv=b[sortCol]??0;
      if(['net_pnl','net_pnl_pct','max_dd_dollar'].includes(sortCol)){
        av=a.dollar_stats?.[sortCol==='max_dd_dollar'?'max_drawdown_dollar':sortCol]??0;
        bv=b.dollar_stats?.[sortCol==='max_dd_dollar'?'max_drawdown_dollar':sortCol]??0;
      }
      if(typeof av==='string') return sortDir*(av.localeCompare(bv));
      return sortDir*(av-bv);
    });
    const tb=document.getElementById('ov-body'); tb.innerHTML='';
    sorted.forEach((d,i)=>{
      const tr=document.createElement('tr');
      tr.innerHTML=`
        <td style="font-weight:700">${d.pair}${d.streak_alert?'<span style="color:var(--loss);margin-left:4px">⚠</span>':''}</td>
        <td style="font-weight:600;color:${SUB}">${d.symbol||d.pair}</td>
        <td style="font-family:monospace;font-size:12px;color:${ACC}">${d.tf_pair||'—'}</td>
        <td style="font-weight:600">${d.trades}</td>
        <td style="color:${WIN};font-weight:700">${d.wins}</td>
        <td style="color:${BE};font-weight:600">${d.bes}</td>
        <td style="color:${LOSS};font-weight:700">${d.losses}</td>
        <td>${badge(d.wr)}</td>
        <td><span class="badge y">${(d.be_rate||0).toFixed(0)}%</span></td>
        <td><span class="badge r">${(d.loss_rate||0).toFixed(0)}%</span></td>
        <td>${rspan(d.total_r)}</td>
        <td style="color:${d.dollar_stats?(d.dollar_stats.net_pnl>=0?WIN:LOSS):SUB};font-weight:700">${d.dollar_stats?money(d.dollar_stats.net_pnl):'—'}</td>
        <td style="color:${d.dollar_stats?(d.dollar_stats.net_pnl_pct>=0?WIN:LOSS):SUB};font-weight:700">${d.dollar_stats?pct(d.dollar_stats.net_pnl_pct):'—'}</td>
        <td style="color:${d.dollar_stats?LOSS:SUB};font-weight:600">${d.dollar_stats?'-$'+Number(d.dollar_stats.max_drawdown_dollar||0).toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:2}):'—'}</td>
        <td>${rspan(d.exp,3)}</td>
        <td style="color:${d.pf==null||d.pf>=1?WIN:LOSS};font-weight:700">${pfStr(d.pf)}</td>
        <td style="color:${LOSS};font-weight:600">-${d.max_dd.toFixed(2)}R</td>
        <td style="color:${WIN};font-weight:600">+${d.best_rr.toFixed(2)}R</td>
        <td style="color:${LOSS};font-weight:600">${d.worst_rr.toFixed(2)}R</td>
        <td style="color:${SUB}">${holdStr(d.avg_hold_min)}</td>
        <td style="color:${d.max_loss_streak>=4?LOSS:SUB};font-weight:${d.max_loss_streak>=4?700:400}">${d.max_loss_streak}</td>`;
      tr.style.cursor='pointer';
      tr.addEventListener('click',()=>showDetail(D.indexOf(d)));
      tb.appendChild(tr);
    });
  }
  renderTable();
  document.querySelectorAll('#ov-table thead th').forEach(th=>{
    th.addEventListener('click',()=>{
      const col=th.dataset.col;
      if(sortCol===col) sortDir*=-1; else { sortCol=col; sortDir=-1; }
      document.querySelectorAll('#ov-table thead th').forEach(x=>x.classList.remove('sort-asc','sort-desc'));
      th.classList.add(sortDir===1?'sort-asc':'sort-desc');
      renderTable();
    });
  });

  const cb={};
  D.forEach(d=>Object.entries(d.rr_buckets).forEach(([k,v])=>cb[k]=(cb[k]||0)+v));
  renderRRDist('rr-dist-all',cb);
  setTimeout(()=>drawLine('cv-combined',CEQ,G.total_r>=0?WIN:LOSS,200),80);
})();

function renderRRDist(id,buckets){
  const el=document.getElementById(id); if(!el) return; el.innerHTML='';
  const total=Object.values(buckets).reduce((a,b)=>a+b,0)||1;
  Object.entries(buckets).forEach(([k,v])=>{
    const col=k.includes('-')||k==='<-1'?LOSS:k==='0'?BE:WIN;
    const div=document.createElement('div'); div.className='rr-bucket';
    div.style.borderColor=col+'33';
    div.innerHTML=`<div class="bv" style="color:${col}">${v}</div>
      <div class="bl">${k}R</div>
      <div style="font-size:10px;color:${SUB};margin-top:3px">${(v/total*100).toFixed(0)}%</div>`;
    el.appendChild(div);
  });
}

// ════════════════════════════════════════════════════════════════════════
// MONTHLY
// ════════════════════════════════════════════════════════════════════════
(function(){
  const el=document.getElementById('v-monthly');
  const maxAbsR=Math.max(...MC.map(m=>Math.abs(m.r)),1);
  el.innerHTML=`
  <div class="sh">Monthly Net R</div>
  <div class="cbox" style="margin-bottom:24px">
    <div class="ctitle">Monthly Bars <span id="mc-total"></span></div>
    <canvas id="cv-monthly" height="220"></canvas>
  </div>
  <div class="sh">Cumulative R</div>
  <div class="cbox" style="margin-bottom:24px">
    <div class="ctitle">Running Total <span id="mc-cum"></span></div>
    <canvas id="cv-monthly-cum" height="180"></canvas>
  </div>
  <div class="sh">Monthly Breakdown</div>
  <div style="background:var(--card);border:1px solid var(--brd);border-radius:12px;overflow:hidden;margin-bottom:24px">
    <div class="month-row" style="background:var(--card2);font-size:9px;color:var(--sub);font-weight:700;letter-spacing:.12em;text-transform:uppercase;border-bottom:2px solid var(--brd)">
      <div>Month</div><div>Net R</div><div>Cumulative R</div><div>Trades</div><div>W% / BE% / L%</div><div>Score</div><div>Bar</div>
    </div>
    <div id="month-rows"></div>
  </div>`;

  document.getElementById('mc-total').textContent=(MC.reduce((s,m)=>s+m.r,0)>=0?'+':'')+MC.reduce((s,m)=>s+m.r,0).toFixed(2)+'R total';
  document.getElementById('mc-cum').textContent='peak '+(Math.max(...MC.map(m=>m.cum_r))>=0?'+':'')+Math.max(...MC.map(m=>m.cum_r)).toFixed(2)+'R';

  const mRows=document.getElementById('month-rows');
  MC.forEach(m=>{
    const div=document.createElement('div'); div.className='month-row';
    const bw=Math.round(Math.abs(m.r)/maxAbsR*220);
    const bar=m.r>=0?`<div class="month-bar-pos" style="width:${bw}px"></div>`
                     :`<div class="month-bar-neg" style="width:${bw}px"></div>`;
    const allScores=D.map(d=>(d.monthly||{})[m.ym]?.score||0).filter(s=>s>0);
    const avgScore=allScores.length?Math.round(allScores.reduce((a,b)=>a+b,0)/allScores.length):0;
    div.innerHTML=`
      <div style="font-weight:700">${m.ym}</div>
      <div style="color:${m.r>=0?WIN:LOSS};font-weight:700">${m.r>=0?'+':''}${m.r.toFixed(2)}R</div>
      <div style="color:${m.cum_r>=0?WIN:LOSS};font-weight:600">${m.cum_r>=0?'+':''}${m.cum_r.toFixed(2)}R</div>
      <div style="color:${SUB}">${m.trades}</div>
      <div>${rateTrio(m.wr||0, m.be_rate||0, m.loss_rate||0)}</div>
      <div>${scoreBadge(avgScore)}</div>
      <div>${bar}</div>`;
    mRows.appendChild(div);
  });

  setTimeout(()=>{
    drawBars('cv-monthly',MC.map(m=>m.ym.slice(5)+'/'+m.ym.slice(2,4)),MC.map(m=>m.r),220);
    drawLine('cv-monthly-cum',MC.map(m=>m.cum_r),MC[MC.length-1]?.cum_r>=0?WIN:LOSS,180);
  },80);
})();

// ════════════════════════════════════════════════════════════════════════
// CALENDAR
// ════════════════════════════════════════════════════════════════════════
(function(){
  const el=document.getElementById('v-calendar');
  const tip=document.getElementById('cal-tip');
  function dbg(r,n){ if(!n) return '#f0f2f5'; if(r>6) return '#b8ecd4'; if(r>2) return '#d4f0e4'; if(r>0) return '#eaf8f1'; if(r<-2) return '#f7cece'; if(r<0) return '#fdeaea'; return '#f7f8fa'; }
  function dfg(r,n){ if(!n) return DIM; if(r>2) return '#0a7a44'; if(r>0) return '#0d8a4e'; if(r<-1) return '#a01818'; if(r<0) return '#b83030'; return SUB; }

  el.innerHTML=`
  <div class="sh">Daily P&amp;L Calendar — hover to preview · click to expand</div>
  <div class="cal-detail" id="cal-detail"><div style="color:var(--sub);font-size:14px">Click any trade day to see its breakdown.</div></div>
  <div style="display:flex;align-items:center;gap:18px;margin-bottom:20px;flex-wrap:wrap;font-size:13px;color:var(--sub)">
    <div style="display:flex;align-items:center;gap:6px"><div style="width:18px;height:18px;border-radius:4px;background:#b8ecd4;border:1px solid #9edfc2"></div>&gt;2R profit</div>
    <div style="display:flex;align-items:center;gap:6px"><div style="width:18px;height:18px;border-radius:4px;background:#eaf8f1;border:1px solid #b6e8d0"></div>Profit</div>
    <div style="display:flex;align-items:center;gap:6px"><div style="width:18px;height:18px;border-radius:4px;background:#fdeaea;border:1px solid #f5b8b8"></div>Loss</div>
    <div style="display:flex;align-items:center;gap:6px"><div style="width:18px;height:18px;border-radius:4px;background:#f7cece;border:1px solid #f0a0a0"></div>&lt;-2R loss</div>
  </div>
  <div class="cal-wrap" id="cal-wrap"></div>`;

  const wrap=document.getElementById('cal-wrap');
  const detEl=document.getElementById('cal-detail');
  const allDates=Object.keys(CD).sort();
  if(!allDates.length){ wrap.innerHTML='<p style="color:var(--sub)">No entry_dt data.</p>'; return; }
  const years=[...new Set(allDates.map(d=>d.slice(0,4)))].sort();

  years.forEach(yr=>{
    const ydiv=document.createElement('div'); ydiv.style.marginBottom='22px';
    ydiv.innerHTML=`<div style="font-size:14px;font-weight:700;color:var(--sub);text-transform:uppercase;letter-spacing:.1em;margin-bottom:14px">${yr}</div><div class="cal-months" id="cal-${yr}"></div>`;
    wrap.appendChild(ydiv);
    const mWrap=document.getElementById('cal-'+yr);
    for(let mo=0;mo<12;mo++){
      const ym=`${yr}-${String(mo+1).padStart(2,'0')}`;
      if(!allDates.some(d=>d.startsWith(ym))) continue;
      const mDiv=document.createElement('div'); mDiv.className='cal-month';
      mDiv.innerHTML=`<div class="cal-month-title">${MONTHS[mo]} ${yr}</div>
        <div class="cal-dow">${['M','T','W','T','F','S','S'].map(d=>`<span>${d}</span>`).join('')}</div>
        <div class="cal-days" id="cd-${yr}-${mo}"></div>`;
      mWrap.appendChild(mDiv);
      const grid=document.getElementById(`cd-${yr}-${mo}`);
      const off=(new Date(yr,mo,1).getDay()+6)%7;
      const dim=new Date(yr,mo+1,0).getDate();
      for(let e=0;e<off;e++){ const x=document.createElement('div'); x.className='cal-empty'; grid.appendChild(x); }
      for(let d=1;d<=dim;d++){
        const ds=`${yr}-${String(mo+1).padStart(2,'0')}-${String(d).padStart(2,'0')}`;
        const day=CD[ds];
        const cell=document.createElement('div');
        cell.className='cal-day'+(day?' has-trades':'');
        if(!day){
          cell.style.background='#f0f2f5';
          cell.innerHTML=`<span class="dn" style="color:${DIM}">${d}</span>`;
        } else {
          const val=day.r, tot=day.w+day.b+day.l;
          cell.style.cssText=`background:${dbg(val,1)};border:1px solid ${val>=0?'#b6e8d0':'#f5b8b8'}`;
          cell.innerHTML=`<span class="dn" style="color:${dfg(val,1)}">${d}</span>
            <span class="dr" style="color:${dfg(val,1)}">${(val>=0?'+':'')+val.toFixed(1)}R</span>
            <span class="dt" style="color:${dfg(val,1)}">${tot}t</span>`;
          cell.addEventListener('mouseenter',()=>{
            tip.innerHTML=`<div style="font-size:11px;font-weight:700;color:var(--sub);margin-bottom:6px">${ds}</div>
              <div style="font-size:24px;font-weight:800;color:${val>=0?WIN:LOSS};margin-bottom:10px">${(val>=0?'+':'')+val.toFixed(2)}R</div>
              <div style="display:flex;gap:14px;font-size:14px;font-weight:700">
                <span style="color:${WIN}">W: ${day.w}</span><span style="color:${BE}">BE: ${day.b}</span><span style="color:${LOSS}">L: ${day.l}</span>
              </div><div style="font-size:12px;color:var(--sub);margin-top:6px">${tot} trade${tot!==1?'s':''}</div>`;
            tip.style.display='block';
          });
          cell.addEventListener('mousemove',e=>{
            const x=e.clientX+16,y=e.clientY-10;
            tip.style.left=(x+240>window.innerWidth?x-260:x)+'px'; tip.style.top=y+'px';
          });
          cell.addEventListener('mouseleave',()=>tip.style.display='none');
          cell.addEventListener('click',()=>{
            const rc=val>=0?WIN:LOSS;
            const r2=ratesFromCounts(day.w,day.b,day.l);
            let pillsHtml=(day.pairs||[]).map(t=>{
              const cls=t.outcome==='WIN_FULL'?'win':t.outcome==='LOSS'?'loss':'be';
              return `<div class="tpill ${cls}">${t.dir==='LONG'?'↑':'↓'} ${t.pair} ${t.entry} ${(t.rr>=0?'+':'')+t.rr.toFixed(2)}R</div>`;
            }).join('');
            detEl.innerHTML=`<div style="font-size:17px;font-weight:800;margin-bottom:10px">${ds}
              <span style="color:${rc};margin-left:12px">${(val>=0?'+':'')+val.toFixed(2)}R</span>
              <span style="font-size:13px;color:var(--sub);margin-left:8px;font-weight:400">${tot} trades</span></div>
              <div style="display:flex;gap:12px;align-items:center;margin-bottom:10px">
                ${rateTrio(r2.wr,r2.ber,r2.lr)}
                <span style="color:${WIN};font-size:14px;font-weight:700">✓ ${day.w} W</span>
                <span style="color:${BE};font-size:14px;font-weight:700">≈ ${day.b} BE</span>
                <span style="color:${LOSS};font-size:14px;font-weight:700">✕ ${day.l} L</span>
              </div><div style="display:flex;flex-wrap:wrap;gap:6px">${pillsHtml}</div>`;
            detEl.scrollIntoView({behavior:'smooth',block:'nearest'});
          });
        }
        grid.appendChild(cell);
      }
    }
  });
})();

// ════════════════════════════════════════════════════════════════════════
// EQUITY
// ════════════════════════════════════════════════════════════════════════
(function(){
  const el=document.getElementById('v-equity');
  el.innerHTML='<div class="sh">Individual Equity Curves</div><div class="grid2" id="eq-grid"></div>';
  const grid=document.getElementById('eq-grid');
  D.forEach((d,i)=>{
    const col=d.total_r>=0?WIN:LOSS;
    const box=document.createElement('div'); box.className='cbox';
    box.innerHTML=`<div class="ctitle">${d.pair}
      <span style="color:${col}">${d.total_r>=0?'+':''}${d.total_r.toFixed(2)}R</span>
      <span style="color:${SUB};font-size:11px">WR ${d.wr.toFixed(0)}% · BE ${(d.be_rate||0).toFixed(0)}% · L ${(d.loss_rate||0).toFixed(0)}% · PF ${pfStr(d.pf)}</span>
    </div><canvas id="eq-${i}" height="150"></canvas>`;
    grid.appendChild(box);
  });
  setTimeout(()=>D.forEach((d,i)=>drawLine('eq-'+i,d.equity,d.total_r>=0?WIN:LOSS,150)),80);
})();

// ════════════════════════════════════════════════════════════════════════
// SESSIONS
// ════════════════════════════════════════════════════════════════════════
function renderSessions(){
  const el=document.getElementById('v-sessions');
  const ss=computeSessionStats(TZ_OFFSET);
  const maxAbsR=Math.max(...Object.values(ss).map(v=>Math.abs(v.r)),1);
  const ORDER=['Sydney','Tokyo','London','New York','Overlap','Off-Hours'];

  const cards=ORDER.map(name=>{
    const def=SESSION_DEFS.find(s=>s.name===name)||{name,emoji:'🕐',color:SUB};
    const s=ss[name]||{w:0,l:0,b:0,r:0,trades:0};
    if(!s.trades) return '';
    const ls=name==='Off-Hours'?null:((def.start+Math.round(TZ_OFFSET))+24)%24;
    const le=name==='Off-Hours'?null:((def.end  +Math.round(TZ_OFFSET))+24)%24;
    const timeStr=name==='Off-Hours'?'Outside main sessions':`${String(ls).padStart(2,'0')}:00 – ${String(le).padStart(2,'0')}:00 local`;
    const r2=ratesFromCounts(s.w,s.b,s.l);
    const bw=Math.round(Math.abs(s.r)/maxAbsR*100);
    const col=def.color; const rc=s.r>=0?WIN:LOSS;
    const exp=s.trades?(s.r/s.trades).toFixed(3):'0.000';
    return `<div class="sess-card" style="border-color:${col}44">
      <div class="sc-bg" style="background:${col}"></div>
      <div class="sess-name" style="color:${col}">${def.emoji} ${name}</div>
      <div class="sess-time">${timeStr}</div>
      <div class="sess-r" style="color:${rc}">${s.r>=0?'+':''}${s.r.toFixed(2)}R</div>
      <div class="sess-meta">${s.trades} trades · ${exp>=0?'+':''}${exp}R/T</div>
      <div style="margin-bottom:10px">${rateTrio(r2.wr,r2.ber,r2.lr)}</div>
      <div class="sess-bar-track"><div class="sess-bar-fill" style="width:${bw}%;background:${rc}"></div></div>
      <div class="sess-counts"><span style="color:${WIN}">W ${s.w}</span><span style="color:${BE}">BE ${s.b}</span><span style="color:${LOSS}">L ${s.l}</span></div>
    </div>`;
  }).join('');

  const hs=computeHourStats(TZ_OFFSET);
  const heatmap=Array.from({length:24},(_,h)=>{
    const s=hs[h]||{w:0,l:0,b:0,r:0}; const n=s.w+s.l+s.b; const lr=n?s.l/n:0;
    const sn=classifySessions(h); const ms=SESSION_DEFS.find(sd=>sn.includes(sd.name));
    const bg=n===0?'#f0f2f5':lr>=.6?'#fde8e8':lr<=.3?'#e8f7f0':'#f7f8fa';
    const fc=n===0?DIM:lr>=.6?LOSS:lr<=.3&&n>0?WIN:SUB;
    const bc=ms?ms.color+'55':BRD;
    const dot=ms?`<div style="width:6px;height:6px;border-radius:50%;background:${ms.color};margin-bottom:2px"></div>`:'';
    return `<div class="hcell" style="background:${bg};border-color:${bc}"
      title="${String(h).padStart(2,'0')}:00 · ${sn.join('/')} · W${s.w} BE${s.b} L${s.l} · ${n?(s.r>=0?'+':'')+s.r.toFixed(2):0}R">
      ${dot}<div class="hh" style="color:${fc}">${String(h).padStart(2,'0')}</div>
      <div class="hr" style="color:${fc}">${n?(s.r>=0?'+':'')+s.r.toFixed(1)+'R':'—'}</div></div>`;
  }).join('');

  const legend=SESSION_DEFS.map(s=>{
    const ls=((s.start+Math.round(TZ_OFFSET))+24)%24;
    const le=((s.end  +Math.round(TZ_OFFSET))+24)%24;
    return `<div style="display:flex;align-items:center;gap:6px;font-size:12px;color:var(--sub)">
      <div style="width:12px;height:12px;border-radius:3px;background:${s.color};opacity:.7"></div>
      <strong style="color:${s.color}">${s.name}</strong> ${String(ls).padStart(2,'0')}:00–${String(le).padStart(2,'0')}:00</div>`;
  }).join('');

  const tableRows=ORDER.filter(n=>(ss[n]||{trades:0}).trades>0).map(name=>{
    const def=SESSION_DEFS.find(s=>s.name===name)||{color:SUB,emoji:'🕐'};
    const s=ss[name]||{w:0,l:0,b:0,r:0,trades:0};
    const r2=ratesFromCounts(s.w,s.b,s.l);
    const exp=s.trades?s.r/s.trades:0;
    return `<div style="display:grid;grid-template-columns:150px 70px 70px 70px 70px 100px 100px 1fr;gap:0;padding:13px 16px;border-bottom:1px solid var(--brd);font-size:14px;align-items:center">
      <div style="font-weight:700;color:${def.color}">${def.emoji} ${name}</div>
      <div style="font-weight:600">${s.trades}</div>
      <div style="color:${WIN};font-weight:700">${s.w}</div>
      <div style="color:${BE};font-weight:600">${s.b}</div>
      <div style="color:${LOSS};font-weight:700">${s.l}</div>
      <div style="color:${s.r>=0?WIN:LOSS};font-weight:700">${s.r>=0?'+':''}${s.r.toFixed(2)}R</div>
      <div style="color:${exp>=0?WIN:LOSS};font-weight:600">${exp>=0?'+':''}${exp.toFixed(3)}R</div>
      <div>${rateTrio(r2.wr,r2.ber,r2.lr)}</div></div>`;
  }).join('');

  const tzOpt=tzSel.options[tzSel.selectedIndex].text;
  el.innerHTML=`
  <div style="background:#1a1f2e;border-radius:12px;padding:14px 20px;margin-bottom:24px;display:flex;align-items:center;gap:16px;flex-wrap:wrap">
    <div style="font-size:13px;font-weight:700;color:#9aa0b4;text-transform:uppercase;letter-spacing:.1em">Active timezone:</div>
    <div style="font-size:16px;font-weight:800;color:#e8eaf0">${tzOpt}</div>
    <div style="font-size:12px;color:#6a7090;margin-left:auto">Change timezone in the bar above ↑</div>
  </div>
  <div class="sh">Forex Session Performance (local time)</div>
  <div class="sess-grid">${cards}</div>
  <div class="sh">Hour Heatmap (local time)</div>
  <div style="display:flex;gap:12px;flex-wrap:wrap;margin-bottom:14px">${legend}</div>
  <div class="hgrid">${heatmap}</div>
  <div class="sh">Net R by Session</div>
  <div class="cbox" style="margin-bottom:24px"><canvas id="cv-sess-bar" height="220"></canvas></div>
  <div class="sh">Session Comparison Table</div>
  <div style="background:var(--card);border:1px solid var(--brd);border-radius:12px;overflow:hidden;margin-bottom:28px">
    <div style="display:grid;grid-template-columns:150px 70px 70px 70px 70px 100px 100px 1fr;gap:0;padding:12px 16px;background:var(--card2);font-size:10px;font-weight:700;color:var(--sub);text-transform:uppercase;letter-spacing:.1em;border-bottom:2px solid var(--brd)">
      <div>Session</div><div>Trades</div><div>W</div><div>BE</div><div>L</div><div>Net R</div><div>Expect/T</div><div>W% / BE% / L%</div>
    </div>${tableRows}
  </div>`;

  const visNames=ORDER.filter(n=>(ss[n]||{trades:0}).trades>0);
  const visVals=visNames.map(n=>+((ss[n]||{r:0}).r.toFixed(2)));
  const visCols=visNames.map(n=>(SESSION_DEFS.find(s=>s.name===n)||{color:SUB}).color);
  setTimeout(()=>drawCustomBars('cv-sess-bar',visNames,visVals,visCols,220),80);
}

// ════════════════════════════════════════════════════════════════════════
// HOURS
// ════════════════════════════════════════════════════════════════════════
function renderHours(){
  const el=document.getElementById('v-hours');
  const agg=computeHourStats(TZ_OFFSET);
  const qual=Object.entries(agg).filter(([,s])=>(s.w+s.l+s.b)>=2).sort((a,b)=>b[1].r-a[1].r);
  const bestH=qual.slice(0,3); const worstH=qual.slice(-3).reverse();
  const maxAbsR=Math.max(...Object.values(agg).map(s=>Math.abs(s.r)),1);
  const tzOpt=tzSel.options[tzSel.selectedIndex].text.split('(')[0].trim();

  function hwCard(title,hours,col){
    const rows=(hours||[]).map(([h,s],i)=>{
      const n=s.w+s.l+s.b;
      const r2=ratesFromCounts(s.w,s.b,s.l);
      const bw=Math.round(Math.abs(s.r)/maxAbsR*100);
      const sn=classifySessions(parseInt(h));
      return `<div class="hw-row" style="background:${col}11;border-color:${col}22">
        <div class="hw-hour" style="color:${col}">${String(h).padStart(2,'0')}<small>:00 local</small></div>
        <div class="hw-stats">
          <div class="hw-r" style="color:${col}">${s.r>=0?'+':''}${s.r.toFixed(2)}R</div>
          <div style="margin:5px 0">${rateTrio(r2.wr,r2.ber,r2.lr)}</div>
          <div class="hw-meta">${n} trades · W${s.w} BE${s.b} L${s.l}</div>
          <div style="font-size:11px;color:var(--sub);margin-top:3px">${sn.join(' / ')}</div>
          <div class="hw-bar" style="width:${bw}%;background:${col};opacity:.7"></div>
        </div></div>`;
    }).join('');
    return `<div class="hw-card"><div class="hw-title" style="color:${col}">${title}</div>${rows}</div>`;
  }

  el.innerHTML=`
  <div class="sh">Best & Worst Hours — all pairs · local time (${tzOpt})</div>
  <div class="hw-grid">${hwCard('🟢 Best Hours by Net R',bestH,WIN)}${hwCard('🔴 Worst Hours by Net R',worstH,LOSS)}</div>
  <div class="sh">Loss Rate Heatmap (local time)</div>
  <div class="hgrid" id="hmap2"></div>
  <div class="sh">Net R by Local Hour</div>
  <div class="cbox"><canvas id="cv-hours2" height="220"></canvas></div>`;

  const hmap=document.getElementById('hmap2');
  for(let h=0;h<24;h++){
    const s=agg[h]||{w:0,l:0,b:0,r:0}; const n=s.w+s.l+s.b; const lr=n?s.l/n:0;
    const sn=classifySessions(h); const ms=SESSION_DEFS.find(sd=>sn.includes(sd.name));
    const bg=lr>=.70?'#fde8e8':lr>=.55?'#fef0f0':lr>=.40?'#fff5f5':lr<=.25&&n>=2?'#e8f7f0':'#f7f8fa';
    const fc=lr>=.55?LOSS:lr<=.25&&n>=2?WIN:SUB;
    const bc=ms?ms.color+'55':BRD;
    const dot=ms?`<div style="width:6px;height:6px;border-radius:50%;background:${ms.color};margin-bottom:2px"></div>`:'';
    const c=document.createElement('div'); c.className='hcell'; c.style.cssText=`background:${bg};border-color:${bc}`;
    c.innerHTML=`${dot}<div class="hh" style="color:${fc}">${String(h).padStart(2,'0')}</div>
      <div class="hr" style="color:${fc}">${n?(s.r>=0?'+':'')+s.r.toFixed(1)+'R':'—'}</div>`;
    c.title=`${String(h).padStart(2,'0')}:00 · ${sn.join('/')} · W${s.w} BE${s.b} L${s.l} · Net ${(s.r>=0?'+':'')+s.r.toFixed(2)}R · Loss ${n?(lr*100).toFixed(0):0}%`;
    hmap.appendChild(c);
  }
  const hrs=[...Array(24).keys()];
  setTimeout(()=>drawBars('cv-hours2',hrs.map(h=>String(h).padStart(2,'0')),hrs.map(h=>agg[h]?+agg[h].r.toFixed(2):0),220),80);
}

// ════════════════════════════════════════════════════════════════════════
// PATTERNS & DOW (MODIFIED with streak analysis)
// ════════════════════════════════════════════════════════════════════════
(function(){
  const el=document.getElementById('v-patterns');
  const patAgg={};
  const patStreaks={}; // Store streaks per pattern
  
  D.forEach(d=>{
    // Track streaks for each pattern
    const patternStreaks = {};
    let currentStreak = {type: null, count: 0};
    
    (d.raw_trades||[]).forEach(t=>{
      const pat = t.pattern || 'UNKNOWN';
      if(!patternStreaks[pat]) patternStreaks[pat] = {maxWin:0, maxLoss:0, currentWin:0, currentLoss:0};
      
      if(t.outcome === 'WIN_FULL'){
        patternStreaks[pat].currentWin++;
        patternStreaks[pat].currentLoss = 0;
        if(patternStreaks[pat].currentWin > patternStreaks[pat].maxWin){
          patternStreaks[pat].maxWin = patternStreaks[pat].currentWin;
        }
      } else if(t.outcome === 'LOSS'){
        patternStreaks[pat].currentLoss++;
        patternStreaks[pat].currentWin = 0;
        if(patternStreaks[pat].currentLoss > patternStreaks[pat].maxLoss){
          patternStreaks[pat].maxLoss = patternStreaks[pat].currentLoss;
        }
      } else {
        patternStreaks[pat].currentWin = 0;
        patternStreaks[pat].currentLoss = 0;
      }
    });
    
    // Aggregate pattern stats
    Object.entries(d.pattern_stats||{}).forEach(([p,s])=>{
      if(!patAgg[p]) patAgg[p]={w:0,l:0,b:0,r:0,trades:0, maxWinStreak:0, maxLossStreak:0};
      patAgg[p].w+=s.w; patAgg[p].l+=s.l; patAgg[p].b+=s.b; patAgg[p].r+=s.r; patAgg[p].trades+=s.trades;
      patAgg[p].maxWinStreak = Math.max(patAgg[p].maxWinStreak, patternStreaks[p]?.maxWin || 0);
      patAgg[p].maxLossStreak = Math.max(patAgg[p].maxLossStreak, patternStreaks[p]?.maxLoss || 0);
    });
  });
  
  const pats=Object.entries(patAgg).sort((a,b)=>b[1].r-a[1].r);
  const dowAgg={};
  D.forEach(d=>Object.entries(d.dow_stats||{}).forEach(([day,s])=>{
    if(!dowAgg[day]) dowAgg[day]={name:s.name,w:0,l:0,b:0,r:0,trades:0};
    dowAgg[day].w+=s.w; dowAgg[day].l+=s.l; dowAgg[day].b+=s.b; dowAgg[day].r+=s.r; dowAgg[day].trades+=s.trades;
  }));
  const dows=Object.entries(dowAgg).sort((a,b)=>parseInt(a[0])-parseInt(b[0]));
  const maxPatR=Math.max(...pats.map(x=>Math.abs(x[1].r)),1);

  const patRows=pats.map(([p,s])=>{
    const r2=ratesFromCounts(s.w,s.b,s.l);
    const bw=Math.round(Math.abs(s.r)/maxPatR*120);
    const bar=s.r>=0?`<div style="height:8px;width:${bw}px;background:${WIN};border-radius:2px;opacity:.8"></div>`
                     :`<div style="height:8px;width:${bw}px;background:${LOSS};border-radius:2px;opacity:.8"></div>`;
    
    // Create streak bars
    const winBarWidth = Math.min(100, (s.maxWinStreak / 10) * 100);
    const lossBarWidth = Math.min(100, (s.maxLossStreak / 10) * 100);
    const streakColorWin = s.maxWinStreak >= 5 ? WIN : WIN+'aa';
    const streakColorLoss = s.maxLossStreak >= 5 ? LOSS : LOSS+'aa';
    
    return `<div class="pat-row">
      <div style="font-weight:700">${p}</div>
      <div style="color:${SUB}">${s.trades}</div>
      <div>${rateTrio(r2.wr,r2.ber,r2.lr)}</div>
      <div style="color:${s.r>=0?WIN:LOSS};font-weight:700">${s.r>=0?'+':''}${s.r.toFixed(2)}R</div>
      <div style="color:${s.r/s.trades>=0?WIN:LOSS}">${(s.r/s.trades>=0?'+':'')}${(s.r/s.trades).toFixed(2)}R/T</div>
      <div style="min-width:100px">
        <div style="display:flex;gap:8px;align-items:center;margin-bottom:4px">
          <span style="font-size:10px;color:${WIN};font-weight:700">W${s.maxWinStreak}</span>
          <div style="flex:1;height:4px;background:${WIN}22;border-radius:2px;overflow:hidden">
            <div style="width:${winBarWidth}%;height:4px;background:${streakColorWin};border-radius:2px"></div>
          </div>
          <span style="font-size:10px;color:${LOSS};font-weight:700">L${s.maxLossStreak}</span>
          <div style="flex:1;height:4px;background:${LOSS}22;border-radius:2px;overflow:hidden">
            <div style="width:${lossBarWidth}%;height:4px;background:${streakColorLoss};border-radius:2px"></div>
          </div>
        </div>
        ${s.maxLossStreak >= 5 ? `<div style="font-size:9px;color:${LOSS};margin-top:2px">⚠️ ${s.maxLossStreak} consecutive losses</div>` : 
          s.maxWinStreak >= 5 ? `<div style="font-size:9px;color:${WIN};margin-top:2px">🔥 ${s.maxWinStreak} consecutive wins</div>` : ''}
      </div>
      <div>${bar}</div></div>`;
  }).join('');

  const dowCards=dows.map(([,s])=>{
    const r2=ratesFromCounts(s.w,s.b,s.l);
    const col=s.r>=0?WIN:LOSS;
    return `<div class="dow-cell" style="border-color:${col}22">
      <div class="dow-name">${s.name}</div>
      <div class="dow-r" style="color:${col}">${s.r>=0?'+':''}${s.r.toFixed(2)}R</div>
      <div style="margin:8px 0">${rateTrio(r2.wr,r2.ber,r2.lr)}</div>
      <div class="dow-meta">${s.trades} trades</div>
      <div class="dow-meta" style="margin-top:3px">W${s.w} BE${s.b} L${s.l}</div>
    </div>`;
  }).join('');

  el.innerHTML=`
  <div class="sh">Pattern Performance (sorted by net R) — with Win/Loss Streaks</div>
  <div style="background:var(--card);border:1px solid var(--brd);border-radius:12px;overflow:hidden;margin-bottom:28px">
    <div class="pat-row" style="background:var(--card2);font-size:9px;color:var(--sub);font-weight:700;letter-spacing:.12em;text-transform:uppercase;border-bottom:2px solid var(--brd)">
      <div>Pattern</div><div>Trades</div><div>W% / BE% / L%</div><div>Total R</div><div>Expect/T</div><div>Streaks (W/L)</div><div>Bar</div>
    </div>${patRows||'<div style="padding:16px;color:var(--sub)">No pattern data.</div>'}
  </div>
  <div class="sh">Day of Week Performance</div>
  <div class="dow-grid">${dowCards}</div>
  <div class="sh">Net R by Day of Week</div>
  <div class="cbox"><canvas id="cv-dow" height="200"></canvas></div>`;

  setTimeout(()=>drawBars('cv-dow',dows.map(([,s])=>s.name),dows.map(([,s])=>+s.r.toFixed(2)),200),80);
})();

// ════════════════════════════════════════════════════════════════════════
// PARTIAL CLOSE ANALYSIS - FULLY WORKING VERSION
// ════════════════════════════════════════════════════════════════════════
(function(){
  // Wait for DOM and navigation to be ready
  function initPartialClose() {
    try {
      // Check if we have data
      if (typeof D === 'undefined' || !D || D.length === 0) {
        console.log('Partial Close: No data available');
        return;
      }
      
      // Find navigation
      const nav = document.getElementById('nav');
      if (!nav) {
        console.log('Partial Close: Navigation not found');
        return;
      }
      
      // Check if tab already exists
      let existingTab = Array.from(nav.querySelectorAll('.ntab')).find(btn => btn.dataset.v === 'partialclose');
      let el = document.getElementById('v-partialclose');
      
      // Create tab if it doesn't exist
      if (!existingTab) {
        const newTab = document.createElement('button');
        newTab.className = 'ntab';
        newTab.dataset.v = 'partialclose';
        newTab.textContent = 'Partial Close';
        
        // Add click handler for the new tab
        newTab.addEventListener('click', function() {
          // Remove active class from all tabs and views
          document.querySelectorAll('.ntab').forEach(x => x.classList.remove('on'));
          document.querySelectorAll('.view').forEach(x => x.classList.remove('on'));
          
          // Activate this tab and its view
          this.classList.add('on');
          const view = document.getElementById('v-' + this.dataset.v);
          if (view) view.classList.add('on');
        });
        
        nav.appendChild(newTab);
        existingTab = newTab;
      }
      
      // Create view if it doesn't exist
      if (!el) {
        el = document.createElement('div');
        el.id = 'v-partialclose';
        el.className = 'view';
        document.body.appendChild(el);
      }
      
      // Collect partial close data
      const partialData = {
        total_tp1_hits: 0,
        entry_retrace_hits: 0,
        by_pattern: {},
        by_pair: {}
      };
      
      // Iterate through all pairs
      if (D && D.forEach) {
        D.forEach(pair => {
          if (!pair || !pair.raw_trades) return;
          
          pair.raw_trades.forEach(trade => {
            if (!trade) return;
            
            // Check if this trade hit TP1 (positive R and not breakeven)
            const isWin = trade.outcome === 'WIN_FULL';
            const hasProfit = trade.realized_rr > 0;
            const isNotBE = trade.outcome !== 'BREAKEVEN';
            const hasTP1Hit = (isWin || hasProfit) && isNotBE;
            
            if (!hasTP1Hit) return;
            
            partialData.total_tp1_hits++;
            
            // Check if entry was retraced
            let hitEntry = false;
            if (trade.hit_entry_after_tp1 === true || trade.hit_entry_after_tp1 === 'True' || trade.hit_entry_after_tp1 === 'true') {
              hitEntry = true;
              partialData.entry_retrace_hits++;
            }
            
            // By pattern
            const pat = trade.pattern || 'UNKNOWN';
            if (!partialData.by_pattern[pat]) {
              partialData.by_pattern[pat] = { total: 0, retrace: 0 };
            }
            partialData.by_pattern[pat].total++;
            if (hitEntry) partialData.by_pattern[pat].retrace++;
            
            // By pair
            const pairName = pair.pair || trade.pair || 'UNKNOWN';
            if (!partialData.by_pair[pairName]) {
              partialData.by_pair[pairName] = { total: 0, retrace: 0 };
            }
            partialData.by_pair[pairName].total++;
            if (hitEntry) partialData.by_pair[pairName].retrace++;
          });
        });
      }
      
      // If no data, show message
      if (partialData.total_tp1_hits === 0) {
        if (el) {
          el.innerHTML = `<div class="sh">Partial Close Analysis</div>
            <div class="cbox" style="padding:40px;text-align:center">
              <div style="font-size:48px;margin-bottom:16px">📊</div>
              <div style="font-size:16px;color:var(--sub)">No partial close data available.</div>
              <div style="font-size:13px;color:var(--dim);margin-top:8px">
                Run backtest with hit_entry_after_tp1 tracking enabled.
              </div>
            </div>`;
        }
        return;
      }
      
      // Calculate rates
      const retraceRate = (partialData.entry_retrace_hits / partialData.total_tp1_hits * 100).toFixed(1);
      
      // Determine recommendation
      let recColor = '#0d9e5c';
      let recIcon = '✅';
      let recTitle = 'SAFE TO MOVE SL';
      let recommendation = 'Move SL to entry after TP1 - Price rarely returns to entry';
      
      if (retraceRate >= 25 && retraceRate < 45) {
        recColor = '#b07d00';
        recIcon = '⚖️';
        recTitle = 'SELECTIVE';
        recommendation = 'Consider selective SL move - Check pattern breakdown below';
      } else if (retraceRate >= 45) {
        recColor = '#d63b3b';
        recIcon = '⚠️';
        recTitle = 'CAUTION';
        recommendation = 'Keep original SL - High probability of retrace to entry';
      }
      
      // Build pattern rows
      let patternRows = '';
      const patterns = Object.entries(partialData.by_pattern)
        .sort((a, b) => (b[1].retrace / b[1].total) - (a[1].retrace / a[1].total));
      
      for (const [pat, data] of patterns) {
        const patRetraceRate = (data.retrace / data.total * 100).toFixed(1);
        let rateColor = '#0d9e5c';
        if (patRetraceRate > 50) rateColor = '#d63b3b';
        else if (patRetraceRate > 30) rateColor = '#b07d00';
        
        patternRows += `<div class="pat-row">
          <div style="font-weight:700;min-width:140px">${escapeHtml(pat)}</div>
          <div style="min-width:60px">${data.total}</div>
          <div style="color:${rateColor};font-weight:700;min-width:70px">${patRetraceRate}%</div>
          <div style="flex:1">
            <div style="height:6px;background:${rateColor}33;border-radius:3px;margin-top:6px">
              <div style="width:${patRetraceRate}%;height:6px;background:${rateColor};border-radius:3px"></div>
            </div>
          </div>
        </div>`;
      }
      
      // Build pair rows (top 15)
      let pairRows = '';
      const pairs = Object.entries(partialData.by_pair)
        .sort((a, b) => (b[1].retrace / b[1].total) - (a[1].retrace / a[1].total))
        .slice(0, 15);
      
      for (const [pairName, data] of pairs) {
        const pairRetraceRate = (data.retrace / data.total * 100).toFixed(1);
        let rateColor = '#0d9e5c';
        if (pairRetraceRate > 50) rateColor = '#d63b3b';
        else if (pairRetraceRate > 30) rateColor = '#b07d00';
        
        pairRows += `<div class="pat-row">
          <div style="font-weight:700;min-width:180px">${escapeHtml(pairName)}</div>
          <div style="min-width:60px">${data.total}</div>
          <div style="color:${rateColor};font-weight:700;min-width:70px">${pairRetraceRate}%</div>
          <div style="flex:1">
            <div style="height:6px;background:${rateColor}33;border-radius:3px;margin-top:6px">
              <div style="width:${pairRetraceRate}%;height:6px;background:${rateColor};border-radius:3px"></div>
            </div>
          </div>
        </div>`;
      }
      
      // Set the HTML
      if (el) {
        el.innerHTML = `
        <div class="sh">Partial Close Analysis — Did price revisit entry after TP1?</div>
        
        <div class="grid2" style="margin-bottom:24px">
          <div class="cbox">
            <div class="ctitle">Key Metrics <span>${partialData.total_tp1_hits} trades hit TP1</span></div>
            <div style="display:grid;grid-template-columns:1fr 1fr;gap:20px">
              <div style="text-align:center">
                <div style="font-size:13px;color:var(--sub);margin-bottom:8px">⬇️ Retraced to Entry</div>
                <div style="font-size:48px;font-weight:800;color:${recColor}">${retraceRate}%</div>
                <div style="font-size:13px;color:var(--sub);margin-top:4px">${partialData.entry_retrace_hits} trades</div>
              </div>
              <div style="text-align:center">
                <div style="font-size:13px;color:var(--sub);margin-bottom:8px">📋 Verdict</div>
                <div style="font-size:20px;font-weight:800;color:${recColor}">${recTitle}</div>
                <div style="font-size:12px;color:var(--sub);margin-top:8px">${retraceRate < 30 ? 'Safe to move SL' : retraceRate > 45 ? 'Keep original SL' : 'Check patterns'}</div>
              </div>
            </div>
          </div>
          
          <div class="cbox">
            <div class="ctitle">Recommendation</div>
            <div style="padding:20px;background:${recColor}11;border-radius:10px;border-left:4px solid ${recColor}">
              <div style="font-size:16px;font-weight:800;color:${recColor};margin-bottom:8px">${recIcon} ${recommendation.split(' - ')[0]}</div>
              <div style="font-size:13px;color:var(--txt);line-height:1.5">${recommendation.split(' - ')[1] || recommendation}</div>
            </div>
          </div>
        </div>
        
        <div class="sh">Performance by Pattern</div>
        <div style="background:var(--card);border:1px solid var(--brd);border-radius:12px;overflow:hidden;margin-bottom:24px">
          <div class="pat-row" style="background:var(--card2);font-size:10px;color:var(--sub);font-weight:700;text-transform:uppercase;border-bottom:2px solid var(--brd)">
            <div style="min-width:140px">Pattern</div>
            <div style="min-width:60px">TP1 Hits</div>
            <div style="min-width:70px">Retrace %</div>
            <div style="flex:1">Distribution</div>
          </div>
          ${patternRows || '<div style="padding:16px;color:var(--sub)">No pattern data available.</div>'}
        </div>
        
        <div class="sh">Top 15 Pairs by Retrace Rate</div>
        <div style="background:var(--card);border:1px solid var(--brd);border-radius:12px;overflow:hidden;margin-bottom:24px">
          <div class="pat-row" style="background:var(--card2);font-size:10px;color:var(--sub);font-weight:700;text-transform:uppercase;border-bottom:2px solid var(--brd)">
            <div style="min-width:180px">Pair</div>
            <div style="min-width:60px">TP1 Hits</div>
            <div style="min-width:70px">Retrace %</div>
            <div style="flex:1">Distribution</div>
          </div>
          ${pairRows || '<div style="padding:16px;color:var(--sub)">No pair data available.</div>'}
        </div>
        
        <div class="grid2">
          <div class="cbox" style="background:#0d9e5c08">
            <div class="ctitle">✅ When to Move SL to Entry</div>
            <ul style="margin:12px 0 0 20px;line-height:1.8">
              <li>Retrace rate &lt; 30% → price rarely comes back</li>
              <li>Focus on patterns with low retrace rates</li>
              <li>Creates risk-free trade after TP1</li>
              <li>Let remaining position run to TP2</li>
            </ul>
          </div>
          
          <div class="cbox" style="background:#d63b3b08">
            <div class="ctitle">⚠️ When NOT to Move SL to Entry</div>
            <ul style="margin:12px 0 0 20px;line-height:1.8">
              <li>Retrace rate &gt; 50% → high chance of being stopped out</li>
              <li>Keep original SL or use wider stop</li>
              <li>Consider taking full profit at TP1</li>
              <li>Use trailing stop instead of moving to entry</li>
            </ul>
          </div>
        </div>
        
        <div class="cbox" style="margin-top:16px;background:#4f6ef708">
          <div class="ctitle">💡 Actionable Insight</div>
          <div style="font-size:14px;line-height:1.6;color:var(--txt)">
            Based on ${partialData.total_tp1_hits} TP1 hits: 
            <strong style="color:${recColor}">${retraceRate}% retrace rate</strong>.
            ${retraceRate < 30 ? 
              '✅ Move SL to entry after TP1. This locks in profits and creates risk-free trades.' :
              retraceRate > 45 ?
              '❌ Do NOT move SL to entry. Take full profit at TP1 or use wider stop loss.' :
              '⚖️ Move SL to entry only on patterns with retrace rates below 30% (see table above).'
            }
          </div>
        </div>`;
      }
      
    } catch (err) {
      console.error('Partial Close Analysis Error:', err);
    }
  }
  
  // Helper function to escape HTML
  function escapeHtml(str) {
    if (!str) return '';
    return String(str).replace(/[&<>]/g, function(m) {
      if (m === '&') return '&amp;';
      if (m === '<') return '&lt;';
      if (m === '>') return '&gt;';
      return m;
    });
  }
  
  // Initialize after DOM is ready
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initPartialClose);
  } else {
    initPartialClose();
  }
})();

// ════════════════════════════════════════════════════════════════════════
// TRADE LOG
// ════════════════════════════════════════════════════════════════════════
(function(){
  const el=document.getElementById('v-tradelog');
  const ALL_TRADES=[];
  D.forEach(d=>(d.raw_trades||[]).forEach(t=>ALL_TRADES.push({...t,pair:t.pair||d.pair})));
  ALL_TRADES.sort((a,b)=>a.entry_dt.localeCompare(b.entry_dt));
  const logCols=TRADE_COLUMNS.length?TRADE_COLUMNS:[
    'id','symbol','direction','entry_dt','close_dt','entry','sl','tp1','tp2','rr','outcome','realized_rr'
  ];

  const allPairs=[...new Set(ALL_TRADES.map(t=>t.pair))].sort();
  const allTfPairs=[...new Set(ALL_TRADES.map(t=>t.tf_pair||'').filter(Boolean))].sort();
  const allPatterns=[...new Set(ALL_TRADES.map(t=>t.pattern||'UNKNOWN').filter(Boolean))].sort();

  el.innerHTML=`
  <div class="sh">All Trades — ${ALL_TRADES.length} total</div>
  <div id="tlog-filters">
    <input id="tlog-search" placeholder="🔍 Search pair, pattern, date…">
    <select class="fsel" id="f-pair"><option value="">All Pairs</option>${allPairs.map(p=>`<option>${p}</option>`).join('')}</select>
    <select class="fsel" id="f-tf"><option value="">All TF Pairs</option>${allTfPairs.map(p=>`<option>${p}</option>`).join('')}</select>
    <select class="fsel" id="f-outcome">
      <option value="">All Outcomes</option>
      <option value="WIN_FULL">Win</option>
      <option value="LOSS">Loss</option>
      <option value="BREAKEVEN">Breakeven</option>
    </select>
    <select class="fsel" id="f-dir">
      <option value="">All Directions</option>
      <option value="LONG">Long ↑</option>
      <option value="SHORT">Short ↓</option>
    </select>
    <select class="fsel" id="f-pattern"><option value="">All Patterns</option>${allPatterns.map(p=>`<option>${p}</option>`).join('')}</select>
    <input class="fsel" type="date" id="f-from" title="From date">
    <input class="fsel" type="date" id="f-to"   title="To date">
    <span id="tlog-count"></span>
    <button id="export-btn">⬇ Export CSV</button>
  </div>
  <div class="tbl-wrap">
    <table id="tlog-table">
      <thead><tr>${logCols.map(c=>`<th data-col="${c}">${c}</th>`).join('')}<th data-col="hold_min">hold_min</th></tr></thead>
      <tbody id="tlog-body"></tbody>
    </table>
  </div>`;

  let tlSortCol='entry_dt', tlSortDir=1;
  let filtered=[...ALL_TRADES];

  function applyFilters(){
    const q=document.getElementById('tlog-search').value.toLowerCase();
    const fp=document.getElementById('f-pair').value;
    const ftf=document.getElementById('f-tf').value;
    const fo=document.getElementById('f-outcome').value;
    const fd=document.getElementById('f-dir').value;
    const fpat=document.getElementById('f-pattern').value;
    const ffrom=document.getElementById('f-from').value;
    const fto=document.getElementById('f-to').value;
    filtered=ALL_TRADES.filter(t=>{
      if(fp && t.pair!==fp) return false;
      if(ftf && (t.tf_pair||'')!==ftf) return false;
      if(fo && t.outcome!==fo) return false;
      if(fd && t.direction!==fd) return false;
      if(fpat && (t.pattern||'UNKNOWN')!==fpat) return false;
      if(ffrom && t.entry_dt.slice(0,10)<ffrom) return false;
      if(fto   && t.entry_dt.slice(0,10)>fto)   return false;
      if(q){
        const hay=`${t.pair} ${t.pattern||''} ${t.entry_dt} ${t.outcome} ${t.direction}`.toLowerCase();
        if(!hay.includes(q)) return false;
      }
      return true;
    });
    renderTlog();
  }

  function renderTlog(){
    const sorted=[...filtered].sort((a,b)=>{
      let av=a[tlSortCol]??0, bv=b[tlSortCol]??0;
      if(typeof av==='string') return tlSortDir*(av.localeCompare(bv));
      return tlSortDir*(av-bv);
    });
    const tb=document.getElementById('tlog-body'); tb.innerHTML='';
    sorted.forEach((t,idx)=>{
      const tr=document.createElement('tr');
      tr.innerHTML=logCols.map(col=>{
        const val=fmtTradeVal(t,col);
        if(col==='outcome'){
          return `<td><span class="badge ${t.outcome==='WIN_FULL'?'g':t.outcome==='LOSS'?'r':'y'}">${val}</span></td>`;
        }
        return `<td style="${tradeCellStyle(t,col)}">${val}</td>`;
      }).join('') + `<td style="color:${SUB}">${holdStr(t.hold_min)}</td>`;
      tb.appendChild(tr);
    });
    document.getElementById('tlog-count').textContent=`${filtered.length} of ${ALL_TRADES.length} trades`;
  }

  document.querySelectorAll('#tlog-table thead th').forEach(th=>{
    th.style.cursor='pointer';
    th.addEventListener('click',()=>{
      const col=th.dataset.col;
      if(tlSortCol===col) tlSortDir*=-1; else { tlSortCol=col; tlSortDir=-1; }
      document.querySelectorAll('#tlog-table thead th').forEach(x=>x.classList.remove('sort-asc','sort-desc'));
      th.classList.add(tlSortDir===1?'sort-asc':'sort-desc');
      renderTlog();
    });
  });

  ['tlog-search','f-pair','f-tf','f-outcome','f-dir','f-pattern','f-from','f-to'].forEach(id=>{
    document.getElementById(id).addEventListener('input',applyFilters);
    document.getElementById(id).addEventListener('change',applyFilters);
  });

  document.getElementById('export-btn').addEventListener('click',()=>{
    const rows=[[...logCols,'hold_min']];
    filtered.forEach(t=>rows.push([...logCols.map(c=>tradeVal(t,c)),t.hold_min||'']));
    const csv=rows.map(r=>r.map(v=>JSON.stringify(v)).join(',')).join('\\n');
    const blob=new Blob([csv],{type:'text/csv'});
    const a=document.createElement('a'); a.href=URL.createObjectURL(blob);
    a.download=`trades_export_${new Date().toISOString().slice(0,10)}.csv`; a.click();
  });

  applyFilters();
})();

// ════════════════════════════════════════════════════════════════════════
// CORRELATION
// ════════════════════════════════════════════════════════════════════════
(function(){
  const el=document.getElementById('v-correlation');
  if(!PL||PL.length<2){
    el.innerHTML='<div class="sh">Pair Correlation</div><p style="color:var(--sub);padding:20px">Need at least 2 pairs with overlapping dates.</p>';
    return;
  }

  function cellHtml(cell,mode){
    if(cell.days===0) return `<td style="background:#f0f2f5;color:${DIM}">—</td>`;
    const pct=mode==='loss'?cell.loss_pct:cell.win_pct;
    if(pct===null) return `<td style="background:#f0f2f5;color:${DIM}">—</td>`;
    const intensity=pct/100;
    const col=mode==='loss'
      ?`rgba(214,59,59,${0.1+intensity*0.7})`
      :`rgba(13,158,92,${0.1+intensity*0.7})`;
    const tc=pct>55?'#fff':'var(--txt)';
    return `<td style="background:${col};color:${tc}" title="${cell.days} shared days · ${pct}%">${pct}%</td>`;
  }

  function buildTable(mode){
    const modeLabel=mode==='loss'?'Co-Loss Rate':'Co-Win Rate';
    let html=`<div class="sh">${modeLabel} Heatmap — % of shared days where both pairs ${mode==='loss'?'lost':'won'}</div>
    <div style="overflow-x:auto;margin-bottom:28px"><table class="corr-table">
      <thead><tr><th></th>${PL.map(p=>`<th>${p}</th>`).join('')}</tr></thead><tbody>`;
    PL.forEach((p1,i)=>{
      html+=`<tr><th style="text-align:left;white-space:nowrap">${p1}</th>`;
      PL.forEach((p2,j)=>{
        if(i===j) html+=`<td style="background:var(--card2);font-weight:800;color:var(--acc)">${p1.slice(0,3)}</td>`;
        else html+=cellHtml(CM[i][j],mode);
      });
      html+=`</tr>`;
    });
    html+=`</tbody></table></div>`;
    return html;
  }

  const risks=[];
  PL.forEach((p1,i)=>PL.forEach((p2,j)=>{
    if(j<=i) return;
    const c=CM[i][j];
    if(c.days>=3&&c.loss_pct>=60) risks.push({p1,p2,...c});
  }));
  risks.sort((a,b)=>b.loss_pct-a.loss_pct);

  const riskHtml=risks.length
    ?risks.map(r=>`<div class="streak-alert" style="margin-bottom:8px">
        <div class="streak-alert-icon">⚠️</div>
        <div>
          <div style="font-weight:800;font-size:14px;color:var(--loss)">${r.p1} + ${r.p2} — ${r.loss_pct}% co-loss rate</div>
          <div style="font-size:13px;color:var(--sub);margin-top:2px">Both pairs lost on the same day ${r.coloss} out of ${r.days} shared trading days. High concentration risk.</div>
        </div></div>`).join('')
    :`<div class="streak-ok"><div class="streak-alert-icon">✅</div>
        <div style="font-weight:700;color:var(--win);font-size:14px">No high co-loss pairs detected (threshold: 60%, min 3 shared days).</div></div>`;

  el.innerHTML=`
  <div class="sh">Concentration Risk Alerts</div>
  <div style="margin-bottom:24px">${riskHtml}</div>
  <div style="display:flex;gap:10px;margin-bottom:20px;flex-wrap:wrap">
    <button class="pbtn sel" id="corr-loss-btn" onclick="corrMode('loss')">🔴 Co-Loss Matrix</button>
    <button class="pbtn"     id="corr-win-btn"  onclick="corrMode('win')">🟢 Co-Win Matrix</button>
  </div>
  <div id="corr-content">${buildTable('loss')}</div>
  <div style="font-size:13px;color:var(--sub);margin-top:8px">Darker red = both pairs lose together more often. Min 3 shared days shown. Diagonal = pair itself.</div>`;

  window.corrMode=function(mode){
    ['loss','win'].forEach(m=>document.getElementById('corr-'+m+'-btn').classList.remove('sel'));
    document.getElementById('corr-'+mode+'-btn').classList.add('sel');
    document.getElementById('corr-content').innerHTML=buildTable(mode);
  };
})();

// ════════════════════════════════════════════════════════════════════════
// RISK OF RUIN
// ════════════════════════════════════════════════════════════════════════
(function(){
  const el=document.getElementById('v-risk');

  const allMaxLoss   = D.map(d=>d.max_loss_streak||0);
  const allExpLoss   = D.map(d=>d.expected_max_loss_streak||0);
  const globalMaxStreak  = Math.max(...allMaxLoss, 1);
  const globalExpStreak  = Math.max(...allExpLoss, 1);
  const globalAvgStreak  = Math.round(allExpLoss.reduce((a,b)=>a+b,0)/Math.max(allExpLoss.length,1));
  const meanRunLoss = G.wr>0 ? Math.round(1/((100-G.wr)/100)) : 3;

  el.innerHTML=`
  <div class="sh">Risk of Ruin Calculator</div>
  <div class="ror-grid">
    <div class="ror-inputs">
      <div class="ror-label">Win Rate (%)</div>
      <input class="ror-input" id="ror-wr" type="number" min="1" max="99" step="0.1" value="${Math.round(G.wr)}">
      <div class="ror-label">Avg Win (R) <span style="font-size:10px;color:var(--win);font-weight:600">← from your data</span></div>
      <input class="ror-input" id="ror-aw" type="number" min="0.1" step="0.01" value="${G.avg_win||1.5}">
      <div class="ror-label">Avg Loss (R) <span style="font-size:10px;color:var(--loss);font-weight:600">← from your data</span></div>
      <input class="ror-input" id="ror-al" type="number" min="0.1" step="0.01" value="${G.avg_loss||1.0}">
      <div class="ror-label">% Account at Risk Per Trade</div>
      <input class="ror-input" id="ror-rpt" type="number" min="0.1" max="50" step="0.1" value="1">
      <div class="ror-label">Ruin Threshold (% drawdown)</div>
      <input class="ror-input" id="ror-thr" type="number" min="1" max="99" step="1" value="50">
      <div class="ror-label">Starting Capital ($)</div>
      <input class="ror-input" id="ror-cap" type="number" min="100" step="100" value="10000">
    </div>
    <div class="ror-result" id="ror-result"></div>
  </div>

  <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(160px,1fr));gap:10px;margin-bottom:24px">
    ${[
      ['Observed Max Streak',globalMaxStreak,'consecutive losses seen',LOSS],
      ['Expected Max Streak',globalExpStreak,'statistically expected',BE],
      ['Avg Expected Streak',globalAvgStreak,'mean across all pairs',SUB],
      ['Avg Run Length',meanRunLoss,'losses before a win',SUB],
    ].map(([l,v,s,c])=>`
    <div class="cbox" style="padding:14px 16px;border-color:${c}33">
      <div style="font-size:10px;font-weight:700;color:var(--sub);text-transform:uppercase;letter-spacing:.1em;margin-bottom:6px">${l}</div>
      <div style="font-size:28px;font-weight:800;color:${c};line-height:1">${v}</div>
      <div style="font-size:11px;color:var(--sub);margin-top:4px">${s}</div>
    </div>`).join('')}
  </div>

  <div style="background:var(--card);border:1px solid var(--brd);border-radius:12px;padding:20px;margin-bottom:24px">
    <div class="sh">Streak Drawdown Impact</div>
    <table class="ror-table" id="ror-streaks">
      <thead><tr>
        <th>Streak Scenario</th><th>Losses</th><th>Account Impact</th><th>Remaining $</th><th>Hits Ruin?</th>
      </tr></thead>
      <tbody></tbody>
    </table>
  </div>

  <div style="background:var(--card);border:1px solid var(--brd);border-radius:12px;padding:20px;margin-bottom:24px">
    <div class="sh">Streak Simulator — losses needed to hit ruin threshold</div>
    <div id="ror-streak-sim" style="padding:8px 0"></div>
  </div>

  <div style="background:var(--card);border:1px solid var(--brd);border-radius:12px;padding:20px;margin-bottom:24px">
    <div class="sh">Consecutive Loss Scenarios</div>
    <table class="ror-table" id="ror-scenarios">
      <thead><tr><th>Consecutive Losses</th><th>Account Impact</th><th>Remaining Capital</th><th>% of Account</th></tr></thead>
      <tbody></tbody>
    </table>
  </div>

  <div style="background:var(--card);border:1px solid var(--brd);border-radius:12px;padding:20px;margin-bottom:24px">
    <div class="sh">Risk of Ruin at Different Thresholds</div>
    <table class="ror-table" id="ror-thr-table">
      <thead><tr><th>Ruin Threshold</th><th>Risk of Ruin</th><th>Streak-Adjusted RoR</th><th>Assessment</th></tr></thead>
      <tbody></tbody>
    </table>
  </div>`;

  function calcRoR(){
    const wr  = parseFloat(document.getElementById('ror-wr').value)/100  || 0.5;
    const aw  = parseFloat(document.getElementById('ror-aw').value)       || G.avg_win||1.5;
    const al  = parseFloat(document.getElementById('ror-al').value)       || G.avg_loss||1.0;
    const rpt = parseFloat(document.getElementById('ror-rpt').value)/100  || 0.01;
    const thr = parseFloat(document.getElementById('ror-thr').value)/100  || 0.5;
    const cap = parseFloat(document.getElementById('ror-cap').value)       || 10000;
    const lr  = 1 - wr;
    const edge = wr*aw - lr*al;

    let ror=0;
    if(edge<=0){ ror=1; }
    else {
      const a=(lr*al)/(wr*aw);
      ror=Math.min(1,Math.pow(a,thr/rpt));
    }

    const streakImpact = 1 - Math.pow(1-rpt, globalExpStreak);
    const remainingThr = Math.max(0, thr - streakImpact);
    let streakAdjRor;
    if(edge<=0){ streakAdjRor=1; }
    else if(remainingThr<=0){ streakAdjRor=1; }
    else {
      const a=(lr*al)/(wr*aw);
      streakAdjRor=Math.min(1,Math.pow(a,remainingThr/rpt));
    }

    const lossesToRuin = Math.ceil(Math.log(1-thr)/Math.log(1-rpt));

    const col   = ror>0.1?LOSS:ror>0.02?BE:WIN;
    const saCol = streakAdjRor>0.1?LOSS:streakAdjRor>0.02?BE:WIN;
    const label = ror>0.2?'HIGH RISK':ror>0.05?'ELEVATED':ror>0.01?'MODERATE':'LOW RISK';

    document.getElementById('ror-result').innerHTML=`
      <div class="ror-big" style="color:${col}">${(ror*100).toFixed(1)}%</div>
      <div style="font-size:20px;font-weight:800;color:${col};margin-bottom:10px">${label}</div>
      <div class="ror-sub">Base RoR — hitting ${(thr*100).toFixed(0)}% drawdown</div>
      <div style="margin-top:12px;padding:10px 14px;border-radius:8px;background:${saCol}18;border:1px solid ${saCol}44">
        <div style="font-size:22px;font-weight:800;color:${saCol}">${(streakAdjRor*100).toFixed(1)}%</div>
        <div style="font-size:12px;color:var(--sub);margin-top:3px">Streak-adjusted RoR</div>
        <div style="font-size:11px;color:var(--sub);margin-top:2px">Assumes exp. streak of ${globalExpStreak} losses eats ${(streakImpact*100).toFixed(1)}% first</div>
      </div>
      <div class="ror-sub" style="margin-top:12px">Edge: <strong style="color:${edge>=0?WIN:LOSS}">${edge>=0?'+':''}${edge.toFixed(3)}R/trade</strong></div>
      <div class="ror-sub" style="margin-top:4px">Losses to ruin: <strong style="color:${LOSS}">${lossesToRuin} consecutive</strong></div>
      <div style="font-size:11px;color:var(--sub);margin-top:10px;text-align:center">Adjust inputs on the left to recalculate</div>`;

    const stb=document.querySelector('#ror-streaks tbody'); stb.innerHTML='';
    const streakScenarios=[
      ['Avg run length (expected)', meanRunLoss],
      ['Avg expected max streak',   globalAvgStreak],
      ['Global expected max streak',globalExpStreak],
      ['Observed worst streak',     globalMaxStreak],
      ['2× observed worst',         globalMaxStreak*2],
    ];
    streakScenarios.forEach(([label,n])=>{
      const impact=1-Math.pow(1-rpt,n);
      const rem=cap*(1-impact);
      const ruined=impact>=thr;
      const tr=document.createElement('tr');
      if(ruined) tr.style.background='var(--loss-bg)';
      else if(impact>=thr*0.7) tr.style.background='var(--be-bg)';
      tr.innerHTML=`
        <td style="font-weight:700">${label}</td>
        <td style="color:${LOSS};font-weight:700;text-align:center">${n}</td>
        <td style="color:${ruined?LOSS:impact>=thr*0.5?BE:SUB};font-weight:700">-${(impact*100).toFixed(1)}%</td>
        <td>$${rem.toFixed(2)}</td>
        <td style="font-weight:800;color:${ruined?LOSS:WIN}">${ruined?'💀 YES':'✅ NO'}</td>`;
      stb.appendChild(tr);
    });

    const simEl=document.getElementById('ror-streak-sim');
    const simCol=lossesToRuin<=globalMaxStreak?LOSS:lossesToRuin<=globalExpStreak*1.5?BE:WIN;
    simEl.innerHTML=`
      <div style="display:flex;align-items:flex-end;gap:24px;flex-wrap:wrap;margin-bottom:16px">
        <div>
          <div style="font-size:48px;font-weight:800;color:${simCol};line-height:1">${lossesToRuin}</div>
          <div style="font-size:13px;color:var(--sub);margin-top:4px">consecutive losses to hit ${(thr*100).toFixed(0)}% ruin at ${(rpt*100).toFixed(1)}% risk/trade</div>
        </div>
        <div style="flex:1;min-width:200px">
          <div style="font-size:12px;font-weight:700;color:var(--sub);margin-bottom:8px;text-transform:uppercase;letter-spacing:.08em">vs your streak history</div>
          ${[
            ['Avg run length',    meanRunLoss,    lossesToRuin<=meanRunLoss],
            ['Avg expected max',  globalAvgStreak,lossesToRuin<=globalAvgStreak],
            ['Expected max',      globalExpStreak,lossesToRuin<=globalExpStreak],
            ['Observed max',      globalMaxStreak,lossesToRuin<=globalMaxStreak],
          ].map(([lbl,v,danger])=>`
          <div style="display:flex;align-items:center;gap:10px;margin-bottom:6px">
            <div style="font-size:12px;color:var(--sub);width:140px">${lbl}</div>
            <div style="font-size:14px;font-weight:800;color:${danger?LOSS:WIN};width:32px">${v}</div>
            <div style="font-size:12px;font-weight:700;color:${danger?LOSS:WIN}">${danger?'⚠ RUIN REACHABLE':'✅ safe'}</div>
          </div>`).join('')}
        </div>
      </div>
      <div style="background:var(--brd);border-radius:6px;height:10px;overflow:hidden">
        <div style="height:10px;border-radius:6px;background:${simCol};width:${Math.min(100,(lossesToRuin/Math.max(globalMaxStreak*2,1)*100)).toFixed(1)}%;transition:width .4s"></div>
      </div>
      <div style="display:flex;justify-content:space-between;font-size:11px;color:var(--sub);margin-top:4px">
        <span>0 losses</span><span>${globalMaxStreak*2} losses (2× observed max)</span>
      </div>`;

    const tb=document.querySelector('#ror-scenarios tbody'); tb.innerHTML='';
    const maxShow=Math.max(10,globalMaxStreak+2);
    for(let n=1;n<=maxShow;n++){
      const impact=1-Math.pow(1-rpt,n);
      const rem=cap*(1-impact);
      const prob=Math.pow(lr,n)*100;
      const isExpected=n===globalExpStreak;
      const isObserved=n===globalMaxStreak;
      const tr=document.createElement('tr');
      if(impact>=thr) tr.style.background='var(--loss-bg)';
      else if(isExpected||isObserved) tr.style.background='var(--be-bg)';
      const tag=isObserved?` <span style="background:${LOSS}22;color:${LOSS};border-radius:4px;padding:1px 6px;font-size:10px;font-weight:700">observed max</span>`
               :isExpected?` <span style="background:${BE}22;color:${BE};border-radius:4px;padding:1px 6px;font-size:10px;font-weight:700">expected max</span>`:'';
      tr.innerHTML=`
        <td>${n} loss${n>1?'es':''}${tag} <span style="font-size:11px;color:${SUB}">(${prob.toFixed(2)}% likely)</span></td>
        <td style="color:${LOSS};font-weight:700">-${(impact*100).toFixed(1)}%</td>
        <td>$${rem.toFixed(2)}</td>
        <td style="color:${impact>thr?LOSS:impact>thr*0.5?BE:SUB}">${(100-impact*100).toFixed(1)}%</td>`;
      tb.appendChild(tr);
    }

    const tb2=document.querySelector('#ror-thr-table tbody'); tb2.innerHTML='';
    [10,20,30,40,50,60,70,80].forEach(t=>{
      const a=(lr*al)/(wr*aw);
      const r2=edge<=0?1:Math.min(1,Math.pow(a,(t/100)/rpt));
      const remThr2=Math.max(0,(t/100)-streakImpact);
      const rAdj=edge<=0||remThr2<=0?1:Math.min(1,Math.pow(a,remThr2/rpt));
      const tr2=document.createElement('tr');
      const col2=r2>0.1?LOSS:r2>0.02?BE:WIN;
      const colAdj=rAdj>0.1?LOSS:rAdj>0.02?BE:WIN;
      const lbl2=r2>0.2?'🔴 HIGH':r2>0.05?'🟡 ELEVATED':r2>0.01?'🟢 MODERATE':'🟢 LOW';
      tr2.innerHTML=`
        <td style="font-weight:700">${t}% drawdown</td>
        <td style="color:${col2};font-weight:700">${(r2*100).toFixed(1)}%</td>
        <td style="color:${colAdj};font-weight:700">${(rAdj*100).toFixed(1)}%</td>
        <td>${lbl2}</td>`;
      tb2.appendChild(tr2);
    });
  }

  ['ror-wr','ror-aw','ror-al','ror-rpt','ror-thr','ror-cap'].forEach(id=>{
    document.getElementById(id).addEventListener('input',calcRoR);
  });
  calcRoR();
})();

// ════════════════════════════════════════════════════════════════════════
// SAME-DAY STRIKES
// ════════════════════════════════════════════════════════════════════════
(function(){
  const el=document.getElementById('v-sameday');
  const days=Object.entries(CD).filter(([,d])=>(d.w+d.b+d.l)>=1).sort((a,b)=>a[1].r-b[1].r);
  if(!days.length){ el.innerHTML='<div class="sh">Same-Day Strikes</div><p style="color:var(--sub);padding:20px">No trade data found.</p>'; return; }
  const multiDays=days.filter(([,d])=>(d.w+d.b+d.l)>=2);
  const massLoss=days.filter(([,d])=>{ const t=d.w+d.b+d.l; return t>=2&&d.l/t>=0.7; });
  const massWin=[...days].sort((a,b)=>b[1].r-a[1].r).filter(([,d])=>{ const t=d.w+d.b+d.l; return t>=2&&d.w/t>=0.7; });
  const worstDay=days[0]; const bestDay=[...days].sort((a,b)=>b[1].r-a[1].r)[0];

  el.innerHTML=`
  <div class="sh">Same-Day Strikes — all trade days sorted worst first</div>
  <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(160px,1fr));gap:10px;margin-bottom:24px">
    ${[['Total days',days.length,ACC],['Multi-pair',multiDays.length,ACC3],['Mass-loss',massLoss.length,LOSS],['Mass-win',massWin.length,WIN]].map(([l,v,c])=>`
    <div class="cbox" style="padding:16px 18px">
      <div style="font-size:11px;font-weight:700;color:var(--sub);text-transform:uppercase;letter-spacing:.1em;margin-bottom:6px">${l}</div>
      <div style="font-size:28px;font-weight:800;color:${c}">${v}</div>
    </div>`).join('')}
    <div class="cbox" style="padding:16px 18px">
      <div style="font-size:11px;font-weight:700;color:var(--sub);text-transform:uppercase;letter-spacing:.1em;margin-bottom:6px">Worst Day</div>
      <div style="font-size:17px;font-weight:800;color:var(--loss)">${worstDay[0]}</div>
      <div style="font-size:13px;color:var(--loss);font-weight:700;margin-top:2px">${worstDay[1].r.toFixed(2)}R · ${worstDay[1].l}L</div>
    </div>
    <div class="cbox" style="padding:16px 18px">
      <div style="font-size:11px;font-weight:700;color:var(--sub);text-transform:uppercase;letter-spacing:.1em;margin-bottom:6px">Best Day</div>
      <div style="font-size:17px;font-weight:800;color:var(--win)">${bestDay[0]}</div>
      <div style="font-size:13px;color:var(--win);font-weight:700;margin-top:2px">+${bestDay[1].r.toFixed(2)}R · ${bestDay[1].w}W</div>
    </div>
  </div>
  <div style="display:flex;gap:8px;align-items:center;margin-bottom:18px;flex-wrap:wrap">
    <span style="font-size:13px;color:var(--sub);font-weight:600">Filter:</span>
    <button class="pbtn sel" id="sdc-f-all"   onclick="sdcFilter('all')">All</button>
    <button class="pbtn"     id="sdc-f-multi" onclick="sdcFilter('multi')">⚡ Multi-pair</button>
    <button class="pbtn"     id="sdc-f-loss"  onclick="sdcFilter('loss')">🔴 Mass-loss</button>
    <button class="pbtn"     id="sdc-f-win"   onclick="sdcFilter('win')">🟢 Mass-win</button>
    <span style="margin-left:auto;font-size:13px;color:var(--sub)" id="sdc-count"></span>
  </div>
  <div id="sdc-list"></div>`;

  function renderPill(t){
    const cls=t.outcome==='WIN_FULL'?'win':t.outcome==='LOSS'?'loss':'be';
    return `<div class="sdc-pill ${cls}">${t.dir==='LONG'?'↑':'↓'} ${t.pair} <span style="opacity:.7;font-size:11px">${t.entry}</span> ${(t.rr>=0?'+':'')+t.rr.toFixed(2)}R</div>`;
  }
  function renderDay(date,d){
    const tot=d.w+d.b+d.l, lr2=tot?d.l/tot:0, wr2=tot?d.w/tot:0;
    const nc=d.r>=0?WIN:LOSS;
    const iML=lr2>=0.7&&tot>=2; const iMW=wr2>=0.7&&tot>=2;
    const r2=ratesFromCounts(d.w,d.b,d.l);
    const sorted=[...(d.pairs||[])].sort((a,b)=>d.r<0?(a.outcome==='LOSS'?-1:1):(b.outcome==='WIN_FULL'?-1:1));
    const badge2=iML?`<div class="sdc-streak-badge">⚠ ${d.l}/${tot} lost</div>`
      :iMW?`<div class="sdc-streak-badge" style="background:var(--win-bg);color:var(--win);border-color:var(--win-brd)">🎯 ${d.w}/${tot} won</div>`:'';
    return `<div class="sdc-row" style="${iML?'border-color:#f5b8b8':iMW?'border-color:#b6e8d0':''}">
      <div class="sdc-header">
        <div class="sdc-date">${date}</div>
        <div class="sdc-net" style="color:${nc}">${(d.r>=0?'+':'')+d.r.toFixed(2)}R</div>
        <div>${rateTrio(r2.wr,r2.ber,r2.lr)}</div>
        <div class="sdc-counts">
          <span style="color:${WIN}">W ${d.w}</span><span style="color:${BE}">BE ${d.b}</span><span style="color:${LOSS}">L ${d.l}</span>
          <span style="color:${SUB}">/ ${tot} trades</span>
        </div>${badge2}
      </div>
      <div class="sdc-pills">${sorted.map(renderPill).join('')}</div>
    </div>`;
  }
  window.sdcFilter=function(mode){
    ['all','multi','loss','win'].forEach(f=>document.getElementById('sdc-f-'+f)?.classList.remove('sel'));
    document.getElementById('sdc-f-'+mode)?.classList.add('sel');
    let fil=days;
    if(mode==='multi') fil=days.filter(([,d])=>(d.w+d.b+d.l)>=2);
    else if(mode==='loss') fil=days.filter(([,d])=>{ const t=d.w+d.b+d.l;return t>=2&&d.l/t>=0.7; });
    else if(mode==='win')  fil=[...days].sort((a,b)=>b[1].r-a[1].r).filter(([,d])=>{ const t=d.w+d.b+d.l;return t>=2&&d.w/t>=0.7; });
    document.getElementById('sdc-count').textContent=fil.length+' day'+(fil.length!==1?'s':'');
    document.getElementById('sdc-list').innerHTML=fil.length?fil.map(([date,d])=>renderDay(date,d)).join(''):'<div style="color:var(--sub);padding:20px">No days match.</div>';
  };
  window.sdcFilter('all');
})();

// ════════════════════════════════════════════════════════════════════════
// PAIR DETAIL
// ════════════════════════════════════════════════════════════════════════
function showDetail(i){
  document.querySelectorAll('.ntab').forEach(x=>x.classList.remove('on'));
  document.querySelectorAll('.view').forEach(x=>x.classList.remove('on'));
  document.querySelector('[data-v="detail"]').classList.add('on');
  document.getElementById('v-detail').classList.add('on');
  renderDetail(i);
}

function renderDetail(idx){
  _curDetail=idx;
  const d=D[idx]; const col=d.total_r>=0?WIN:LOSS;
  const el=document.getElementById('v-detail');
  const tzOpt=tzSel.options[tzSel.selectedIndex].text.split('(')[0].trim();

  const pHourAgg={};
  (d.raw_trades||[]).forEach(t=>{
    const lh=getLocalHour(t.entry_dt,TZ_OFFSET); if(lh===null) return;
    if(!pHourAgg[lh]) pHourAgg[lh]={w:0,l:0,b:0,r:0};
    pHourAgg[lh].r+=t.realized_rr;
    if(t.outcome==='WIN_FULL')pHourAgg[lh].w++; else if(t.outcome==='LOSS')pHourAgg[lh].l++; else pHourAgg[lh].b++;
  });
  const scoredH=Object.entries(pHourAgg).filter(([,s])=>(s.w+s.l+s.b)>=2).sort((a,b)=>b[1].r-a[1].r);
  const pBest=scoredH.slice(0,3); const pWorst=scoredH.slice(-3).reverse();

  function hwRowsLocal(hours,c){
    return (hours||[]).map(([h,s])=>{
      const n=s.w+s.l+s.b;
      const r2=ratesFromCounts(s.w,s.b,s.l);
      const sn=classifySessions(parseInt(h));
      return `<div class="hw-row" style="background:${c}11;border-color:${c}22">
        <div class="hw-hour" style="color:${c};font-size:24px">${String(h).padStart(2,'0')}<small>:00</small></div>
        <div class="hw-stats">
          <div class="hw-r" style="color:${c};font-size:16px">${s.r>=0?'+':''}${s.r.toFixed(2)}R</div>
          <div style="margin:5px 0">${rateTrio(r2.wr,r2.ber,r2.lr)}</div>
          <div class="hw-meta">${n} trades · W${s.w} BE${s.b} L${s.l}</div>
          <div style="font-size:11px;color:var(--sub);margin-top:3px">${sn.join(' / ')}</div>
        </div></div>`;
    }).join('');
  }

  const mEntries=Object.entries(d.monthly||{}).sort();
  const mHtml=mEntries.length?mEntries.map(([ym,m])=>`
    <div class="month-row" style="grid-template-columns:90px 110px 50px 70px 1fr 60px 1fr">
      <div style="font-weight:700">${ym}</div>
      <div style="color:${m.r>=0?WIN:LOSS};font-weight:700">${m.r>=0?'+':''}${m.r.toFixed(2)}R</div>
      <div></div>
      <div style="color:${SUB}">${m.trades}</div>
      <div>${rateTrio(m.wr||0, m.be_rate||0, m.loss_rate||0)}</div>
      <div>${scoreBadge(m.score||0)}</div>
      <div></div>
    </div>`).join(''):'<div style="padding:14px;color:var(--sub)">No data.</div>';

  const rolling=d.rolling||{};
  const rollHtml=['r10','r20','r50','all'].map(k=>{
    const r=rolling[k]||{}; const label=k==='all'?'All Time':k.replace('r','Last ');
    const wrCol=r.wr>=50?WIN:r.wr>=35?BE:LOSS;
    const expCol=r.exp>=0?WIN:LOSS;
    return `<div class="roll-cell">
      <div class="roll-label">${label}</div>
      <div class="roll-wr" style="color:${wrCol}">${(r.wr||0).toFixed(1)}%</div>
      <div class="roll-rates">${rateTrio(r.wr||0, r.be_rate||0, r.loss_rate||0)}</div>
      <div class="roll-exp" style="color:${expCol}">${(r.exp||0)>=0?'+':''}${(r.exp||0).toFixed(3)}R/T</div>
      <div class="roll-n" style="color:${SUB}">${r.trades||0} trades</div>
    </div>`;
  }).join('');

  const saHtml=d.streak_alert
    ?`<div class="streak-alert" style="margin-bottom:20px">
        <div class="streak-alert-icon">⚠️</div>
        <div>
          <div style="font-weight:800;color:var(--loss)">Unusual Loss Streak Detected</div>
          <div style="font-size:13px;color:var(--sub);margin-top:3px">Max observed: ${d.max_loss_streak} · Expected max: ~${d.expected_max_loss_streak} for WR ${d.wr.toFixed(0)}%</div>
        </div></div>`
    :`<div class="streak-ok" style="margin-bottom:20px">
        <div class="streak-alert-icon">✅</div>
        <div style="font-weight:700;color:var(--win)">Loss streak (${d.max_loss_streak}) within expected range (~${d.expected_max_loss_streak})</div></div>`;

  let savedNote='';
  try{ savedNote=localStorage.getItem('lst_note_'+d.pair)||''; }catch(e){}

  const btnHtml=D.map((x,j)=>`<button class="pbtn${j===idx?' sel':''}" onclick="renderDetail(${j})">${x.pair}</button>`).join('');

  const kpis=[
    [(d.total_r>=0?'+':'')+d.total_r.toFixed(2)+'R','Total R',col],
    [d.dollar_stats?money(d.dollar_stats.net_pnl):'—','Net PnL',d.dollar_stats&&d.dollar_stats.net_pnl>=0?WIN:LOSS],
    [d.dollar_stats?pct(d.dollar_stats.net_pnl_pct):'—','Net Return',d.dollar_stats&&d.dollar_stats.net_pnl_pct>=0?WIN:LOSS],
    [d.dollar_stats?'-$'+Number(d.dollar_stats.max_drawdown_dollar||0).toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:2}):'—','Max DD $',LOSS],
    [d.wr.toFixed(1)+'%','Win Rate',d.wr>=50?WIN:LOSS],
    [(d.be_rate||0).toFixed(1)+'%','BE Rate',BE],
    [(d.loss_rate||0).toFixed(1)+'%','Loss Rate',LOSS],
    [pfStr(d.pf),'Prof Factor',d.pf==null||d.pf>=1?WIN:LOSS],
    [d.trades,'Trades',ACC],
    [(d.exp>=0?'+':'')+d.exp.toFixed(3)+'R','Expect/T',d.exp>=0?WIN:LOSS],
    ['-'+d.max_dd.toFixed(2)+'R','Max DD',LOSS],
    [d.max_win_streak,'W Streak',WIN],
    [d.max_loss_streak,'L Streak',d.max_loss_streak>=4?LOSS:SUB],
    ['+'+d.best_rr.toFixed(2)+'R','Best Trade',WIN],
    [d.worst_rr.toFixed(2)+'R','Worst Trade',LOSS],
  ].map(([v,l,c])=>`<div class="dkpi"><div class="v" style="color:${c}">${v}</div><div class="l">${l}</div></div>`).join('');

  const detailCols=TRADE_COLUMNS.length?TRADE_COLUMNS:[
    'id','symbol','direction','entry_dt','close_dt','entry','sl','tp1','tp2','rr','outcome','realized_rr'
  ];
  const detailTradeRows=(d.raw_trades||[]).map(t=>`
    <tr>${detailCols.map(col=>{
      const val=fmtTradeVal(t,col);
      if(col==='outcome'){
        return `<td><span class="badge ${t.outcome==='WIN_FULL'?'g':t.outcome==='LOSS'?'r':'y'}">${val}</span></td>`;
      }
      return `<td style="${tradeCellStyle(t,col)}">${val}</td>`;
    }).join('')}<td style="color:${SUB}">${holdStr(t.hold_min)}</td></tr>
  `).join('');

  el.innerHTML=`
  <div class="sh">Pair Detail</div>
  <div class="pbtns">${btnHtml}</div>
  <div class="dpanel">
    <div class="dpair">${d.pair}</div>
    ${saHtml}
    <div class="dkpis">${kpis}</div>
    <div class="sh">Rolling Performance Windows</div>
    <div class="roll-grid" style="margin-bottom:20px">${rollHtml}</div>
    <div class="sh">Equity Curve</div>
    <div class="cbox" style="margin-bottom:20px"><canvas id="det-eq" height="170"></canvas></div>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:18px;margin-bottom:20px">
      <div><div class="sh">Best Hours (${tzOpt})</div><div class="hw-card">${hwRowsLocal(pBest,WIN)}</div></div>
      <div><div class="sh">Worst Hours (${tzOpt})</div><div class="hw-card">${hwRowsLocal(pWorst,LOSS)}</div></div>
    </div>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:18px;margin-bottom:20px">
      <div><div class="sh">Net R by Hour (${tzOpt})</div><div class="cbox"><canvas id="det-hours" height="170"></canvas></div></div>
      <div><div class="sh">Long vs Short</div><div class="cbox"><canvas id="det-dir" height="170"></canvas></div></div>
    </div>
    <div class="sh">RR Distribution</div>
    <div class="rr-dist" style="margin-bottom:20px" id="det-rr"></div>
    <div class="sh">Hold Time by Outcome</div>
    <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin-bottom:20px">
      ${[['Win',d.avg_win_hold,WIN],['Loss',d.avg_loss_hold,LOSS],['Breakeven',d.avg_be_hold,BE]].map(([label,val,c])=>`
      <div class="cbox" style="text-align:center">
        <div style="font-size:11px;font-weight:700;color:var(--sub);text-transform:uppercase;letter-spacing:.1em;margin-bottom:8px">${label}</div>
        <div style="font-size:26px;font-weight:800;color:${c}">${holdStr(val)}</div>
        <div style="font-size:11px;color:var(--sub);margin-top:4px">avg hold time</div>
      </div>`).join('')}
    </div>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:18px;margin-bottom:20px">
      <div>
        <div class="sh">Long Stats</div>
        <div class="cbox">
          <div style="font-size:28px;font-weight:700;color:${d.long_r>=0?WIN:LOSS}">${d.long_r>=0?'+':''}${(d.long_r||0).toFixed(2)}R</div>
          <div style="margin:8px 0">${rateTrio(d.long_wr||0, 0, 100-(d.long_wr||0))}</div>
          <div style="font-size:11px;color:var(--sub);margin-top:4px">${d.long_trades} trades · ${d.long_wins} wins</div>
        </div>
      </div>
      <div>
        <div class="sh">Short Stats</div>
        <div class="cbox">
          <div style="font-size:28px;font-weight:700;color:${d.short_r>=0?WIN:LOSS}">${d.short_r>=0?'+':''}${(d.short_r||0).toFixed(2)}R</div>
          <div style="margin:8px 0">${rateTrio(d.short_wr||0, 0, 100-(d.short_wr||0))}</div>
          <div style="font-size:11px;color:var(--sub);margin-top:4px">${d.short_trades} trades · ${d.short_wins} wins</div>
        </div>
      </div>
    </div>
    <div class="sh">Monthly Breakdown</div>
    <div style="background:var(--card2);border:1px solid var(--brd);border-radius:10px;overflow:hidden;margin-bottom:20px">
      <div class="month-row" style="font-size:11px;color:var(--sub);font-weight:700;letter-spacing:.1em;text-transform:uppercase;background:var(--card2);border-bottom:2px solid var(--brd);grid-template-columns:90px 110px 50px 70px 1fr 60px 1fr">
        <div>Month</div><div>Net R</div><div></div><div>Trades</div><div>W% / BE% / L%</div><div>Score</div><div></div>
      </div>${mHtml}
    </div>
    <div class="sh">Trades With Accounting</div>
    <div class="tbl-wrap" style="margin-bottom:20px">
      <table>
        <thead><tr>${detailCols.map(c=>`<th>${c}</th>`).join('')}<th>hold_min</th></tr></thead>
        <tbody>${detailTradeRows}</tbody>
      </table>
    </div>
    <div class="sh">Notes</div>
    <div class="cbox">
      <textarea class="notes-area" id="det-notes" placeholder="Add your observations about this pair…">${savedNote}</textarea>
      <div style="display:flex;align-items:center;justify-content:space-between;margin-top:10px">
        <span class="notes-saved" id="notes-saved-lbl">✓ Saved</span>
        <button class="pbtn" id="notes-clear-btn">Clear</button>
      </div>
    </div>
  </div>`;

  renderRRDist('det-rr',d.rr_buckets||{});

  const notesEl=document.getElementById('det-notes');
  const savedLbl=document.getElementById('notes-saved-lbl');
  let _nt;
  notesEl.addEventListener('input',()=>{
    clearTimeout(_nt);
    _nt=setTimeout(()=>{
      try{ localStorage.setItem('lst_note_'+d.pair,notesEl.value); }catch(e){}
      savedLbl.classList.add('show');
      setTimeout(()=>savedLbl.classList.remove('show'),2000);
    },600);
  });
  document.getElementById('notes-clear-btn').addEventListener('click',()=>{
    notesEl.value='';
    try{ localStorage.removeItem('lst_note_'+d.pair); }catch(e){}
  });

  setTimeout(()=>{
    drawLine('det-eq',d.equity,col,170);
    const hrs=Object.keys(pHourAgg).map(Number).sort((a,b)=>a-b);
    drawBars('det-hours',hrs.map(h=>String(h).padStart(2,'0')),hrs.map(h=>+pHourAgg[h].r.toFixed(2)),170);
    drawGroupBars('det-dir',['LONG','SHORT'],[[d.long_wins,(d.long_trades||0)-d.long_wins],[d.short_wins,(d.short_trades||0)-d.short_wins]],170);
  },50);
}

// ════════════════════════════════════════════════════════════════════════
// CANVAS HELPERS
// ════════════════════════════════════════════════════════════════════════
function getCtx(id,H){
  const c=document.getElementById(id); if(!c) return null;
  const dpr=window.devicePixelRatio||1;
  const W=c.parentElement.offsetWidth-40||460;
  c.width=W*dpr; c.height=H*dpr;
  c.style.width=W+'px'; c.style.height=H+'px';
  const ctx=c.getContext('2d'); ctx.scale(dpr,dpr);
  return {ctx,W,H};
}
function drawLine(id,data,color,H=170){
  const g=getCtx(id,H); if(!g||data.length<2) return;
  const {ctx,W}=g;
  const pad={t:14,r:14,b:24,l:54};
  const cw=W-pad.l-pad.r, ch=H-pad.t-pad.b;
  const mn=Math.min(0,...data), mx=Math.max(0,...data), range=mx-mn||1;
  const sy=v=>pad.t+ch*(1-(v-mn)/range);
  const sx=i=>pad.l+(i/(data.length-1))*cw;
  ctx.fillStyle='#f7f8fa'; ctx.fillRect(0,0,W,H);
  [0,.25,.5,.75,1].forEach(t=>{ const y=pad.t+t*ch; ctx.strokeStyle='#e8eaf0'; ctx.lineWidth=1; ctx.beginPath(); ctx.moveTo(pad.l,y); ctx.lineTo(pad.l+cw,y); ctx.stroke(); });
  ctx.strokeStyle='#c4c9d6'; ctx.lineWidth=1.5; ctx.setLineDash([5,4]);
  ctx.beginPath(); ctx.moveTo(pad.l,sy(0)); ctx.lineTo(pad.l+cw,sy(0)); ctx.stroke(); ctx.setLineDash([]);
  const grad=ctx.createLinearGradient(0,pad.t,0,pad.t+ch);
  grad.addColorStop(0,color+'40'); grad.addColorStop(1,color+'08');
  ctx.beginPath(); ctx.moveTo(sx(0),sy(0));
  data.forEach((v,i)=>ctx.lineTo(sx(i),sy(v)));
  ctx.lineTo(sx(data.length-1),sy(0)); ctx.closePath(); ctx.fillStyle=grad; ctx.fill();
  ctx.beginPath(); ctx.strokeStyle=color; ctx.lineWidth=2.5;
  data.forEach((v,i)=>i===0?ctx.moveTo(sx(i),sy(v)):ctx.lineTo(sx(i),sy(v))); ctx.stroke();
  [mn,0,mx].forEach(v=>{ ctx.fillStyle=v===0?'#9aa0b4':(v>0?WIN:LOSS); ctx.font='bold 11px Inter,sans-serif'; ctx.textAlign='right'; ctx.fillText((v>=0?'+':'')+v.toFixed(1)+'R',pad.l-6,sy(v)+4); });
}
function drawBars(id,labels,values,H=220){
  const g=getCtx(id,H); if(!g) return;
  const {ctx,W}=g;
  const pad={t:14,r:14,b:26,l:54};
  const cw=W-pad.l-pad.r, ch=H-pad.t-pad.b;
  const mn=Math.min(0,...values), mx=Math.max(0,...values), range=mx-mn||1;
  const sy=v=>pad.t+ch*(1-(v-mn)/range);
  const zy=sy(0); const bw=Math.max(4,cw/labels.length*0.6); const gap=cw/labels.length;
  ctx.fillStyle='#f7f8fa'; ctx.fillRect(0,0,W,H);
  [0,.25,.5,.75,1].forEach(t=>{ const y=pad.t+t*ch; ctx.strokeStyle='#e8eaf0'; ctx.lineWidth=1; ctx.beginPath(); ctx.moveTo(pad.l,y); ctx.lineTo(pad.l+cw,y); ctx.stroke(); });
  ctx.strokeStyle='#c4c9d6'; ctx.lineWidth=1.5; ctx.setLineDash([5,4]); ctx.beginPath(); ctx.moveTo(pad.l,zy); ctx.lineTo(pad.l+cw,zy); ctx.stroke(); ctx.setLineDash([]);
  labels.forEach((lbl,i)=>{
    const v=values[i]; const x=pad.l+i*gap+gap/2-bw/2;
    const barTop=v>=0?sy(v):zy; const barBottom=v>=0?zy:sy(v); const barH=Math.max(1,barBottom-barTop);
    ctx.fillStyle=v>=0?WIN+'dd':LOSS+'dd';
    ctx.beginPath(); ctx.roundRect(x,barTop,bw,barH,3); ctx.fill();
    ctx.fillStyle=SUB; ctx.font='bold 10px Inter,sans-serif'; ctx.textAlign='center'; ctx.fillText(lbl,x+bw/2,H-6);
  });
  [mn,0,mx].forEach(v=>{ ctx.fillStyle=v===0?'#9aa0b4':(v>0?WIN:LOSS); ctx.font='bold 11px Inter,sans-serif'; ctx.textAlign='right'; ctx.fillText((v>=0?'+':'')+v.toFixed(1)+'R',pad.l-6,sy(v)+4); });
}
function drawCustomBars(id,labels,values,colors,H=220){
  const g=getCtx(id,H); if(!g) return;
  const {ctx,W}=g;
  const pad={t:24,r:14,b:36,l:54};
  const cw=W-pad.l-pad.r, ch=H-pad.t-pad.b;
  const mn=Math.min(0,...values), mx=Math.max(0,...values), range=mx-mn||1;
  const sy=v=>pad.t+ch*(1-(v-mn)/range); const zy=sy(0);
  const bw=Math.max(8,cw/labels.length*0.55); const gap=cw/labels.length;
  ctx.fillStyle='#f7f8fa'; ctx.fillRect(0,0,W,H);
  [0,.25,.5,.75,1].forEach(t=>{ const y=pad.t+ch*(1-t); ctx.strokeStyle='#e8eaf0'; ctx.lineWidth=1; ctx.beginPath(); ctx.moveTo(pad.l,y); ctx.lineTo(pad.l+cw,y); ctx.stroke(); });
  ctx.strokeStyle='#c4c9d6'; ctx.lineWidth=1.5; ctx.setLineDash([5,4]); ctx.beginPath(); ctx.moveTo(pad.l,zy); ctx.lineTo(pad.l+cw,zy); ctx.stroke(); ctx.setLineDash([]);
  labels.forEach((lbl,i)=>{
    const v=values[i]; const x=pad.l+i*gap+gap/2-bw/2;
    const barTop=v>=0?sy(v):zy; const barBottom=v>=0?zy:sy(v); const barH=Math.max(1,barBottom-barTop);
    ctx.fillStyle=(colors[i]||ACC)+'cc'; ctx.beginPath(); ctx.roundRect(x,barTop,bw,barH,4); ctx.fill();
    ctx.fillStyle=colors[i]||ACC; ctx.font='bold 11px Inter,sans-serif'; ctx.textAlign='center';
    ctx.fillText((v>=0?'+':'')+v.toFixed(1)+'R',x+bw/2,v>=0?barTop-6:barBottom+14);
    ctx.fillStyle=SUB; ctx.font='bold 11px Inter,sans-serif'; ctx.fillText(lbl,x+bw/2,H-6);
  });
  [mn,0,mx].forEach(v=>{ ctx.fillStyle=v===0?'#9aa0b4':(v>0?WIN:LOSS); ctx.font='bold 11px Inter,sans-serif'; ctx.textAlign='right'; ctx.fillText((v>=0?'+':'')+v.toFixed(1)+'R',pad.l-6,sy(v)+4); });
}
function drawGroupBars(id,labels,groups,H=170){
  const g=getCtx(id,H); if(!g) return;
  const {ctx,W}=g;
  const pad={t:14,r:14,b:26,l:14};
  const cw=W-pad.l-pad.r, ch=H-pad.t-pad.b;
  const maxV=Math.max(...groups.map(g=>g[0]+g[1]),1);
  const sy=v=>pad.t+ch*(1-v/maxV);
  ctx.fillStyle='#f7f8fa'; ctx.fillRect(0,0,W,H);
  [.25,.5,.75,1].forEach(t=>{ const y=pad.t+ch*(1-t); ctx.strokeStyle='#e8eaf0'; ctx.lineWidth=1; ctx.beginPath(); ctx.moveTo(pad.l,y); ctx.lineTo(pad.l+cw,y); ctx.stroke(); });
  const gw=cw/labels.length, bw=gw*0.3;
  labels.forEach((lbl,i)=>{
    const [w,l]=groups[i]; const gx=pad.l+i*gw+gw/2;
    [[w,WIN+'cc',-(bw+3)],[l,LOSS+'cc',3]].forEach(([v,c,ox])=>{
      const barH=Math.max(1,v/maxV*ch);
      ctx.fillStyle=c; ctx.beginPath(); ctx.roundRect(gx+ox,sy(v),bw,barH,3); ctx.fill();
    });
    ctx.fillStyle=SUB; ctx.font='bold 11px Inter,sans-serif'; ctx.textAlign='center'; ctx.fillText(lbl,gx,H-6);
    ctx.fillStyle='#9aa0b4'; ctx.font='10px Inter,sans-serif'; ctx.fillText('W'+w+'/L'+l,gx,sy(w+l)-5);
  });
}

// ════════════════════════════════════════════════════════════════════════
// ── TF PAIRS (with streak visualization) ───────────────────────────────────
(function(){
  const el=document.getElementById('v-tfpairs');
  const tfs=Object.values(TFS);
  if(!tfs.length){
    el.innerHTML='<div class="sh">TF Pair Comparison</div><p style="color:var(--sub);padding:20px">No htf_interval/ltf_interval columns found. Run backtest with TF_PAIRS env var set.</p>';
    return;
  }

  const byTf={};
  D.forEach(d=>{
    const tf=d.tf_pair||'unknown';
    if(!byTf[tf]) byTf[tf]={tf, symbols:[], trades:0, w:0, l:0, b:0, r:0, 
                              maxWinStreak:0, maxLossStreak:0, 
                              streakLog:[],
                              rrBuckets:{ '1.5R':0, '2R':0, '2.5R':0, '3R+':0 }};
    byTf[tf].symbols.push(d.symbol||d.pair);
    byTf[tf].trades+=d.trades;
    byTf[tf].w+=d.wins;
    byTf[tf].l+=d.losses;
    byTf[tf].b+=d.bes;
    byTf[tf].r+=d.total_r;
    byTf[tf].maxWinStreak = Math.max(byTf[tf].maxWinStreak, d.max_win_streak||0);
    byTf[tf].maxLossStreak = Math.max(byTf[tf].maxLossStreak, d.max_loss_streak||0);
    
// Calculate RR distribution from raw trades (1.5R minimum)
(d.raw_trades||[]).forEach(t=>{
  if(t.outcome!=='WIN_FULL') return;
  const rr = Math.abs(t.realized_rr);
  if(rr < 1.5) return;  // Skip anything below 1.5R
  else if(rr < 2.0) byTf[tf].rrBuckets['1.5R']++;
  else if(rr < 2.5) byTf[tf].rrBuckets['2R']++;
  else if(rr < 3.0) byTf[tf].rrBuckets['2.5R']++;
  else byTf[tf].rrBuckets['3R+']++;
});
    
    // Record significant streaks (3+)
    let curW=0, curL=0, lastRecorded='';
    (d.raw_trades||[]).forEach(t=>{
      if(t.outcome==='WIN_FULL'){
        curW++; curL=0;
        if(curW>=3 && curW>parseInt(lastRecorded.replace(/[^0-9]/g,'')||0)){
          lastRecorded=`W${curW}`;
          byTf[tf].streakLog.push({symbol:d.symbol, streak:`🔥${curW}W`, date:t.entry_dt?.slice(0,10)});
        }
      } else if(t.outcome==='LOSS'){
        curL++; curW=0;
        if(curL>=3 && curL>parseInt(lastRecorded.replace(/[^0-9]/g,'')||0)){
          lastRecorded=`L${curL}`;
          byTf[tf].streakLog.push({symbol:d.symbol, streak:`💀${curL}L`, date:t.entry_dt?.slice(0,10)});
        }
      } else {
        curW=0; curL=0; lastRecorded='';
      }
    });
  });
  
  const tfList=Object.values(byTf).sort((a,b)=>b.r-a.r);
  const cols=['#4f6ef7','#0d9e5c','#d63b3b','#b07d00','#7c5cbf','#0e8fca'];
  const tfColor=tf=>cols[tfList.findIndex(t=>t.tf===tf)%cols.length];

  function streakBar(maxStreak, type){
    const limit=Math.min(maxStreak,10);
    const color=type==='win'?WIN:LOSS;
    let bars='';
    for(let i=1;i<=limit;i++){
      const opacity=40 + Math.floor((i/maxStreak)*40);
      bars+=`<div style="flex:1;height:6px;background:${color}${opacity.toString(16)};border-radius:2px" title="${i} ${type} streak"></div>`;
    }
    return `<div style="display:flex;gap:2px;margin-top:6px">${bars}</div>`;
  }

  function renderRRBuckets(buckets, totalWins) {
  if(totalWins === 0) return '<div style="font-size:11px;color:var(--sub);text-align:center">No wins</div>';
  
  const entries = [
    { label: '1.5R', key: '1.5R', color: '#0d9e5c', min: 1.5, max: 1.99 },
    { label: '2R', key: '2R', color: '#0e8fca', min: 2.0, max: 2.49 },
    { label: '2.5R', key: '2.5R', color: '#7c5cbf', min: 2.5, max: 2.99 },
    { label: '3R+', key: '3R+', color: '#4f6ef7', min: 3.0, max: 999 }
  ];
  
  let html = '<div style="margin-top:8px;padding-top:6px;border-top:1px solid var(--brd)">';
  html += '<div style="font-size:9px;color:var(--sub);font-weight:700;text-transform:uppercase;letter-spacing:.08em;margin-bottom:4px;text-align:center">RR DISTRIBUTION</div>';
  html += '<div style="display:flex;gap:4px;justify-content:space-around">';
  
  entries.forEach(e => {
    const cnt = buckets[e.key] || 0;
    const pct = totalWins ? ((cnt / totalWins) * 100).toFixed(0) : 0;
    html += `<div style="text-align:center;flex:1">
      <div style="font-size:11px;font-weight:800;color:${e.color}">${e.label}</div>
      <div style="font-size:16px;font-weight:800;color:${e.color}">${cnt}</div>
      <div style="height:3px;background:${e.color}33;border-radius:2px;margin-top:2px">
        <div style="width:${pct}%;height:3px;background:${e.color};border-radius:2px"></div>
      </div>
      <div style="font-size:9px;color:var(--sub)">${pct}%</div>
    </div>`;
  });
  
  // Calculate average win (using midpoints)
  let totalRR = 0;
  Object.entries(buckets).forEach(([k,v]) => {
    if(k === '1.5R') totalRR += v * 1.75;
    else if(k === '2R') totalRR += v * 2.25;
    else if(k === '2.5R') totalRR += v * 2.75;
    else if(k === '3R+') totalRR += v * 4.0;
  });
  const avgWin = totalWins ? (totalRR / totalWins).toFixed(2) : '0';
  
  html += `</div><div style="font-size:9px;color:var(--sub);text-align:center;margin-top:4px">⚡ Avg Win: ${avgWin}R</div></div>`;
  return html;
}

  const cards=tfList.map(tf=>{
    const r2=ratesFromCounts(tf.w,tf.b,tf.l);
    const exp=(tf.r/tf.trades).toFixed(3);
    const col=tfColor(tf.tf);
    const rc=tf.r>=0?WIN:LOSS;
    const symbols=[...new Set(tf.symbols)].slice(0,6).join(', ');
    const moreSymbols = [...new Set(tf.symbols)].length > 6 ? ` +${[...new Set(tf.symbols)].length-6} more` : '';
    const totalWins = tf.w;
    
    const recentStreaks = tf.streakLog.slice(-3).map(s=> 
      `<span style="font-size:10px;background:${s.streak.includes('W')?WIN+'22':LOSS+'22'};padding:2px 5px;border-radius:4px;margin-right:4px" title="${s.symbol} · ${s.date}">${s.streak}</span>`
    ).join('');
    
    return `<div class="sess-card" style="border-color:${col}44">
      <div class="sc-bg" style="background:${col}"></div>
      <div class="sess-name" style="color:${col};font-size:15px">${tf.tf||'—'}</div>
      <div class="sess-time" style="margin-bottom:10px">${[...new Set(tf.symbols)].length} symbols · ${symbols}${moreSymbols}</div>
      <div class="sess-r" style="color:${rc}">${tf.r>=0?'+':''}${tf.r.toFixed(2)}R</div>
      <div class="sess-meta">${tf.trades} trades · ${exp>=0?'+':''}${exp}R/T</div>
      <div style="margin:10px 0">${rateTrio(r2.wr,r2.ber,r2.lr)}</div>
      
      <!-- RR DISTRIBUTION -->
      ${renderRRBuckets(tf.rrBuckets, totalWins)}
      
      <div style="display:flex;gap:12px;margin:10px 0;padding:8px 0;border-top:1px solid ${col}33;border-bottom:1px solid ${col}33">
        <div style="flex:1;text-align:center">
          <div style="font-size:10px;color:${WIN};font-weight:700">MAX WIN STREAK</div>
          <div style="font-size:20px;font-weight:800;color:${WIN}">${tf.maxWinStreak}</div>
          ${streakBar(tf.maxWinStreak, 'win')}
        </div>
        <div style="flex:1;text-align:center">
          <div style="font-size:10px;color:${LOSS};font-weight:700">MAX LOSS STREAK</div>
          <div style="font-size:20px;font-weight:800;color:${LOSS}">${tf.maxLossStreak}</div>
          ${streakBar(tf.maxLossStreak, 'loss')}
        </div>
      </div>
      
      ${recentStreaks ? `<div style="margin:6px 0;font-size:10px;color:${SUB}">📊 Recent: ${recentStreaks}</div>` : ''}
      
      ${tf.maxLossStreak >= 5 ? `<div style="margin-top:8px;padding:6px;background:${LOSS}22;border-radius:6px;font-size:11px;color:${LOSS};text-align:center">⚠️ ${tf.maxLossStreak} consecutive losses — review this TF pair</div>` : ''}
      
      <div class="sess-bar-track" style="margin:10px 0 6px 0">
        <div class="sess-bar-fill" style="width:${Math.min(100,Math.abs(tf.r)/Math.max(...tfList.map(t=>Math.abs(t.r)),1)*100)}%;background:${rc}"></div>
      </div>
      <div class="sess-counts">
        <span style="color:${WIN}">✓ ${tf.w}</span>
        <span style="color:${BE}">≈ ${tf.b}</span>
        <span style="color:${LOSS}">✗ ${tf.l}</span>
      </div>
    </div>`;
  }).join('');

  const allSymbols=[...new Set(D.map(d=>d.symbol||d.pair))].sort();
  const allTfPairs=[...new Set(D.map(d=>d.tf_pair||''))].filter(Boolean).sort();

  const symTfMap={};
  D.forEach(d=>{
    const sym=d.symbol||d.pair;
    const tf=d.tf_pair||'';
    if(!symTfMap[sym]) symTfMap[sym]={};
    symTfMap[sym][tf]=d;
  });

  const colHead=allTfPairs.map(tf=>`<th style="color:${tfColor(tf)};font-family:monospace">${tf}</th>`).join('');
  
  const symRows=allSymbols.map(sym=>{
    const cells=allTfPairs.map(tf=>{
      const d=symTfMap[sym]?.[tf];
      if(!d) return `<td style="color:${DIM}">—</td>`;
      const col=d.total_r>=0?WIN:LOSS;
      let streakBadge='';
      if(d.max_loss_streak >= 5) streakBadge=`<span style="display:inline-block;margin-left:4px;background:${LOSS}22;color:${LOSS};font-size:9px;padding:1px 4px;border-radius:4px">💀${d.max_loss_streak}</span>`;
      else if(d.max_win_streak >= 5) streakBadge=`<span style="display:inline-block;margin-left:4px;background:${WIN}22;color:${WIN};font-size:9px;padding:1px 4px;border-radius:4px">🔥${d.max_win_streak}</span>`;
      else if(d.max_loss_streak >= 3) streakBadge=`<span style="display:inline-block;margin-left:4px;color:${LOSS};font-size:9px">⚠${d.max_loss_streak}</span>`;
      
      return `<td style="cursor:pointer" onclick="showDetail(${D.indexOf(d)})" title="${d.pair} · Max loss streak: ${d.max_loss_streak} · Max win streak: ${d.max_win_streak}">
        <div style="color:${col};font-weight:700">${d.total_r>=0?'+':''}${d.total_r.toFixed(2)}R${streakBadge}</div>
        <div style="margin:4px 0">${rateTrio(d.wr, d.be_rate||0, d.loss_rate||0)}</div>
        <div style="font-size:11px;color:${SUB}">${d.trades}t | L${d.max_loss_streak}/W${d.max_win_streak}</div>
      </td>`;
    }).join('');
    return `<tr><td style="font-weight:700">${sym}</td>${cells}</tr>`;
  }).join('');

  const barLabels=tfList.map(t=>t.tf||'—');
  const barVals=tfList.map(t=>+t.r.toFixed(2));
  const barCols=tfList.map(t=>tfColor(t.tf));

  el.innerHTML=`
  <div class="sh">TF Pair Summary — with Win/Loss Streaks & RR Distribution</div>
  <div class="sess-grid">${cards}</div>

  <div class="sh">Net R by TF Pair (all symbols combined)</div>
  <div class="cbox" style="margin-bottom:28px"><canvas id="cv-tf-bars" height="200"></canvas></div>

  <div class="sh">Symbol × TF Pair Matrix — click any cell to open detail</div>
  <div class="tbl-wrap" style="margin-bottom:28px">
    <table>
      <thead><tr><th>Symbol</th>${colHead}</tr></thead>
      <tbody>${symRows}</tbody>
    </table>
  </div>

  <div class="sh">TF Pair Comparison Table</div>
  <div class="tbl-wrap">
    <table>
      <thead><tr>
        <th>TF Pair</th><th>Symbols</th><th>Trades</th>
        <th>W</th><th>BE</th><th>L</th>
        <th>W% / BE% / L%</th><th>Total R</th><th>Expect/T</th>
        <th>Max Streaks</th>
      </tr></thead>
      <tbody>
        ${tfList.map(tf=>{
          const r2=ratesFromCounts(tf.w,tf.b,tf.l);
          const exp=tf.r/tf.trades;
          const col=tfColor(tf.tf);
          return `<tr>
            <td style="font-weight:800;font-family:monospace;color:${col}">${tf.tf||'—'}</td>
            <td style="color:${SUB}">${[...new Set(tf.symbols)].length}</td>
            <td style="font-weight:600">${tf.trades}</td>
            <td style="color:${WIN};font-weight:700">${tf.w}</td>
            <td style="color:${BE};font-weight:600">${tf.b}</td>
            <td style="color:${LOSS};font-weight:700">${tf.l}</td>
            <td>${rateTrio(r2.wr,r2.ber,r2.lr)}</td>
            <td style="color:${tf.r>=0?WIN:LOSS};font-weight:700">${tf.r>=0?'+':''}${tf.r.toFixed(2)}R</td>
            <td style="color:${exp>=0?WIN:LOSS};font-weight:600">${exp>=0?'+':''}${exp.toFixed(3)}R</td>
            <td style="font-size:11px"><span style="color:${WIN}">W${tf.maxWinStreak}</span> / <span style="color:${LOSS}">L${tf.maxLossStreak}</span></td>
          </tr>`;
        }).join('')}
      </tbody>
    </table>
  </div>`;

  setTimeout(()=>drawCustomBars('cv-tf-bars',barLabels,barVals,barCols,200),80);
})();

// ── INIT ──────────────────────────────────────────────────────────────────────
renderSessions();
renderHours();

let _rt;
window.addEventListener('resize',()=>{ clearTimeout(_rt); _rt=setTimeout(()=>document.querySelector('.ntab.on')?.click(),200); });
</script>
</body>
</html>"""

HTML = HTML_TEMPLATE.replace("__DATA_JSON__", data_json)

out_path = results_dir / "report.html"
out_path.write_text(HTML, encoding="utf-8")
print(f"  HTML report  -> {out_path}")
if open_browser:
    webbrowser.open(out_path.resolve().as_uri())
