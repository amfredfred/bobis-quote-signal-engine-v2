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
      Even for master == ltf: inactive zones (None ltf_range) cost O(1) lookup
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
      • inv_hit array skipped entirely when use_inv is False
      • Profile scalars (use_be, use_inv, expiry_ms, tp1_mult) hoisted to
        __init__; eliminated per-call attribute dereference
"""

from __future__ import annotations

import sys
import os

# Force UTF-8 encoding on Windows
if sys.platform == "win32":
    import io

    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
    os.environ["PYTHONIOENCODING"] = "utf-8"

import argparse
import bisect
import csv
import datetime
import json
import logging
from dataclasses import replace as dc_replace
from functools import lru_cache
from pathlib import Path
from typing import Optional

import numpy as np
from zoneinfo import ZoneInfo

from config.settings import Settings, interval_to_minutes as _interval_to_minutes
from domain.assets.profiles import AssetRegistry, AssetProfile
from domain.entities.candle import Candle
from domain.entities.enums import SignalDirection, SignalOutcome
from domain.entities.trade import TradeSignal
from domain.market.structure import MarketStructure
from domain.market.swings import SwingDetector, detect_displacement
from domain.signals.builder import build_signal
from domain.signals.entry import find_entry  # shared entry dispatcher
from infrastructure.data_providers.market_data import MarketDataClient

logger = logging.getLogger(__name__)

_CSV_TZ = ZoneInfo("UTC")

interval_to_minutes: callable = lru_cache(maxsize=64)(_interval_to_minutes)

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


# ── ★ TIER 3: Optional Numba JIT ─────────────────────────────────────────────
# If numba is not installed the NumPy fallback is used automatically.
try:
    import numba as _numba

    @_numba.njit(cache=True, fastmath=True)
    def _njit_simulate_with_retrace(
        ts: np.ndarray,
        high: np.ndarray,
        low: np.ndarray,
        close: np.ndarray,
        is_short: bool,
        use_be: bool,
        use_inv: bool,
        entry: float,
        sl: float,
        tp1: float,
        tp2: float,
        rng_hi: float,
        rng_lo: float,
        expiry_cutoff: np.int64,
        rr: float,
        risk_pips: float,
        tp1_mult: float,
    ):
        """Single O(n) pass — tracks if price revisits entry after TP1.

        tp1 "strictly prior" semantics: tp1_prev is updated AFTER the SL/INV
        checks for the current bar, so SL and TP1 on the same bar resolve in
        favour of SL.  The hit_entry_after_tp1 flag is evaluated on bars where
        tp1_prev is already True, i.e. starting from the bar AFTER TP1 fired.

        Return tuple: (outcome_code, bar_index, close_px, realized_rr, hit_entry_after_tp1)
        """
        n = len(ts)
        tp1_prev = False
        tp1_hit_index = -1
        hit_entry_after_tp1 = False

        for i in range(n):
            if ts[i] > expiry_cutoff:
                break

            h = high[i]
            l = low[i]
            c = close[i]

            if is_short:
                tp1_now = l <= tp1
                tp2_now = l <= tp2
                sl_now = h >= sl
                inv_now = use_inv and (c > rng_hi)
                entry_now = h >= entry  # retrace UP to entry after TP1
            else:
                tp1_now = h >= tp1
                tp2_now = h >= tp2
                sl_now = l <= sl
                inv_now = use_inv and (c < rng_lo)
                entry_now = l <= entry  # retrace DOWN to entry after TP1

            # Track entry revisit after TP1.
            # Checked BEFORE updating tp1_prev — a same-bar retrace on the TP1
            # bar is intentionally excluded to match "strictly prior" semantics.
            if tp1_prev and entry_now and not hit_entry_after_tp1:
                hit_entry_after_tp1 = True

            # TP2: tp1 at-or-before this bar wins (highest priority)
            if (tp1_prev or tp1_now) and tp2_now:
                return 3, i, tp2, rr, hit_entry_after_tp1

            # INV: higher priority than SL on same-bar conflict
            if inv_now:
                if tp1_prev and use_be:
                    return 2, i, entry, rr * tp1_mult, hit_entry_after_tp1
                return 1, i, c, -(abs(entry - c) / risk_pips), hit_entry_after_tp1

            # SL
            if sl_now:
                if tp1_prev and use_be:
                    return 2, i, entry, rr * tp1_mult, hit_entry_after_tp1
                return 1, i, sl, -1.0, hit_entry_after_tp1

            # Update AFTER checks — preserves "strictly prior" semantics
            if tp1_now and not tp1_prev:
                tp1_prev = True
                tp1_hit_index = i

        return 0, n, 0.0, 0.0, False

    _NUMBA_AVAILABLE = True
    logger.debug("Numba JIT simulation enabled (single-pass O(n))")

except ImportError:
    _NUMBA_AVAILABLE = False
    logger.debug("Numba not installed — NumPy vectorised simulation active")


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
    )

    def __init__(
        self,
        signal: TradeSignal,
        outcome: SignalOutcome,
        realized_rr: float,
        close_ts: Optional[int],
        close_px: Optional[float],
        hit_entry_after_tp1: bool = False,
    ) -> None:
        self.signal = signal
        self.outcome = outcome
        self.realized_rr = realized_rr
        self.close_ts = close_ts
        self.close_price = close_px
        # FIX: first-class constructor param — never set post-construction.
        self.hit_entry_after_tp1 = hit_entry_after_tp1

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
            "htf_interval": s.htf_interval,
            "ltf_interval": s.ltf_interval,
            "pattern": s.rejection_candle.pattern.value,
            "wick_ratio": round(s.rejection_candle.wick_ratio, 3),
            "htf_high": round(s.htf_range.range_high, 5),
            "htf_low": round(s.htf_range.range_low, 5),
            "tp_level": round(s.htf_range.tp_level, 5),
            "ltf_high": round(s.ltf_range.range_high, 5),
            "ltf_low": round(s.ltf_range.range_low, 5),
        }


# ── BacktestReport ────────────────────────────────────────────────────────────
class BacktestReport:
    def __init__(
        self, symbol: str, results: list[BacktestResult], cfg: Settings
    ) -> None:
        self.symbol = symbol
        self.results = results
        self.cfg = cfg
        self._registry = AssetRegistry(cfg)
        self.profile = self._registry.get(symbol)  # base (combined summary)

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

        W, R = BOLD, RESET
        sep = "─" * 64
        print(f"\n{W}{sep}{R}")
        print(f"{W} BACKTEST · {self.symbol} · {label}{R}")
        print(sep)

        if not compact:
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
            print(
                f" Signals={len(r)} W={len(wins)} BE={len(bes)} L={len(losses)}"
                f" Inv/Exp={len(invals)}/{len(expd)}"
            )

        print(
            f" {'Win rate (closed)':<32} "
            f"{'%s%.1f%%%s' % (GREEN if win_rate >= 50 else RED, win_rate, R)}"
        )
        print(
            f" {'Profit factor':<32} "
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
            max_rr = "∞" if profile.max_rr == 0 else profile.max_rr
            print(f" {' R:R window':<32} [{profile.min_rr}, {max_rr}]")
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
            print(
                f" {dir_label:<8} W={len(w)} BE={len(b)} L={len(l)}"
                f" WR={wr:.0f}% {col}{tr:+.2f}R{R}"
            )
        print(f"{W}{sep}{R}")

    def save_csv(self, path: str) -> None:
        if not self.results:
            return
        fields = list(self.results[0].to_dict().keys())
        with open(path, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            w.writerows(r.to_dict() for r in self.results)
        print(f" {GREEN}Saved → {path}{RESET}")

    def save_json(self, path: str) -> None:
        with open(path, "w") as f:
            json.dump([r.to_dict() for r in self.results], f, indent=2)
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
    ) -> None:
        self.cfg = cfg
        self.symbol = symbol
        self.pairs = pairs
        self.htf_candles = htf_candles
        self.ltf_candles = ltf_candles
        self.htf_lookback = htf_lookback or cfg.htf_lookback
        self._registry = AssetRegistry(cfg)

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
        #   value → (ltf_hi_idx, ltf_range_or_None, rej_result_or_None)
        #
        #   A cache hit (same ltf_hi_idx) means ltf_zone is byte-identical to
        #   the previous call, so find_ltf and find_entry results are reused.
        #   Hit rate ≈ 1 − (1 / ticks_per_ltf_bar); e.g. 98 % for 1 m / 1 h.
        self._zone_cache: dict[tuple, tuple] = {}

        # ★ TIER 2: Zone lo-index cache
        #   bisect_left(ltf_ts_idx, zone_start) is immutable for a zone's
        #   lifetime — compute once and store.
        self._zone_lo: dict[tuple, int] = {}

        # ★ TIER 4: Profile scalars hoisted to instance (no per-call dereference)
        profile = self.profile
        self._use_be: bool = profile.use_breakeven
        self._use_inv: bool = profile.use_invalidation
        self._expiry_ms: int = int(profile.signal_expiry_hours * 3_600_000)
        self._tp1_mult: float = profile.tp1_multiplier

        # Pre-computed constants per pair
        self._stale_ms: dict[tuple[str, str], int] = {
            (htf, ltf): int(cfg.rejection_stale_hours(ltf) * 3_600_000)
            for htf, ltf in pairs
        }
        self._htf_interval_ms: dict[str, int] = {
            htf: interval_to_minutes(htf) * 60_000 for htf, _ in pairs
        }

        assert cfg.entry_model in ("candle_pattern", "crt", "all"), (
            f"Invalid entry_model: {cfg.entry_model!r}. "
            "Must be 'candle_pattern', 'crt', or 'all'."
        )
        logger.info("[%s] Entry model: %s", symbol, cfg.entry_model)

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
        htf_lookback = self.htf_lookback
        use_tf = profile.use_trend_filter
        use_mtp = profile.multi_tf_independent_positions
        symbol = self.symbol

        ltf_intervals = list(dict.fromkeys(ltf for _, ltf in self.pairs))
        master_ltf = min(ltf_intervals, key=interval_to_minutes)
        master_candles = self.ltf_candles[master_ltf]
        max_htf_mins = max(interval_to_minutes(htf) for htf, _ in self.pairs)
        min_ltf_mins = interval_to_minutes(master_ltf)
        ltf_lb = max(100, htf_lookback * (max_htf_mins // min_ltf_mins))
        n = len(master_candles)
        total_steps = n - ltf_lb
        pairs_str = " | ".join(f"{h}/{l}" for h, l in self.pairs)

        print(f"\n{'='*64}")
        print(f"{BOLD} BACKTEST · {symbol} · {pairs_str}{RESET}")
        print(f" Master LTF : {master_ltf} ({n:,} bars) warm-up={ltf_lb}")
        print(f"{'='*64}\n")

        dead_zones: set[tuple] = set()
        seen_ltf: set[tuple] = set()
        open_dir: dict[tuple, int] = {}
        expiry_ms = int(profile.signal_expiry_hours * 3_600_000)
        pivot_bars = cfg.pivot_bars
        max_zones  = cfg.max_htf_zones_per_dir
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
        _find_ltf = SwingDetector.find_ltf_range
        _detect_struct = MarketStructure.detect
        _build_signal = build_signal
        _simulate = self._simulate
        _print_result = self._print_result

        # Entry model args (captured once, passed to shared dispatcher each call)
        _entry_model = cfg.entry_model
        _min_wick_ratio = cfg.min_wick_ratio

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

            # Expire direction locks
            if open_dir:
                expired = [dk for dk, cts in open_dir.items() if cts <= current_ts]
                for dk in expired:
                    del open_dir[dk]

            for htf_interval, ltf_interval in self.pairs:
                htf_all = self.htf_candles[htf_interval]
                htf_ts_idx = self._htf_ts[htf_interval]

                # O(log n) HTF window
                hi_htf = _bisect_right(htf_ts_idx, current_ts)
                lo_htf = max(0, hi_htf - htf_lookback)
                htf_w = htf_all[lo_htf:hi_htf]
                if len(htf_w) < htf_lookback // 3:
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
                cached_rng = _rng_cache.get(htf_interval)
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
                    # Uses htf_all (full series) so there is always enough context
                    # to compute the average body before the BOS candle.
                    if use_disp:
                        htf_all_full = self.htf_candles[htf_interval]
                        disp_atr_mult = cfg.displacement_mult_for(htf_interval, ltf_interval)
                        htf_ranges = [
                            z for z in htf_ranges
                            if detect_displacement(
                                htf_all_full,
                                z.broken_at,
                                atr_period=disp_atr_period,
                                atr_mult=disp_atr_mult,
                            )
                        ]
                    _rng_cache[htf_interval] = (last_htf_ts, htf_ranges)
                else:
                    htf_ranges = cached_rng[1]

                ltf_all = self.ltf_candles[ltf_interval]
                ltf_ts_idx = self._ltf_ts[ltf_interval]
                stale_ms = _stale_ms[(htf_interval, ltf_interval)]

                for htf_range in htf_ranges:
                    zone_key = (htf_range.timestamp, htf_range.bos_direction.value)
                    if zone_key in dead_zones:
                        continue

                    # ★ TIER 2: lo-index computed once per zone (zone_start is immutable)
                    if zone_key not in _zone_lo:
                        zone_start = htf_range.broken_at or htf_range.timestamp
                        _zone_lo[zone_key] = _bisect_left(ltf_ts_idx, zone_start)
                    ltf_lo_idx = _zone_lo[zone_key]

                    ltf_hi_idx = _bisect_right(ltf_ts_idx, current_ts)

                    # ★ TIER 1: Zone-level find_ltf + find_entry cache
                    #   Key: ltf_hi_idx — changes only when a new LTF bar arrives.
                    #   On a cache hit the ltf_zone list is NEVER constructed.
                    cached = _zone_cache.get(zone_key)
                    if cached is not None and cached[0] == ltf_hi_idx:
                        # Fast path — reuse previously computed results
                        ltf_range = cached[1]
                        if not ltf_range:
                            continue
                        rej_result = cached[2]
                    else:
                        # Slow path — compute and store
                        ltf_zone = ltf_all[ltf_lo_idx:ltf_hi_idx]
                        ltf_range = _find_ltf(ltf_zone, htf_range, ltf_all)
                        rej_result = (
                            find_entry(
                                ltf_zone,
                                ltf_range,
                                htf_range,
                                _entry_model,
                                _min_wick_ratio,
                            )
                            if ltf_range
                            else None
                        )
                        _zone_cache[zone_key] = (ltf_hi_idx, ltf_range, rej_result)
                        if not ltf_range:
                            continue

                    direction = ltf_range.direction.value
                    if structure is not None and not structure.allows(direction):
                        continue

                    ltf_key = (ltf_range.timestamp, direction)
                    if ltf_key in seen_ltf:
                        continue

                    if not rej_result:
                        continue

                    rejection, _ = rej_result
                    pair_dir_key = (
                        (symbol, htf_interval, ltf_interval, direction)
                        if use_mtp
                        else (symbol, direction)
                    )
                    if pair_dir_key in open_dir:
                        continue
                    if current_ts - rejection.timestamp > stale_ms:
                        continue

                    # FIX: include direction in signal_id to match signal_service
                    # and prevent collisions when LONG/SHORT share a rejection timestamp.
                    signal = _build_signal(
                        symbol=symbol,
                        htf_interval=htf_interval,
                        ltf_interval=ltf_interval,
                        htf_range=htf_range,
                        ltf_range=ltf_range,
                        rejection=rejection,
                        signal_id=(
                            f"{symbol}_{htf_interval}_{ltf_interval}"
                            f"_{rejection.timestamp}_{direction}"
                        ),
                        profile=_pair_profiles[(htf_interval, ltf_interval)],
                        session_tz=cfg.session_tz,
                    )
                    if signal is None:
                        continue

                    cur_ltf_i = _bisect_right(ltf_ts_idx, current_ts) - 1
                    future_np = self.ltf_candles_np[ltf_interval][cur_ltf_i + 1 :]
                    result = _simulate(signal, future_np)

                    seen_ltf.add(ltf_key)

                    # Mark zone dead and evict from both caches — the zone will
                    # never generate a second signal, so holding cached state for
                    # it wastes memory for the remainder of the backtest.
                    dead_zones.add(zone_key)
                    _zone_cache.pop(zone_key, None)
                    _zone_lo.pop(zone_key, None)

                    open_dir[pair_dir_key] = result.close_ts or (
                        signal.created_at + expiry_ms
                    )
                    self.results.append(result)
                    _print_result(result)
                    break  # one signal per LTF tick per HTF zone scan

        print(f"\r [{'█'*20}] 100.0% signals={len(self.results)}\n")

        report = BacktestReport(symbol, self.results, cfg)
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

        # ★ TIER 4: Hoisted scalars — no attribute dereference in hot path
        is_short = signal.direction == SignalDirection.SHORT
        use_be = self._use_be
        use_inv = self._use_inv
        entry = signal.entry_price
        sl = signal.stop_loss
        tp1 = signal.tp1
        tp2 = signal.tp2
        rng_hi = signal.ltf_range.range_high
        rng_lo = signal.ltf_range.range_low
        birth = signal.created_at
        expiry_cutoff = np.int64(birth + self._expiry_ms)
        rr = signal.risk_reward_ratio
        risk_pips = signal.risk_pips
        tp1_mult = self._tp1_mult

        ts = future_np["timestamp"]

        # ★ TIER 4: O(log n) expiry trim — timestamps are monotonically increasing
        first_exp = int(np.searchsorted(ts, expiry_cutoff, side="right"))
        if first_exp == 0:
            return BacktestResult(signal, SignalOutcome.EXPIRED, 0.0, None, None)
        if first_exp < len(ts):
            future_np = future_np[:first_exp]
            ts = ts[:first_exp]

        # ── ★ TIER 3: Numba single-pass path ────────────────────────────────────
        if _NUMBA_AVAILABLE:
            code, idx, close_px, realized, hit_entry_after_tp1 = (
                _njit_simulate_with_retrace(
                    ts,
                    future_np["high"],
                    future_np["low"],
                    future_np["close"],
                    is_short,
                    use_be,
                    use_inv,
                    entry,
                    sl,
                    tp1,
                    tp2,
                    rng_hi,
                    rng_lo,
                    expiry_cutoff,
                    rr,
                    risk_pips,
                    tp1_mult,
                )
            )
            if code == 0:
                return BacktestResult(signal, SignalOutcome.EXPIRED, 0.0, None, None)
            close_ts = int(ts[idx])
            if code == 3:
                outcome = SignalOutcome.WIN_FULL
            elif code == 2:
                outcome = SignalOutcome.BREAKEVEN
            else:
                outcome = SignalOutcome.LOSS

            return BacktestResult(
                signal,
                outcome,
                realized,
                close_ts,
                float(close_px),
                hit_entry_after_tp1=hit_entry_after_tp1,
            )

        # ── ★ TIER 4: NumPy fallback ─────────────────────────────────────────────
        high = future_np["high"]
        low = future_np["low"]
        close = future_np["close"]
        n = len(ts)

        if is_short:
            tp1_tgt = low <= tp1
            tp2_tgt = low <= tp2
            sl_hit = high >= sl
            inv_hit = (close > rng_hi) if use_inv else None
            entry_hit = high >= entry  # SHORT retrace: price goes back UP to entry
        else:
            tp1_tgt = high >= tp1
            tp2_tgt = high >= tp2
            sl_hit = low <= sl
            inv_hit = (close < rng_lo) if use_inv else None
            entry_hit = low <= entry  # LONG retrace: price comes back DOWN to entry

        # First TP1 hit
        tp1_any = bool(tp1_tgt.any())
        tp1_first = int(tp1_tgt.argmax()) if tp1_any else n

        # Track whether price revisited entry after TP1.
        # Slice starts at tp1_first + 1 to match the Numba kernel's "strictly
        # prior" semantics: tp1_prev is False on the TP1 bar itself (updated
        # AFTER per-bar checks), so a same-bar retrace on the TP1 bar is not
        # detected in either path.
        hit_entry_after_tp1 = False
        if tp1_any and tp1_first < n:
            entry_hit_after_tp1 = entry_hit[tp1_first + 1 :]
            if entry_hit_after_tp1.any():
                entry_hit_idx = tp1_first + 1 + int(entry_hit_after_tp1.argmax())
                tp2_sub = tp2_tgt[tp1_first + 1 :]
                if tp2_sub.any():
                    tp2_idx_relative = int(tp2_sub.argmax())
                    tp2_absolute = tp1_first + 1 + tp2_idx_relative
                    hit_entry_after_tp1 = entry_hit_idx < tp2_absolute
                else:
                    hit_entry_after_tp1 = True

        # TP2: only search at/after tp1_first
        if tp1_any:
            tp2_sub = tp2_tgt[tp1_first:]
            tp2_idx = tp1_first + int(tp2_sub.argmax()) if tp2_sub.any() else n
        else:
            tp2_idx = n

        # First SL hit
        sl_idx = int(sl_hit.argmax()) if sl_hit.any() else n

        # First INV hit
        if use_inv and inv_hit is not None:
            inv_idx = int(inv_hit.argmax()) if inv_hit.any() else n
        else:
            inv_idx = n

        stop_idx = min(tp2_idx, inv_idx, sl_idx)

        if stop_idx == n:
            return BacktestResult(signal, SignalOutcome.EXPIRED, 0.0, None, None)

        close_ts = int(ts[stop_idx])
        tp1_before = tp1_first < stop_idx

        if stop_idx == tp2_idx:
            return BacktestResult(
                signal,
                SignalOutcome.WIN_FULL,
                rr,
                close_ts,
                tp2,
                hit_entry_after_tp1=hit_entry_after_tp1,
            )

        if stop_idx == inv_idx:
            if tp1_before and use_be:
                return BacktestResult(
                    signal,
                    SignalOutcome.BREAKEVEN,
                    rr * tp1_mult,
                    close_ts,
                    entry,
                    hit_entry_after_tp1=hit_entry_after_tp1,
                )
            inv_px = float(close[stop_idx])
            return BacktestResult(
                signal,
                SignalOutcome.LOSS,
                -(abs(entry - inv_px) / risk_pips),
                close_ts,
                inv_px,
                hit_entry_after_tp1=hit_entry_after_tp1,
            )

        # Stop-loss.
        # Same-bar SL vs TP1: SL wins (conservative). tp1_before excludes
        # equality (tp1_first < stop_idx, not <=), so when SL and TP1 hit on
        # the same bar tp1_before is False and we fall through to a full loss.
        # This matches the Numba kernel's "strictly prior" behaviour.
        if tp1_before and use_be:
            return BacktestResult(
                signal,
                SignalOutcome.BREAKEVEN,
                rr * tp1_mult,
                close_ts,
                entry,
                hit_entry_after_tp1=hit_entry_after_tp1,
            )

        return BacktestResult(
            signal,
            SignalOutcome.LOSS,
            -1.0,
            close_ts,
            sl,
            hit_entry_after_tp1=hit_entry_after_tp1,
        )

    # ── console output ────────────────────────────────────────────────────────
    def _print_result(self, r: BacktestResult) -> None:
        s = r.signal
        arrow = DOWN if s.direction == SignalDirection.SHORT else UP
        tf_tag = f"{DIM}[{s.htf_interval}/{s.ltf_interval}]{RESET}"
        et_pattern = f"{DIM}[{s.rejection_candle.pattern.value}]{RESET}"

        # FIX: hit_entry_after_tp1 is always set via __init__ — direct access.
        retrace_marker = f" {YELLOW}↺{RESET}" if r.hit_entry_after_tp1 else ""

        outcome_str = {
            SignalOutcome.WIN_FULL: f"{GREEN}WIN +{r.realized_rr:.2f}R{RESET}",
            SignalOutcome.BREAKEVEN: f"{YELLOW}BE +{r.realized_rr:.2f}R{RESET}",
            SignalOutcome.LOSS: f"{RED}LOSS {r.realized_rr:.2f}R{RESET}",
            SignalOutcome.INVALIDATED: f"{DIM}VOID 0.0R{RESET}",
            SignalOutcome.EXPIRED: f"{DIM}EXPD 0.0R{RESET}",
        }.get(r.outcome, f"{DIM}?{RESET}")

        print(f"\r{' '*80}\r", end="")
        print(
            f" {arrow} {BOLD}{s.direction.value:5s}{RESET} {tf_tag} {et_pattern} "
            f"{CYAN}{self.cfg.dt_ms(s.triggered_at)}{RESET} "
            f"E={s.entry_price:.5f} SL={s.stop_loss:.5f} TP2={s.tp2:.5f} "
            f"RR={s.risk_reward_ratio:.2f} → {outcome_str}{retrace_marker} "
            f"closed {self.cfg.dt_ms(r.close_ts) if r.close_ts else 'OPEN'}"
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
                            ts = int(dt.replace(tzinfo=_CSV_TZ).timestamp() * 1000)
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
    client = MarketDataClient(cfg.local_base_url)
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
    client = MarketDataClient(cfg.local_base_url)
    try:
        return client.fetch_candles_range(symbol, interval, start_ts, end_ts)
    finally:
        client.close()


# ── CLI ───────────────────────────────────────────────────────────────────────
def main() -> None:
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
    p.add_argument("--no-invalidation", action="store_true")
    p.add_argument("--no-trend-filter", action="store_true")
    p.add_argument("--no-session-filter", action="store_true")
    p.add_argument("--from-date", dest="from_date", metavar="YYYY-MM-DD")
    p.add_argument("--to-date", dest="to_date", metavar="YYYY-MM-DD")
    p.add_argument("--verbose", action="store_true")
    args = p.parse_args()

    cfg = Settings.from_env()
    overrides: dict = {}
    if args.no_breakeven:
        overrides["use_breakeven"] = False
    if args.no_invalidation:
        overrides["use_invalidation"] = False
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
            tzinfo=cfg.session_tz
        )
        range_start_ts = int(_dt.timestamp() * 1000)
    if args.to_date:
        _dt = datetime.datetime.strptime(args.to_date, _date_fmt).replace(
            tzinfo=cfg.session_tz, hour=23, minute=59, second=59
        )
        range_end_ts = int(_dt.timestamp() * 1000)

    if args.symbol:
        symbol = args.symbol
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

    bt = MultiPairBacktester(
        cfg=cfg,
        symbol=symbol,
        pairs=tf_pairs_to_run,
        htf_candles=htf_cache,
        ltf_candles=ltf_cache,
        htf_lookback=args.htf_lookback,
    )
    report = bt.run()

    if args.output:
        if args.output.endswith(".json"):
            report.save_json(args.output)
        else:
            report.save_csv(args.output)


if __name__ == "__main__":
    main()
