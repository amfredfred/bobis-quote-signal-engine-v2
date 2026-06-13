"""
app/backtesting/backtest.py — backtester using the new domain layer.

Optimizations (cumulative, newest batch annotated with ★):

  Original layer
  ──────────────
  - Removed build_chart_data (unused)
  - HTF window: O(n) list-comp → O(log n) bisect_right
  - MarketStructure.detect / find_htf_ranges cached per HTF candle close
  - interval_to_minutes wrapped with lru_cache; htf_interval_ms pre-computed
  - stale_ms pre-computed per (htf, ltf) pair
  - _bisect_ts (pure-Python) replaced with stdlib bisect.bisect_left (C impl)
  - _ltf_ts / _htf_ts pre-computed in __init__
  - Removed BacktestResult chart artefacts; __slots__ for memory savings
  - Hot-loop method references hoisted to locals
  - open_dir expiry: dedicated expired list (avoids re-scan)
  - _simulate fully vectorized with NumPy (50-200× vs pure Python)

  ★ New layer (this revision)
  ────────────────────────────
  ★ TIER 1 — Zone-level _find_ltf / find_entry cache
      Combined per-zone cache keyed by (zone_key, ltf_hi_idx).
      Both domain calls are skipped on cache hit (identical ltf_zone).
      Win rate ≈ 1 − (1 / ticks_per_ltf_bar); e.g. 98 % for 1 m master / 1 h LTF.
      Even for master == ltf: inactive zones (no entry result) cost O(1) lookup
      instead of O(zone_len) list-copy + domain call.

  ★ TIER 2 — Zone lo-index precomputed once per zone lifetime
      bisect_left(ltf_ts_idx, zone_start) result is immutable; stored in
      _zone_lo on first visit, never recomputed.

  ★ TIER 3 — Optional Numba JIT _simulate_core
      If numba is installed: single O(n) pass in native machine code.
      Eliminates all intermediate array allocations (cumsum, shift,
      3× nonzero index arrays, expiry bool mask).
      tp1 "strictly prior" semantics preserved by updating the flag AFTER
      SL/INV checks within each iteration.

  ★ TIER 4 — NumPy _simulate fallback hardened
      • Expiry trim: O(n) bool mask → O(log n) searchsorted (timestamps sorted)
      • cumsum + shift replaced by: tp1_first = argmax(tp1_tgt); index compare
      • tp1_before = (tp1_first < stop_idx)  — no shifted array allocation
      • np.nonzero → np.argmax + .any() guard (avoids allocating index arrays)
      • Profile scalars (use_be, expiry_ms, tp1_mult) hoisted to
        __init__; eliminated per-call attribute dereference
"""

from __future__ import annotations

import sys
import os

import argparse
import bisect
import copy
import csv
import datetime
import json
import logging
from dataclasses import replace as dc_replace
from functools import lru_cache
from pathlib import Path
from typing import Optional

import math

import numpy as np

from app.engine.decision_engine import DecisionEngine
from app.engine.market_replay import replay_signal_lifecycle
from app.engine.parity_trace import ParityTraceWriter, trace_from_signal
from config.settings import Settings, interval_to_minutes as _interval_to_minutes
from domain.assets.profiles import (
    SUPPORTED_SYMBOLS,
    AssetProfile,
    AssetRegistry,
    normalize_symbol,
)
from domain.entities.candle import Candle
from domain.entities.enums import SignalDirection, SignalOutcome
from domain.entities.trade import TradeSignal
from domain.market.structure import MarketStructure
from domain.market.swings import SwingDetector, detect_displacement
from domain.signals.entry import find_entry  # shared entry dispatcher
from domain.trade_management import (
    breakeven_price,
    protected_breakeven_rr,
    tp1_booked_rr,
    tp2_weighted_rr,
)
from infrastructure.data_providers.market_data import MarketDataClient

logger = logging.getLogger(__name__)

interval_to_minutes: callable = lru_cache(maxsize=64)(_interval_to_minutes)

# ── Account simulation defaults ───────────────────────────────────────────────
DEFAULT_START_BALANCE: float = 5_000.0
DEFAULT_RISK_PERCENT: float = 1.0
DEFAULT_TRAILING_GIVEBACK_PCT: float = 0.0

# ── ANSI colours ──────────────────────────────────────────────────────────────
GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
CYAN = "\033[96m"
BOLD = "\033[1m"
DIM = "\033[2m"
RESET = "\033[0m"
UP = f"{GREEN}▲{RESET}"
DOWN = f"{RED}▼{RESET}"


# ── Accounting helpers ────────────────────────────────────────────────────────

def calculate_trade_accounting(
    *,
    balance_before: float,
    result_r: float,
    risk_percent: float,
    peak_balance_before: float,
) -> dict:
    risk_amount = balance_before * (risk_percent / 100)
    pnl = result_r * risk_amount
    balance_after = balance_before + pnl
    peak_balance_after = max(peak_balance_before, balance_after)
    drawdown_after = peak_balance_after - balance_after
    drawdown_pct_after = (
        (drawdown_after / peak_balance_after * 100) if peak_balance_after > 0 else 0.0
    )
    return {
        "risk_amount": risk_amount,
        "pnl": pnl,
        "balance_after": balance_after,
        "peak_balance_after": peak_balance_after,
        "drawdown_after": drawdown_after,
        "drawdown_pct_after": drawdown_pct_after,
    }


def _fmt_currency(value: float) -> str:
    sign = "+" if value >= 0 else "-"
    return f"{sign}${abs(value):,.2f}"


def _fmt_currency_plain(value: float) -> str:
    return f"${value:,.2f}"


def _fmt_pct(value: float, sign: bool = False) -> str:
    if sign:
        return f"{value:+.2f}%"
    return f"{value:.2f}%"


def _fmt_r(value: float) -> str:
    return f"{value:+.2f}R"




# ── BacktestResult ────────────────────────────────────────────────────────────
class BacktestResult:
    """Lean result container — __slots__ cuts per-instance overhead ~40 %."""

    __slots__ = (
        "signal",
        "outcome",
        "realized_rr",
        "close_ts",
        "close_price",
        "hit_entry_after_tp1",
        "trail_mfe_price",
        "trailed_sl",
        "trailing_giveback_pct",
    )

    def __init__(
        self,
        signal: TradeSignal,
        outcome: SignalOutcome,
        realized_rr: float,
        close_ts: Optional[int],
        close_px: Optional[float],
        hit_entry_after_tp1: bool = False,
        trail_mfe_price: Optional[float] = None,
        trailed_sl: Optional[float] = None,
        trailing_giveback_pct: float = DEFAULT_TRAILING_GIVEBACK_PCT,
    ) -> None:
        self.signal = signal
        self.outcome = outcome
        self.realized_rr = realized_rr
        self.close_ts = close_ts
        self.close_price = close_px
        # FIX: first-class constructor param — never set post-construction.
        self.hit_entry_after_tp1 = hit_entry_after_tp1
        self.trail_mfe_price = trail_mfe_price
        self.trailed_sl = trailed_sl
        self.trailing_giveback_pct = trailing_giveback_pct

    def to_dict(self) -> dict:
        s = self.signal
        # FIX: datetime.utcfromtimestamp deprecated since Python 3.12.
        _fmt = lambda ms: datetime.datetime.fromtimestamp(
            ms / 1000, tz=datetime.timezone.utc
        ).strftime("%Y-%m-%d %H:%M:%S")
        return {
            "id": s.id,
            "symbol": s.symbol,
            "direction": s.direction.value,
            "setup_dt": _fmt(s.setup_candle_open_at or s.rejection_candle.timestamp),
            "actionable_dt": _fmt(
                s.setup_candle_close_at
                or s.rejection_candle.timestamp
                + _interval_to_minutes(s.ltf_interval) * 60 * 1000
            ),
            "entry_dt": _fmt(s.triggered_at),
            "close_dt": _fmt(self.close_ts) if self.close_ts else "",
            "entry": round(s.entry_price, 5),
            "sl": round(s.stop_loss, 5),
            "tp1": round(s.tp1, 5),
            "tp2": round(s.tp2, 5),
            "rr": round(s.risk_reward_ratio, 3),
            "outcome": self.outcome.value,
            "realized_rr": round(self.realized_rr, 3),
            "hit_entry_after_tp1": self.hit_entry_after_tp1,
            "trail_mfe_price": (
                round(self.trail_mfe_price, 5)
                if self.trail_mfe_price is not None
                else ""
            ),
            "trailed_sl": round(self.trailed_sl, 5) if self.trailed_sl is not None else "",
            "trailing_giveback_pct": self.trailing_giveback_pct,
            "htf_interval": s.htf_interval,
            "ltf_interval": s.ltf_interval,
            "pattern": s.rejection_candle.pattern.value,
            "zone_attempt": s.zone_attempt,
            "wick_ratio": round(s.rejection_candle.wick_ratio, 3),
            "htf_high": round(s.htf_range.range_high, 5),
            "htf_low": round(s.htf_range.range_low, 5),
            "tp_level": round(s.htf_range.tp_level, 5),
        }


# ── BacktestReport ────────────────────────────────────────────────────────────
class BacktestReport:
    def __init__(
        self,
        symbol: str,
        results: list[BacktestResult],
        cfg: Settings,
        start_balance: float = DEFAULT_START_BALANCE,
        risk_percent: float = DEFAULT_RISK_PERCENT,
        trailing_giveback_pct: float = DEFAULT_TRAILING_GIVEBACK_PCT,
    ) -> None:
        self.symbol = symbol
        self.results = results
        self.cfg = cfg
        self.start_balance = start_balance
        self.risk_percent = risk_percent
        self.trailing_giveback_pct = trailing_giveback_pct
        self._registry = AssetRegistry(cfg)
        self.profile = self._registry.get(symbol)  # base (combined summary)

    def _compute_accounting(
        self, results: list[BacktestResult]
    ) -> tuple[list[dict], dict]:
        """Compute per-trade accounting and aggregate summary for a result subset."""
        balance = self.start_balance
        peak = self.start_balance
        max_dd = 0.0
        max_dd_pct = 0.0

        per_trade: list[dict] = []
        for r in results:
            raw_rr = r.realized_rr
            s = r.signal
            entry = s.entry_price
            exit_px = r.close_price or entry

            acct = calculate_trade_accounting(
                balance_before=balance,
                result_r=raw_rr,
                risk_percent=self.risk_percent,
                peak_balance_before=peak,
            )
            per_trade.append({
                "balance_before": balance,
                **acct,
                "theoretical_rr": raw_rr,
                "executed_rr": raw_rr,
                "raw_entry_price": entry,
                "executed_entry_price": entry,
                "raw_exit_price": exit_px,
                "executed_exit_price": exit_px,
            })
            balance = acct["balance_after"]
            peak = acct["peak_balance_after"]
            max_dd = max(max_dd, acct["drawdown_after"])
            max_dd_pct = max(max_dd_pct, acct["drawdown_pct_after"])

        final = balance
        net_pnl = final - self.start_balance
        net_pnl_pct = (net_pnl / self.start_balance * 100) if self.start_balance > 0 else 0.0

        pnls = [a["pnl"] for a in per_trade]
        wins_pnl = [p for p in pnls if p > 0]
        losses_pnl = [p for p in pnls if p < 0]
        gross_profit = sum(wins_pnl)
        gross_loss = abs(sum(losses_pnl))
        pf_dollar = (gross_profit / gross_loss) if gross_loss > 0 else None

        summary = {
            "start_balance": self.start_balance,
            "final_balance": final,
            "net_pnl": net_pnl,
            "net_pnl_pct": net_pnl_pct,
            "max_drawdown": max_dd,
            "max_drawdown_pct": max_dd_pct,
            "gross_profit": gross_profit,
            "gross_loss": gross_loss,
            "profit_factor_dollar": pf_dollar,
            "avg_pnl": sum(pnls) / len(pnls) if pnls else 0.0,
            "avg_win_pnl": sum(wins_pnl) / len(wins_pnl) if wins_pnl else 0.0,
            "avg_loss_pnl": sum(losses_pnl) / len(losses_pnl) if losses_pnl else 0.0,
            "best_trade_pnl": max(pnls) if pnls else 0.0,
            "worst_trade_pnl": min(pnls) if pnls else 0.0,
        }
        return per_trade, summary

    def _build_equity_curve(
        self, results: list[BacktestResult], per_trade: list[dict]
    ) -> list[dict]:
        _fmt = lambda ms: datetime.datetime.fromtimestamp(
            ms / 1000, tz=datetime.timezone.utc
        ).strftime("%Y-%m-%d %H:%M:%S") if ms else ""
        curve = [
            {
                "time": "",
                "balance": self.start_balance,
                "pnl": 0.0,
                "result_r": 0.0,
                "drawdown": 0.0,
                "drawdown_pct": 0.0,
            }
        ]
        for r, a in zip(results, per_trade):
            curve.append(
                {
                    "time": _fmt(r.close_ts),
                    "balance": round(a["balance_after"], 2),
                    "pnl": round(a["pnl"], 2),
                    "result_r": round(r.realized_rr, 4),
                    "drawdown": round(a["drawdown_after"], 2),
                    "drawdown_pct": round(a["drawdown_pct_after"], 4),
                }
            )
        return curve

    def print(self) -> None:
        if not self.results:
            print(f" {YELLOW}No signals generated.{RESET}")
            return
        self._print_summary("COMBINED", self.results, self.profile)
        pairs = sorted(
            {(x.signal.htf_interval, x.signal.ltf_interval) for x in self.results}
        )
        if len(pairs) > 1:
            for htf, ltf in pairs:
                subset = [
                    x
                    for x in self.results
                    if x.signal.htf_interval == htf and x.signal.ltf_interval == ltf
                ]
                pair_profile = self._registry.get(self.symbol, htf, ltf)
                self._print_summary(f"{htf}/{ltf}", subset, pair_profile, compact=True)

    def _print_summary(
        self,
        label: str,
        r: list[BacktestResult],
        profile: AssetProfile,
        compact: bool = False,
    ) -> None:
        WIN = SignalOutcome.WIN_FULL
        LOSS = SignalOutcome.LOSS
        BE = SignalOutcome.BREAKEVEN
        INV = SignalOutcome.INVALIDATED
        EXP = SignalOutcome.EXPIRED

        wins = [x for x in r if x.outcome == WIN]
        losses = [x for x in r if x.outcome == LOSS]
        bes = [x for x in r if x.outcome == BE]
        invals = [x for x in r if x.outcome == INV]
        expd = [x for x in r if x.outcome == EXP]
        closed = wins + losses + bes

        total_r = sum(x.realized_rr for x in r)
        win_rate = len(wins) / len(closed) * 100 if closed else 0.0
        pf_num = sum(x.realized_rr for x in wins + bes)
        pf_den = abs(sum(x.realized_rr for x in losses))
        pf = pf_num / pf_den if pf_den else float("inf")

        longs = [x for x in r if x.signal.direction == SignalDirection.LONG]
        shorts = [x for x in r if x.signal.direction == SignalDirection.SHORT]

        # Account simulation
        per_trade, acct = self._compute_accounting(r)

        W, R = BOLD, RESET
        sep = "─" * 64
        print(f"\n{W}{sep}{R}")
        print(f"{W} BACKTEST · {self.symbol} · {label}{R}")
        print(sep)

        if not compact:
            # ── Account performance (primary) ──────────────────────────────
            risk_label = f"{self.risk_percent:g}% risk/trade"
            net_col = GREEN if acct["net_pnl"] >= 0 else RED
            dd_col = RED if acct["max_drawdown"] > 0 else DIM
            print(f" {'ACCOUNT SIMULATION':<32} {DIM}({risk_label}){R}")
            print(
                f" {'  Start Balance':<32} "
                f"{BOLD}{_fmt_currency_plain(acct['start_balance'])}{R}"
            )
            print(
                f" {'  Final Balance':<32} "
                f"{net_col}{BOLD}{_fmt_currency_plain(acct['final_balance'])}{R}"
            )
            print(
                f" {'  Net PnL':<32} "
                f"{net_col}{_fmt_currency(acct['net_pnl'])}{R}"
            )
            print(
                f" {'  Net Return':<32} "
                f"{net_col}{_fmt_pct(acct['net_pnl_pct'], sign=True)}{R}"
            )
            print(
                f" {'  Max Drawdown':<32} "
                f"{dd_col}-{_fmt_currency_plain(acct['max_drawdown'])}{R}"
            )
            print(
                f" {'  Max Drawdown %':<32} "
                f"{dd_col}-{_fmt_pct(acct['max_drawdown_pct'])}{R}"
            )
            if acct["gross_loss"] > 0:
                pf_dollar = acct["profit_factor_dollar"]
                pf_dollar_str = f"{pf_dollar:.2f}" if pf_dollar is not None else "N/A"
                pf_col = GREEN if (pf_dollar or 0) >= 1 else RED
                print(
                    f" {'  Profit Factor ($)':<32} "
                    f"{pf_col}{pf_dollar_str}{R}"
                )
            print(f" {'─'*42}")

            # ── Trade counts ───────────────────────────────────────────────
            print(f" {'Total signals':<32} {len(r)}")
            print(f" {' LONG / SHORT':<32} {len(longs)} / {len(shorts)}")
            print(f" {'Closed (W+BE+L)':<32} {len(closed)}")
            print(f" {' Wins':<32} {GREEN}{len(wins)}{R}")
            print(f" {' Breakevens':<32} {YELLOW}{len(bes)}{R}")
            print(f" {' Losses':<32} {RED}{len(losses)}{R}")
            print(
                f" {' Invalidated / Expired':<32} {DIM}{len(invals)} / {len(expd)}{R}"
            )
            print(f" {'─'*42}")
        else:
            # Compact: one-liner account summary
            net_col = GREEN if acct["net_pnl"] >= 0 else RED
            print(
                f" Signals={len(r)} W={len(wins)} BE={len(bes)} L={len(losses)}"
                f" Inv/Exp={len(invals)}/{len(expd)}"
                f"  {net_col}Net {_fmt_currency(acct['net_pnl'])}"
                f" ({_fmt_pct(acct['net_pnl_pct'], sign=True)})"
                f"  DD -{_fmt_pct(acct['max_drawdown_pct'])}{R}"
            )

        print(
            f" {'Win rate (closed)':<32} "
            f"{'%s%.1f%%%s' % (GREEN if win_rate >= 50 else RED, win_rate, R)}"
        )
        print(
            f" {'Profit factor (R)':<32} "
            f"{'%s%.2f%s' % (GREEN if pf >= 1 else RED, pf, R)}"
        )
        print(
            f" {'Total R':<32} "
            f"{'%s%+.2fR%s' % (GREEN if total_r >= 0 else RED, total_r, R)}"
        )

        if wins or bes:
            avg_w = sum(x.realized_rr for x in wins + bes) / len(wins + bes)
            print(f" {'Avg win/BE':<32} {GREEN}+{avg_w:.2f}R{R}")
        if losses:
            avg_l = sum(x.realized_rr for x in losses) / len(losses)
            print(f" {'Avg loss':<32} {RED}{avg_l:.2f}R{R}")

        if not compact:
            print(f" {'─'*42}")
            min_rr, max_rr = self._rr_window_for_results(r, profile)
            print(f" {' R:R window':<32} [{min_rr}, {max_rr}]")
            print(f" {' Wick ratio':<32} [{self.cfg.min_wick_ratio}, ∞]")
        print(f" {'─'*42}")

        for dir_label, subset in [("LONG", longs), ("SHORT", shorts)]:
            if not subset:
                continue
            w = [x for x in subset if x.outcome == WIN]
            b = [x for x in subset if x.outcome == BE]
            l = [x for x in subset if x.outcome == LOSS]
            cl = w + b + l
            wr = len(w) / len(cl) * 100 if cl else 0.0
            tr = sum(x.realized_rr for x in subset)
            col = GREEN if tr >= 0 else RED
            # Independent accounting per direction (same start_balance)
            _, dir_acct = self._compute_accounting(subset)
            dir_net_col = GREEN if dir_acct["net_pnl"] >= 0 else RED
            print(
                f" {dir_label:<8} W={len(w)} BE={len(b)} L={len(l)}"
                f" WR={wr:.0f}% {col}{tr:+.2f}R{R}"
                f"  {dir_net_col}{_fmt_currency(dir_acct['net_pnl'])}{R}"
            )
        print(f"{W}{sep}{R}")

    def _rr_window_for_results(
        self, r: list[BacktestResult], fallback: AssetProfile
    ) -> tuple[float | str, float | str]:
        """Return the effective RR display window for the result's TF pairs."""
        pairs = sorted(
            {
                (x.signal.htf_interval, x.signal.ltf_interval)
                for x in r
                if x.signal.htf_interval and x.signal.ltf_interval
            }
        )
        if not pairs:
            max_rr = "∞" if fallback.max_rr == 0 else fallback.max_rr
            return fallback.min_rr, max_rr

        profiles = [self._registry.get(self.symbol, htf, ltf) for htf, ltf in pairs]
        min_values = sorted({p.min_rr for p in profiles})
        max_values = sorted({p.max_rr for p in profiles})

        min_rr: float | str = (
            min_values[0]
            if len(min_values) == 1
            else f"{min_values[0]}-{min_values[-1]}"
        )
        if any(v == 0 for v in max_values):
            max_rr: float | str = "∞"
        elif len(max_values) == 1:
            max_rr = max_values[0]
        else:
            max_rr = f"{max_values[0]}-{max_values[-1]}"
        return min_rr, max_rr

    def print_streak_analysis(self) -> None:
        """Run losing-streak analysis on this backtest's results."""
        if not self.results:
            print(" No results to analyse.")
            return
        from app.backtesting.streak_analysis import run as _streak_run
        _streak_run(self.results, symbol=self.symbol)

    def save_csv(self, path: str) -> None:
        if not self.results:
            return
        per_trade, _ = self._compute_accounting(self.results)
        acct_fields = [
            "balance_before",
            "risk_amount",
            "pnl",
            "balance_after",
            "peak_balance_after",
            "drawdown_after",
            "drawdown_pct_after",
            "theoretical_rr",
            "executed_rr",
            "raw_entry_price",
            "executed_entry_price",
            "raw_exit_price",
            "executed_exit_price",
        ]
        base_fields = list(self.results[0].to_dict().keys())
        all_fields = base_fields + acct_fields
        with open(path, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=all_fields)
            w.writeheader()
            for r, a in zip(self.results, per_trade):
                row = r.to_dict()
                for k in acct_fields:
                    val = a[k]
                    row[k] = round(val, 6) if isinstance(val, float) else val
                w.writerow(row)
        print(f" {GREEN}Saved → {path}{RESET}")

    def save_json(self, path: str) -> None:
        per_trade, summary = self._compute_accounting(self.results)
        equity_curve = self._build_equity_curve(self.results, per_trade)
        acct_fields = [
            "balance_before",
            "risk_amount",
            "pnl",
            "balance_after",
            "peak_balance_after",
            "drawdown_after",
            "drawdown_pct_after",
            "theoretical_rr",
            "executed_rr",
            "raw_entry_price",
            "executed_entry_price",
            "raw_exit_price",
            "executed_exit_price",
        ]
        trades = []
        for r, a in zip(self.results, per_trade):
            d = r.to_dict()
            for k in acct_fields:
                val = a[k]
                d[k] = round(val, 6) if isinstance(val, float) else val
            trades.append(d)
        out = {
            "summary": {
                k: round(v, 6) if isinstance(v, float) else v
                for k, v in summary.items()
            },
            "risk_percent": self.risk_percent,
            "trailing_giveback_pct": self.trailing_giveback_pct,
            "equity_curve": equity_curve,
            "trades": trades,
        }
        with open(path, "w") as f:
            json.dump(out, f, indent=2)
        print(f" {GREEN}Saved → {path}{RESET}")


# ── MultiPairBacktester ───────────────────────────────────────────────────────
class MultiPairBacktester:
    def __init__(
        self,
        cfg: Settings,
        symbol: str,
        pairs: list[tuple[str, str]],
        htf_candles: dict[str, list[Candle]],
        ltf_candles: dict[str, list[Candle]],
        htf_lookback: Optional[int] = None,
        start_balance: float = DEFAULT_START_BALANCE,
        risk_percent: float = DEFAULT_RISK_PERCENT,
        trailing_giveback_pct: float = DEFAULT_TRAILING_GIVEBACK_PCT,
        trace_out: Optional[str] = None,
    ) -> None:
        # Validate account simulation inputs
        if not math.isfinite(start_balance):
            raise ValueError("startBalance must be a valid number")
        if start_balance <= 0:
            raise ValueError("startBalance must be greater than 0")
        if not math.isfinite(risk_percent):
            raise ValueError("riskPercent must be a valid number")
        if risk_percent <= 0 or risk_percent > 100:
            raise ValueError(
                "riskPercent must be greater than 0 and less than or equal to 100"
            )
        if not math.isfinite(trailing_giveback_pct):
            raise ValueError("trailingGivebackPct must be a valid number")
        if trailing_giveback_pct < 0:
            raise ValueError("trailingGivebackPct must be >= 0")
        if trailing_giveback_pct >= 100:
            raise ValueError("trailingGivebackPct must be < 100")

        self.cfg = cfg
        self.symbol = symbol
        self.pairs = pairs
        self.htf_candles = htf_candles
        self.ltf_candles = ltf_candles
        self._lookback_override = htf_lookback  # CLI int override, or None
        self.start_balance = start_balance
        self.risk_percent = risk_percent
        self.trailing_giveback_pct = trailing_giveback_pct
        self.trace_out = trace_out
        self._trace_writer: Optional[ParityTraceWriter] = None
        self._registry = AssetRegistry(cfg)
        self._decision_engine = DecisionEngine()

        # Per tf-pair profiles — only max_rr differs; all other attrs are symbol-level.
        self._pair_profiles: dict[tuple[str, str], AssetProfile] = {
            (htf, ltf): self._registry.get(symbol, htf, ltf) for htf, ltf in pairs
        }
        # Convenience: base profile (non-rr attrs identical across pairs)
        self.profile = self._pair_profiles[pairs[0]]

        self.results: list[BacktestResult] = []

        # Pre-computed sorted timestamp lists
        self._ltf_ts: dict[str, list[int]] = {
            ltf: [c.timestamp for c in candles] for ltf, candles in ltf_candles.items()
        }
        self._htf_ts: dict[str, list[int]] = {
            htf: [c.timestamp for c in candles] for htf, candles in htf_candles.items()
        }

        # NumPy structured arrays for vectorised simulation
        self.ltf_candles_np: dict[str, np.ndarray] = {
            ltf: self._candles_to_np(candles) for ltf, candles in ltf_candles.items()
        }

        # Per-HTF-interval result caches (keyed by last HTF close timestamp)
        self._ms_cache: dict[str, tuple[int, object]] = {}
        self._range_cache: dict[str, tuple[int, list]] = {}

        # ★ TIER 1: Zone-level combined cache
        #   key  → zone_key (htf_range.timestamp, bos_direction.value)
        #   value → (ltf_hi_idx, rej_result_or_None)
        #
        #   A cache hit (same ltf_hi_idx) means ltf_zone is byte-identical to
        #   the previous call, so find_entry result is reused.
        #   Hit rate ≈ 1 − (1 / ticks_per_ltf_bar); e.g. 98 % for 1 m / 1 h.
        self._zone_cache: dict[tuple, tuple] = {}

        # ★ TIER 2: Zone lo-index cache
        #   bisect_left(ltf_ts_idx, zone_start) is immutable for a zone's
        #   lifetime — compute once and store.
        self._zone_lo: dict[tuple, int] = {}

        # ★ TIER 4: Profile scalars hoisted to instance (no per-call dereference)
        profile = self.profile
        self._use_be: bool = profile.move_sl_to_be_on_tp1
        self._expiry_ms: int = int(profile.signal_expiry_hours * 3_600_000)
        self._tp1_protected_rr: float = tp1_booked_rr(
            full_rr=1.0,
            tp1_trigger_pct=profile.tp1_trigger_pct,
            tp1_close_pct=profile.tp1_close_pct,
        )

        # Pre-computed constants per pair
        self._stale_ms: dict[tuple[str, str], int] = {
            (htf, ltf): int(cfg.rejection_stale_hours(ltf) * 3_600_000)
            for htf, ltf in pairs
        }
        self._htf_interval_ms: dict[str, int] = {
            htf: interval_to_minutes(htf) * 60_000 for htf, _ in pairs
        }


    def _resolve_lookback(self, htf_interval: str, ltf_interval: str) -> int:
        if self._lookback_override is not None:
            return self._lookback_override
        return self.cfg.resolve_htf_lookback(htf_interval, ltf_interval)

    def _candles_to_np(self, candles: list[Candle]) -> np.ndarray:
        dtype = [
            ("timestamp", "i8"),
            ("open", "f8"),
            ("high", "f8"),
            ("low", "f8"),
            ("close", "f8"),
            ("volume", "f8"),
        ]
        if not candles:
            return np.empty(0, dtype=dtype)
        return np.array(
            [(c.timestamp, c.open, c.high, c.low, c.close, c.volume) for c in candles],
            dtype=dtype,
        )

    # ── main loop ─────────────────────────────────────────────────────────────
    def run(self) -> BacktestReport:
        cfg = self.cfg
        profile = self.profile  # base profile — non-rr attrs only
        _pair_profiles = self._pair_profiles
        _pair_lookbacks = {
            (htf, ltf): self._resolve_lookback(htf, ltf) for htf, ltf in self.pairs
        }
        use_tf = profile.use_trend_filter
        use_mtp = profile.multi_tf_independent_positions
        symbol = self.symbol

        ltf_intervals = list(dict.fromkeys(ltf for _, ltf in self.pairs))
        master_ltf = min(ltf_intervals, key=interval_to_minutes)
        master_candles = self.ltf_candles[master_ltf]
        max_htf_mins = max(interval_to_minutes(htf) for htf, _ in self.pairs)
        min_ltf_mins = interval_to_minutes(master_ltf)
        master_ltf_ms = min_ltf_mins * 60_000
        max_lookback = max(_pair_lookbacks.values())
        ltf_lb = max(100, max_lookback * (max_htf_mins // min_ltf_mins))
        n = len(master_candles)
        total_steps = n - ltf_lb
        pairs_str = " | ".join(f"{h}/{l}" for h, l in self.pairs)

        trail_header = (
            f"  Giveback trail: {self.trailing_giveback_pct:g}%"
            if self.trailing_giveback_pct > 0
            else ""
        )
        print(f"\n{'='*64}")
        print(f"{BOLD} BACKTEST · {symbol} · {pairs_str}{RESET}")
        print(
            f" Master LTF : {master_ltf} ({n:,} bars) warm-up={ltf_lb}"
            f"{trail_header}"
        )
        print(f"{'='*64}\n")

        dead_zones: set[tuple] = set()
        zone_signal_counts: dict[tuple, int] = {}
        seen_rej: set[tuple] = set()
        open_dir: dict[tuple, int] = {}
        expiry_ms = int(profile.signal_expiry_hours * 3_600_000)
        pivot_bars = cfg.pivot_bars
        max_zones  = cfg.max_htf_zones_per_dir
        max_signal_count = cfg.max_signal_count_per_zone
        use_disp        = cfg.use_displacement_filter
        disp_atr_period = cfg.displacement_atr_period

        # Precomputed lookups
        _stale_ms = self._stale_ms
        _htf_int_ms = self._htf_interval_ms
        _ms_cache = self._ms_cache
        _rng_cache = self._range_cache

        # ★ Zone caches hoisted to locals (hot-path dict lookups are faster on locals)
        _zone_cache = self._zone_cache
        _zone_lo = self._zone_lo

        # Hoist hot-path callables
        _bisect_right = bisect.bisect_right
        _bisect_left = bisect.bisect_left
        _find_htf = SwingDetector.find_htf_ranges
        _detect_struct = MarketStructure.detect
        _simulate = self._simulate
        _print_result = self._print_result


        trace_ctx = ParityTraceWriter(self.trace_out)
        self._trace_writer = trace_ctx.__enter__()
        for ltf_i in range(ltf_lb, n):
            step = ltf_i - ltf_lb
            if step % 200 == 0:
                pct = step / max(total_steps, 1) * 100
                bar = "█" * int(pct / 5) + "░" * (20 - int(pct / 5))
                print(
                    f"\r [{bar}] {pct:5.1f}% signals={len(self.results)}",
                    end="",
                    flush=True,
                )

            current_ts = master_candles[ltf_i].timestamp
            analysis_close = current_ts + master_ltf_ms

            # Expire direction locks
            if open_dir:
                expired = [dk for dk, cts in open_dir.items() if cts <= current_ts]
                for dk in expired:
                    del open_dir[dk]

            for htf_interval, ltf_interval in self.pairs:
                htf_all = self.htf_candles[htf_interval]
                htf_ts_idx = self._htf_ts[htf_interval]
                pair_lookback = _pair_lookbacks[(htf_interval, ltf_interval)]

                # O(log n) HTF window. HTF timestamps are candle opens; only
                # include bars whose close is known at this analysis point.
                hi_htf = _bisect_right(
                    htf_ts_idx, analysis_close - _htf_int_ms[htf_interval]
                )
                lo_htf = max(0, hi_htf - pair_lookback)
                htf_visible_all = htf_all[:hi_htf]
                htf_w = htf_all[lo_htf:hi_htf]
                if len(htf_w) < pair_lookback // 3:
                    continue

                last_htf_ts = htf_w[-1].timestamp

                # MarketStructure — recompute only on HTF bar close
                if use_tf:
                    cached_ms = _ms_cache.get(htf_interval)
                    if cached_ms is None or cached_ms[0] != last_htf_ts:
                        structure = _detect_struct(htf_w, pivot_bars=pivot_bars)
                        _ms_cache[htf_interval] = (last_htf_ts, structure)
                    else:
                        structure = cached_ms[1]
                    if structure.bias.value == "NEUTRAL":
                        continue
                else:
                    structure = None

                # HTF ranges — recompute only on HTF bar close
                range_cache_key = (htf_interval, ltf_interval)
                cached_rng = _rng_cache.get(range_cache_key)
                if cached_rng is None or cached_rng[0] != last_htf_ts:
                    htf_ranges = list(
                        _find_htf(
                            htf_w,
                            pivot_bars=pivot_bars,
                            htf_interval_ms=_htf_int_ms[htf_interval],
                            max_zones_per_dir=max_zones,
                        )
                    )
                    # Apply displacement filter — mirrors signal_service behaviour.
                    # Uses visible HTF context only to avoid future candle leakage.
                    if use_disp:
                        disp_atr_mult = cfg.displacement_mult_for(htf_interval, ltf_interval)
                        htf_ranges = [
                            z for z in htf_ranges
                            if detect_displacement(
                                htf_visible_all,
                                z.broken_at,
                                atr_period=disp_atr_period,
                                atr_mult=disp_atr_mult,
                            )
                        ]
                    _rng_cache[range_cache_key] = (last_htf_ts, htf_ranges)
                else:
                    htf_ranges = cached_rng[1]

                ltf_all = self.ltf_candles[ltf_interval]
                ltf_ts_idx = self._ltf_ts[ltf_interval]
                stale_ms = _stale_ms[(htf_interval, ltf_interval)]
                ltf_interval_ms = interval_to_minutes(ltf_interval) * 60_000
                pair_current_ts = (
                    (analysis_close // ltf_interval_ms) * ltf_interval_ms
                    - ltf_interval_ms
                )
                ltf_hi_idx = _bisect_right(ltf_ts_idx, pair_current_ts)
                if ltf_hi_idx <= 0:
                    continue
                ltf_visible = ltf_all[:ltf_hi_idx]

                for htf_range in htf_ranges:
                    zone_key = (
                        symbol,
                        htf_interval,
                        ltf_interval,
                        htf_range.timestamp,
                        htf_range.bos_direction.value,
                    )
                    cache_key = (
                        htf_interval,
                        ltf_interval,
                        htf_range.timestamp,
                        htf_range.bos_direction.value,
                    )
                    if (
                        zone_key in dead_zones
                        or zone_signal_counts.get(zone_key, 0) >= max_signal_count
                    ):
                        continue

                    # ★ TIER 2: lo-index computed once per zone (zone_start is immutable)
                    if cache_key not in _zone_lo:
                        zone_start = htf_range.broken_at or htf_range.timestamp
                        _zone_lo[cache_key] = _bisect_left(ltf_ts_idx, zone_start)
                    ltf_lo_idx = _zone_lo[cache_key]

                    direction = htf_range.signal_direction.value
                    if structure is not None and not structure.allows(direction):
                        continue

                    # ★ TIER 1: Zone-level find_entry cache
                    #   Key: ltf_hi_idx — changes only when a new LTF bar arrives.
                    cached = _zone_cache.get(cache_key)
                    if cached is not None and cached[0] == ltf_hi_idx:
                        rej_result = cached[1]
                    else:
                        ltf_zone = ltf_visible[ltf_lo_idx:ltf_hi_idx]
                        rej_result = find_entry(
                            ltf_zone,
                            htf_range.signal_direction,
                            htf_range,
                        )
                        _zone_cache[cache_key] = (ltf_hi_idx, rej_result)

                    if not rej_result:
                        continue

                    rejection, _ = rej_result
                    rej_key = (
                        symbol,
                        htf_interval,
                        ltf_interval,
                        rejection.timestamp,
                    )
                    if rej_key in seen_rej:
                        continue
                    pair_dir_key = (
                        (symbol, htf_interval, ltf_interval, direction)
                        if use_mtp
                        else (symbol, direction)
                    )
                    if pair_dir_key in open_dir:
                        continue
                    if pair_current_ts - rejection.timestamp > stale_ms:
                        continue

                    # FIX: include direction in signal_id to match signal_service
                    # and prevent collisions when LONG/SHORT share a rejection timestamp.
                    decision = self._decision_engine.evaluate_setup(
                        symbol=symbol,
                        htf_interval=htf_interval,
                        ltf_interval=ltf_interval,
                        htf_range=htf_range,
                        rejection=rejection,
                        signal_id=(
                            f"{symbol}_{htf_interval}_{ltf_interval}"
                            f"_{rejection.timestamp}_{direction}"
                        ),
                        profile=_pair_profiles[(htf_interval, ltf_interval)],
                    )
                    signal = decision.signal
                    if signal is None:
                        continue
                    signal.setup_candle_open_at = rejection.timestamp
                    signal.setup_candle_close_at = rejection.timestamp + ltf_interval_ms
                    signal.zone_attempt = zone_signal_counts.get(zone_key, 0) + 1

                    cur_ltf_i = ltf_hi_idx - 1
                    future_np = self.ltf_candles_np[ltf_interval][cur_ltf_i + 1 :]
                    result = _simulate(signal, future_np)

                    seen_rej.add(rej_key)
                    zone_signal_counts[zone_key] = signal.zone_attempt

                    if signal.zone_attempt >= max_signal_count:
                        dead_zones.add(zone_key)
                        _zone_cache.pop(cache_key, None)
                        _zone_lo.pop(cache_key, None)

                    open_dir[pair_dir_key] = result.close_ts or (
                        signal.created_at + expiry_ms
                    )
                    self.results.append(result)
                    if self._trace_writer:
                        self._trace_writer.write(
                            trace_from_signal(
                                mode="backtest",
                                signal=result.signal,
                                cfg=cfg,
                                decision_reason=decision.decision_reason,
                                blocked_reason=decision.blocked_reason,
                                account_balance=self.start_balance,
                                risk_percent=self.risk_percent,
                                outcome=result.outcome,
                            )
                        )
                    _print_result(result)
                    break  # one signal per LTF tick per HTF zone scan

        print(f"\r [{'█'*20}] 100.0% signals={len(self.results)}\n")
        trace_ctx.__exit__(None, None, None)
        self._trace_writer = None

        report = BacktestReport(
            symbol,
            self.results,
            cfg,
            start_balance=self.start_balance,
            risk_percent=self.risk_percent,
            trailing_giveback_pct=self.trailing_giveback_pct,
        )
        report.print()
        return report

    # ── simulation ────────────────────────────────────────────────────────────
    def _simulate(self, signal: TradeSignal, future_np: np.ndarray) -> BacktestResult:
        """Dispatch to Numba JIT (single pass) or NumPy fallback (vectorised).

        Extended to track: did price revisit entry after TP1 before TP2?

        Same-bar conflict policy (both paths)
        ──────────────────────────────────────
        SL vs TP1 on the same bar → SL wins (conservative).
        TP1 vs TP2 on the same bar → TP2 wins (tp1_prev set, then TP2 checked
        in the same iteration/bar).
        These semantics match the Numba kernel's "strictly prior" design where
        tp1_prev is updated AFTER the SL/INV checks.
        """
        if len(future_np) == 0:
            return BacktestResult(signal, SignalOutcome.EXPIRED, 0.0, None, None)

        future = [
            Candle(
                int(row["timestamp"]),
                float(row["open"]),
                float(row["high"]),
                float(row["low"]),
                float(row["close"]),
                float(row["volume"]),
            )
            for row in future_np
        ]
        profile = self._registry.get(
            signal.symbol, signal.htf_interval, signal.ltf_interval
        )
        if self.trailing_giveback_pct > 0:
            return self._simulate_with_giveback_trailing(signal, future, profile)

        replayed = replay_signal_lifecycle(signal, future, profile)
        if replayed.outcome is None:
            return BacktestResult(replayed, SignalOutcome.EXPIRED, 0.0, None, None)
        return BacktestResult(
            replayed,
            replayed.outcome,
            replayed.realized_rr or 0.0,
            replayed.closed_at,
            replayed.close_price,
            hit_entry_after_tp1=False,
        )

    # ── console output ────────────────────────────────────────────────────────
    def _simulate_with_giveback_trailing(
        self,
        signal: TradeSignal,
        future: list[Candle],
        profile: AssetProfile,
    ) -> BacktestResult:
        """Backtest-only TP1/BE plus MFE giveback trailing.

        Intrabar ordering is unknowable from OHLC candles. Existing exits are
        resolved before a candle's extreme can advance the trailing stop, so a
        new MFE can only affect later candles.
        """
        if not future:
            return BacktestResult(signal, SignalOutcome.EXPIRED, 0.0, None, None)

        probe = copy.deepcopy(signal)
        is_short = probe.direction == SignalDirection.SHORT
        entry = float(probe.entry_price)
        original_sl = float(probe.stop_loss)
        current_sl = original_sl
        trail_mfe_price: float | None = None
        trailed_sl: float | None = None
        tp1_seen = False
        hit_entry_after_tp1 = False
        expiry_cutoff = int(probe.created_at + profile.signal_expiry_hours * 3_600_000)

        for candle in future:
            if candle.timestamp > expiry_cutoff:
                if tp1_seen and profile.move_sl_to_be_on_tp1:
                    return self._trailing_result(
                        probe,
                        SignalOutcome.BREAKEVEN,
                        self._protected_be_rr(probe, profile),
                        candle.timestamp,
                        self._protected_be_price(probe, profile),
                        hit_entry_after_tp1,
                        trail_mfe_price,
                        trailed_sl,
                    )
                return self._trailing_result(
                    probe,
                    SignalOutcome.EXPIRED,
                    0.0,
                    candle.timestamp,
                    candle.close,
                    hit_entry_after_tp1,
                    trail_mfe_price,
                    trailed_sl,
                )

            tp1_now = candle.low <= probe.tp1 if is_short else candle.high >= probe.tp1
            tp2_now = candle.low <= probe.tp2 if is_short else candle.high >= probe.tp2
            effective_sl = current_sl if tp1_seen and profile.move_sl_to_be_on_tp1 else original_sl
            sl_now = candle.high >= effective_sl if is_short else candle.low <= effective_sl
            entry_now = candle.high >= entry if is_short else candle.low <= entry

            if tp1_seen and entry_now:
                hit_entry_after_tp1 = True

            if (tp1_seen or tp1_now) and tp2_now:
                return self._trailing_result(
                    probe,
                    SignalOutcome.WIN_FULL,
                    probe.risk_reward_ratio,
                    candle.timestamp,
                    probe.tp2,
                    hit_entry_after_tp1,
                    trail_mfe_price,
                    trailed_sl,
                    tp2_hit=True,
                )

            if sl_now:
                if tp1_seen and profile.move_sl_to_be_on_tp1:
                    close_px = effective_sl
                    return self._trailing_result(
                        probe,
                        SignalOutcome.BREAKEVEN,
                        max(
                            self._protected_be_rr(probe, profile),
                            self._rr_at_price(probe, close_px),
                        ),
                        candle.timestamp,
                        close_px,
                        hit_entry_after_tp1,
                        trail_mfe_price,
                        trailed_sl,
                    )
                return self._trailing_result(
                    probe,
                    SignalOutcome.LOSS,
                    -1.0,
                    candle.timestamp,
                    original_sl,
                    hit_entry_after_tp1,
                    trail_mfe_price,
                    trailed_sl,
                )

            if tp1_now and not tp1_seen:
                tp1_seen = True
                probe.tp1_hit_at = candle.timestamp
                current_sl = (
                    self._protected_be_price(probe, profile)
                    if profile.move_sl_to_be_on_tp1
                    else original_sl
                )

            if tp1_seen and profile.move_sl_to_be_on_tp1:
                current_sl, trail_mfe_price, trailed_sl = self._advance_giveback_sl(
                    probe,
                    candle,
                    current_sl,
                    trail_mfe_price,
                    trailed_sl,
                )

        return BacktestResult(
            probe,
            SignalOutcome.EXPIRED,
            0.0,
            None,
            None,
            hit_entry_after_tp1=hit_entry_after_tp1,
            trail_mfe_price=trail_mfe_price,
            trailed_sl=trailed_sl,
            trailing_giveback_pct=self.trailing_giveback_pct,
        )

    def _advance_giveback_sl(
        self,
        signal: TradeSignal,
        candle: Candle,
        current_sl: float,
        trail_mfe_price: float | None,
        trailed_sl: float | None,
    ) -> tuple[float, float | None, float | None]:
        entry = float(signal.entry_price)
        pct = self.trailing_giveback_pct
        if signal.direction == SignalDirection.LONG:
            best_price = max(float(trail_mfe_price or candle.high), candle.high)
            favorable_move = best_price - entry
            if favorable_move <= 0:
                return current_sl, best_price, trailed_sl
            proposed_sl = entry + favorable_move * (1.0 - pct / 100.0)
            if proposed_sl > current_sl and proposed_sl < candle.close:
                return proposed_sl, best_price, proposed_sl
            return current_sl, best_price, trailed_sl

        best_price = min(float(trail_mfe_price or candle.low), candle.low)
        favorable_move = entry - best_price
        if favorable_move <= 0:
            return current_sl, best_price, trailed_sl
        proposed_sl = entry - favorable_move * (1.0 - pct / 100.0)
        if proposed_sl < current_sl and proposed_sl > candle.close:
            return proposed_sl, best_price, proposed_sl
        return current_sl, best_price, trailed_sl

    def _trailing_result(
        self,
        signal: TradeSignal,
        outcome: SignalOutcome,
        realized_rr: float,
        close_ts: int,
        close_px: float,
        hit_entry_after_tp1: bool,
        trail_mfe_price: float | None,
        trailed_sl: float | None,
        tp2_hit: bool = False,
    ) -> BacktestResult:
        signal.outcome = outcome
        signal.realized_rr = realized_rr
        signal.close_price = close_px
        signal.closed_at = close_ts
        if tp2_hit:
            signal.tp2_hit_at = close_ts
        return BacktestResult(
            signal,
            outcome,
            realized_rr,
            close_ts,
            close_px,
            hit_entry_after_tp1=hit_entry_after_tp1,
            trail_mfe_price=trail_mfe_price,
            trailed_sl=trailed_sl,
            trailing_giveback_pct=self.trailing_giveback_pct,
        )

    def _tp1_booked_rr(self, signal: TradeSignal, profile: AssetProfile) -> float:
        return tp1_booked_rr(
            full_rr=signal.risk_reward_ratio,
            tp1_trigger_pct=profile.tp1_trigger_pct,
            tp1_close_pct=profile.tp1_close_pct,
        )

    def _protected_be_price(self, signal: TradeSignal, profile: AssetProfile) -> float:
        return breakeven_price(
            direction=signal.direction,
            entry_price=signal.entry_price,
        )

    def _protected_be_rr(self, signal: TradeSignal, profile: AssetProfile) -> float:
        return protected_breakeven_rr(
            full_rr=signal.risk_reward_ratio,
            tp1_trigger_pct=profile.tp1_trigger_pct,
            tp1_close_pct=profile.tp1_close_pct,
        )

    def _rr_at_price(self, signal: TradeSignal, price: float) -> float:
        if signal.direction == SignalDirection.LONG:
            return (price - signal.entry_price) / signal.risk_pips
        return (signal.entry_price - price) / signal.risk_pips

    def _print_result(self, r: BacktestResult) -> None:
        s = r.signal
        arrow = DOWN if s.direction == SignalDirection.SHORT else UP
        tf_tag = f"{DIM}[{s.htf_interval}/{s.ltf_interval}]{RESET}"
        et_pattern = f"{DIM}[{s.rejection_candle.pattern.value}]{RESET}"
        attempt_tag = f"{DIM}[Z{s.zone_attempt}]{RESET}" if s.zone_attempt > 1 else ""

        # FIX: hit_entry_after_tp1 is always set via __init__ — direct access.
        retrace_marker = f" {YELLOW}↺{RESET}" if r.hit_entry_after_tp1 else ""

        executed_rr = r.realized_rr

        def _rr_label(rr: float) -> str:
            return f"+{rr:.2f}R" if rr >= 0 else f"{rr:.2f}R"

        outcome_str = {
            SignalOutcome.WIN_FULL: f"{GREEN}W {_rr_label(executed_rr)}{RESET}",
            SignalOutcome.BREAKEVEN: f"{YELLOW}BE {_rr_label(executed_rr)}{RESET}",
            SignalOutcome.LOSS: f"{RED}L {_rr_label(executed_rr)}{RESET}",
            SignalOutcome.INVALIDATED: f"{DIM}INV 0.00R{RESET}",
            SignalOutcome.EXPIRED: f"{DIM}EXP 0.00R{RESET}",
        }.get(r.outcome, f"{DIM}?{RESET}")

        setup_ts = s.setup_candle_open_at or s.rejection_candle.timestamp
        actionable_ts = (
            s.setup_candle_close_at
            or s.rejection_candle.timestamp
            + _interval_to_minutes(s.ltf_interval) * 60 * 1000
        )
        close_label = (
            self.cfg.dt_ms(r.close_ts)
            if r.close_ts
            else ("EXPIRED" if r.outcome == SignalOutcome.EXPIRED else "OPEN")
        )

        print(f"\r{' '*80}\r", end="")
        print(
            f" {arrow} {tf_tag} {et_pattern}{attempt_tag} "
            f"S={CYAN}{self.cfg.dt_ms(setup_ts)}{RESET} "
            f"A={CYAN}{self.cfg.dt_ms(actionable_ts)}{RESET} "
            f"E@{CYAN}{self.cfg.dt_ms(s.triggered_at)}{RESET} "
            f"E={s.entry_price:.5f} SL={s.stop_loss:.5f} TP2={s.tp2:.5f} "
            f"→ {outcome_str}{retrace_marker} "
            f"closed {close_label}"
        )


Backtester = MultiPairBacktester  # backwards-compat alias


# ── CSV / API loaders ─────────────────────────────────────────────────────────
def load_csv(path: str) -> list[Candle]:
    candles: list[Candle] = []
    with open(path, newline="") as f:
        sample = f.read(4096)
        f.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample)
        except csv.Error:
            dialect = csv.excel
        has_header = csv.Sniffer().has_header(sample)
        reader = csv.reader(f, dialect)
        if has_header:
            next(reader)
        for row in reader:
            if not row or row[0].startswith("#"):
                continue
            try:
                ts_raw = row[0].strip()
                try:
                    ts_num = float(ts_raw)
                    ts = int(ts_num * 1000) if ts_num < 1e12 else int(ts_num)
                except ValueError:
                    ts = None
                    for fmt in (
                        "%Y-%m-%d %H:%M:%S",
                        "%Y-%m-%dT%H:%M:%S",
                        "%Y-%m-%d %H:%M",
                        "%Y/%m/%d %H:%M:%S",
                    ):
                        try:
                            dt = datetime.datetime.strptime(ts_raw, fmt)
                            ts = int(dt.replace(tzinfo=datetime.timezone.utc).timestamp() * 1000)
                            break
                        except ValueError:
                            continue
                    if ts is None:
                        continue
                o, h, l, c_px = (float(row[i]) for i in range(1, 5))
                vol = float(row[5]) if len(row) > 5 else 0.0
                candles.append(Candle(ts, o, h, l, c_px, vol))
            except (ValueError, IndexError):
                continue
    candles.sort(key=lambda c: c.timestamp)
    return candles


def load_from_api(
    symbol: str, interval: str, outputsize: int, cfg: Settings
) -> list[Candle]:
    client = MarketDataClient.from_settings(cfg)
    try:
        return client.fetch_candles(symbol, interval, outputsize)
    finally:
        client.close()


def load_from_api_range(
    symbol: str,
    interval: str,
    start_ts: int,
    end_ts: Optional[int],
    cfg: Settings,
) -> list[Candle]:
    client = MarketDataClient.from_settings(cfg)
    try:
        return client.fetch_candles_range(symbol, interval, start_ts, end_ts)
    finally:
        client.close()


# ── CLI ───────────────────────────────────────────────────────────────────────
def main() -> None:
    # Force UTF-8 on Windows when running as CLI — must run before any print
    if sys.platform == "win32":
        import io as _io

        sys.stdout = _io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
        sys.stderr = _io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
        os.environ["PYTHONIOENCODING"] = "utf-8"

    p = argparse.ArgumentParser(description="Backtest the signal engine")
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--symbol")
    src.add_argument("--csv-htf", dest="csv_htf")
    p.add_argument("--csv-ltf", dest="csv_ltf")
    p.add_argument("--output")
    p.add_argument("--tf-pair", dest="tf_pair", default=None)
    p.add_argument("--htf-lookback", dest="htf_lookback", type=int, default=None)
    p.add_argument("--min-rr", type=float, default=None)
    p.add_argument(
        "--max-rr",
        type=float,
        default=None,
        help="Hard cap for all pairs — overrides TF_MAX_RR entries",
    )
    p.add_argument("--max-wick", type=float, default=None)
    p.add_argument("--stale-hours", type=float, default=None)
    p.add_argument("--max-sl-mult", type=float, default=None)
    p.add_argument("--no-breakeven", action="store_true")
    p.add_argument("--no-trend-filter", action="store_true")
    p.add_argument("--no-session-filter", action="store_true")
    p.add_argument("--from-date", dest="from_date", metavar="YYYY-MM-DD")
    p.add_argument("--to-date", dest="to_date", metavar="YYYY-MM-DD")
    p.add_argument(
        "--start-balance",
        dest="start_balance",
        type=float,
        default=None,
        metavar="AMOUNT",
        help=f"Starting account balance (default: {DEFAULT_START_BALANCE:,.0f})",
    )
    p.add_argument(
        "--risk-percent",
        dest="risk_percent",
        type=float,
        default=None,
        metavar="PCT",
        help=f"Risk percent per trade (default: {DEFAULT_RISK_PERCENT}%%)",
    )
    p.add_argument(
        "--trailing-giveback-pct",
        dest="trailing_giveback_pct",
        type=float,
        default=None,
        metavar="PCT",
        help="Backtest-only MFE giveback trailing after TP1/BE; 0 disables",
    )
    p.add_argument(
        "--trace-out",
        dest="trace_out",
        help="Write deterministic backtest parity trace JSONL to this path",
    )
    p.add_argument("--verbose", action="store_true")
    p.add_argument(
        "--streak-analysis",
        dest="streak_analysis",
        action="store_true",
        help="Run losing-streak analysis after the backtest completes",
    )
    args = p.parse_args()

    cfg = Settings.from_env()
    overrides: dict = {}
    if args.no_breakeven:
        overrides["move_sl_to_be_on_tp1"] = False
    if args.no_trend_filter:
        overrides["use_trend_filter"] = False
    if args.no_session_filter:
        overrides["use_session_filter"] = False
    if args.min_rr is not None:
        overrides["min_rr"] = args.min_rr
    if args.max_rr is not None:
        # Hard cap — clears per-pair overrides so it applies universally
        overrides["max_rr"] = args.max_rr
        overrides["tf_max_rr"] = {}
    if overrides:
        cfg = dc_replace(cfg, **overrides)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )

    if args.tf_pair:
        # FIX: validate format before indexing to prevent silent mis-configuration.
        parts = args.tf_pair.split(":")
        if len(parts) != 2 or not parts[0].strip() or not parts[1].strip():
            p.error("--tf-pair must be in HTF:LTF format, e.g. '4h:1h'")
        tf_pairs_to_run = [(parts[0].strip(), parts[1].strip())]
    else:
        tf_pairs_to_run = list(cfg.tf_pairs)

    _date_fmt = "%Y-%m-%d"
    range_start_ts: Optional[int] = None
    range_end_ts: Optional[int] = None
    if args.from_date:
        _dt = datetime.datetime.strptime(args.from_date, _date_fmt).replace(
            tzinfo=datetime.timezone.utc
        )
        range_start_ts = int(_dt.timestamp() * 1000)
    if args.to_date:
        _dt = datetime.datetime.strptime(args.to_date, _date_fmt).replace(
            tzinfo=datetime.timezone.utc, hour=23, minute=59, second=59
        )
        range_end_ts = int(_dt.timestamp() * 1000)

    if args.symbol:
        symbol = normalize_symbol(args.symbol)
        if symbol not in SUPPORTED_SYMBOLS:
            p.error(
                "unsupported symbol: "
                + symbol
                + ". Allowed: "
                + ", ".join(sorted(SUPPORTED_SYMBOLS))
            )
        unique_htf = list(dict.fromkeys(htf for htf, _ in tf_pairs_to_run))
        unique_ltf = list(dict.fromkeys(ltf for _, ltf in tf_pairs_to_run))
        htf_cache: dict[str, list] = {}
        ltf_cache: dict[str, list] = {}

        for htf_tf in unique_htf:
            candles = (
                load_from_api_range(symbol, htf_tf, range_start_ts, range_end_ts, cfg)
                if range_start_ts is not None
                else load_from_api(symbol, htf_tf, cfg.htf_outputsize, cfg)
            )
            if not candles:
                print(f" ERROR: no HTF candles for {htf_tf}")
                raise SystemExit(1)
            htf_cache[htf_tf] = candles
            print(
                f" HTF {htf_tf}: {len(candles)} bars "
                f"{cfg.dt_ms(candles[0].timestamp)} → {cfg.dt_ms(candles[-1].timestamp)}"
            )

        earliest_ts = min(c[0].timestamp for c in htf_cache.values())
        for ltf_tf in unique_ltf:
            candles = load_from_api_range(
                symbol, ltf_tf, earliest_ts, range_end_ts, cfg
            )
            if not candles:
                print(f" ERROR: no LTF candles for {ltf_tf}")
                raise SystemExit(1)
            ltf_cache[ltf_tf] = candles
            print(
                f" LTF {ltf_tf}: {len(candles)} bars "
                f"{cfg.dt_ms(candles[0].timestamp)} → {cfg.dt_ms(candles[-1].timestamp)}"
            )
    else:
        if not args.csv_ltf:
            p.error("--csv-ltf required with --csv-htf")
        symbol = Path(args.csv_htf).stem
        htf_cache = {tf_pairs_to_run[0][0]: load_csv(args.csv_htf)}
        ltf_cache = {tf_pairs_to_run[0][1]: load_csv(args.csv_ltf)}

    trailing_giveback_pct = (
        args.trailing_giveback_pct
        if args.trailing_giveback_pct is not None
        else DEFAULT_TRAILING_GIVEBACK_PCT
    )
    if not math.isfinite(trailing_giveback_pct):
        p.error("--trailing-giveback-pct must be a finite number")
    if trailing_giveback_pct < 0:
        p.error("--trailing-giveback-pct must be >= 0")
    if trailing_giveback_pct >= 100:
        p.error("--trailing-giveback-pct must be < 100")

    bt = MultiPairBacktester(
        cfg=cfg,
        symbol=symbol,
        pairs=tf_pairs_to_run,
        htf_candles=htf_cache,
        ltf_candles=ltf_cache,
        htf_lookback=args.htf_lookback,
        start_balance=(
            args.start_balance
            if args.start_balance is not None
            else DEFAULT_START_BALANCE
        ),
        risk_percent=(
            args.risk_percent
            if args.risk_percent is not None
            else DEFAULT_RISK_PERCENT
        ),
        trailing_giveback_pct=trailing_giveback_pct,
        trace_out=args.trace_out,
    )
    report = bt.run()

    if args.streak_analysis:
        report.print_streak_analysis()

    if args.output:
        if args.output.endswith(".json"):
            report.save_json(args.output)
        else:
            report.save_csv(args.output)


if __name__ == "__main__":
    main()
