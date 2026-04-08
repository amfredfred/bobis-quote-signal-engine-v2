"""
app/backtesting/backtest.py — backtester using the new domain layer.

Ported from backtesting/backtest.py with updated imports:
  - interfaces.interfaces  → domain.entities.*
  - core.swing_utils       → domain.market.swings
  - core.rejection_utils   → domain.market.rejection
  - core.market_structure  → domain.market.structure
  - core.signal_utils      → domain.signals.builder
  - config.config          → config.settings
  - config.asset_config    → domain.assets.profiles
  - data.chart_data        → infrastructure.data_providers.chart_data
  - data.market_data       → infrastructure.data_providers.market_data
"""

from __future__ import annotations

import argparse
import bisect
import csv
import datetime
import json
import logging
import sys
from dataclasses import replace as dc_replace
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

from config.settings import Settings, interval_to_minutes
from domain.assets.profiles import AssetRegistry, AssetProfile
from domain.entities.candle import Candle
from domain.entities.enums import SignalDirection, SignalOutcome
from domain.entities.trade import TradeSignal
from domain.market.rejection import RejectionDetector
from domain.market.structure import MarketStructure, TrendBias
from domain.market.swings import SwingDetector
from domain.signals.builder import build_signal
from infrastructure.data_providers.chart_data import build_chart_data

logger = logging.getLogger(__name__)

_CSV_TZ = ZoneInfo("UTC")

GREEN  = "\033[92m"
RED    = "\033[91m"
YELLOW = "\033[93m"
CYAN   = "\033[96m"
BOLD   = "\033[1m"
DIM    = "\033[2m"
RESET  = "\033[0m"
UP     = f"{GREEN}▲{RESET}"
DOWN   = f"{RED}▼{RESET}"


def _bisect_ts(ts_list: list[int], ts: int) -> int:
    lo, hi = 0, len(ts_list)
    while lo < hi:
        mid = (lo + hi) // 2
        if ts_list[mid] < ts:
            lo = mid + 1
        else:
            hi = mid
    return lo


# ── BacktestResult ────────────────────────────────────────────────────────────

class BacktestResult:
    def __init__(
        self,
        signal:      TradeSignal,
        outcome:     SignalOutcome,
        realized_rr: float,
        close_ts:    Optional[int],
        close_px:    Optional[float],
    ) -> None:
        self.signal      = signal
        self.outcome     = outcome
        self.realized_rr = realized_rr
        self.close_ts    = close_ts
        self.close_price = close_px
        self.chart_path: Optional[Path] = None
        self.htf_window: list[Candle]   = []
        self.ltf_index:  int            = 0

    def to_dict(self) -> dict:
        s    = self.signal
        _fmt = lambda ms: datetime.datetime.utcfromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M:%S")
        return {
            "id":           s.id,
            "symbol":       s.symbol,
            "direction":    s.direction.value,
            "entry_dt":     _fmt(s.triggered_at),
            "close_dt":     _fmt(self.close_ts) if self.close_ts else "",
            "entry":        round(s.entry_price, 5),
            "sl":           round(s.stop_loss, 5),
            "tp1":          round(s.tp1, 5),
            "tp2":          round(s.tp2, 5),
            "rr":           round(s.risk_reward_ratio, 3),
            "outcome":      self.outcome.value,
            "realized_rr":  round(self.realized_rr, 3),
            "htf_interval": s.htf_interval,
            "ltf_interval": s.ltf_interval,
            "pattern":      s.rejection_candle.pattern.value,
            "wick_ratio":   round(s.rejection_candle.wick_ratio, 3),
            "htf_high":     round(s.htf_range.range_high, 5),
            "htf_low":      round(s.htf_range.range_low, 5),
            "tp_level":     round(s.htf_range.tp_level, 5),
            "ltf_high":     round(s.ltf_range.range_high, 5),
            "ltf_low":      round(s.ltf_range.range_low, 5),
            "chart":        str(self.chart_path) if self.chart_path else "",
        }


# ── BacktestReport ────────────────────────────────────────────────────────────

class BacktestReport:
    def __init__(self, symbol: str, results: list[BacktestResult], cfg: Settings) -> None:
        self.symbol  = symbol
        self.results = results
        self.cfg     = cfg
        self.profile = AssetRegistry(cfg).get(symbol)

    def print(self) -> None:
        if not self.results:
            print(f"  {YELLOW}No signals generated.{RESET}")
            return
        self._print_summary("COMBINED", self.results, self.profile)
        pairs = sorted(set((x.signal.htf_interval, x.signal.ltf_interval) for x in self.results))
        if len(pairs) > 1:
            for htf, ltf in pairs:
                subset = [x for x in self.results if x.signal.htf_interval == htf and x.signal.ltf_interval == ltf]
                self._print_summary(f"{htf}/{ltf}", subset, self.profile, compact=True)

    def _print_summary(self, label: str, r: list[BacktestResult], profile: AssetProfile, compact: bool = False) -> None:
        wins   = [x for x in r if x.outcome == SignalOutcome.WIN_FULL]
        losses = [x for x in r if x.outcome == SignalOutcome.LOSS]
        bes    = [x for x in r if x.outcome == SignalOutcome.BREAKEVEN]
        invals = [x for x in r if x.outcome == SignalOutcome.INVALIDATED]
        expd   = [x for x in r if x.outcome == SignalOutcome.EXPIRED]
        closed = wins + losses + bes

        total_r  = sum(x.realized_rr for x in r)
        win_rate = len(wins) / len(closed) * 100 if closed else 0.0
        pf_num   = sum(x.realized_rr for x in wins + bes)
        pf_den   = abs(sum(x.realized_rr for x in losses))
        pf       = pf_num / pf_den if pf_den else float("inf")
        longs    = [x for x in r if x.signal.direction == SignalDirection.LONG]
        shorts   = [x for x in r if x.signal.direction == SignalDirection.SHORT]

        W, R = BOLD, RESET
        sep = f"{'─'*64}"
        print(f"\n{W}{sep}{R}")
        print(f"{W}  BACKTEST  ·  {self.symbol}  ·  {label}{R}")
        print(sep)

        if not compact:
            print(f"  {'Total signals':<32} {len(r)}")
            print(f"  {'  LONG / SHORT':<32} {len(longs)} / {len(shorts)}")
            print(f"  {'Closed (W+BE+L)':<32} {len(closed)}")
            print(f"  {'  Wins':<32} {GREEN}{len(wins)}{R}")
            print(f"  {'  Breakevens':<32} {YELLOW}{len(bes)}{R}")
            print(f"  {'  Losses':<32} {RED}{len(losses)}{R}")
            print(f"  {'  Invalidated / Expired':<32} {DIM}{len(invals)} / {len(expd)}{R}")
            print(f"  {'─'*42}")
        else:
            print(f"  Signals={len(r)}  W={len(wins)} BE={len(bes)} L={len(losses)}  Inv/Exp={len(invals)}/{len(expd)}")

        print(f"  {'Win rate (closed)':<32} {'%s%.1f%%%s' % (GREEN if win_rate >= 50 else RED, win_rate, R)}")
        print(f"  {'Profit factor':<32} {'%s%.2f%s' % (GREEN if pf >= 1 else RED, pf, R)}")
        print(f"  {'Total R':<32} {'%s%+.2fR%s' % (GREEN if total_r >= 0 else RED, total_r, R)}")
        if wins or bes:
            avg_w = sum(x.realized_rr for x in wins + bes) / len(wins + bes)
            print(f"  {'Avg win/BE':<32} {GREEN}+{avg_w:.2f}R{R}")
        if losses:
            avg_l = sum(x.realized_rr for x in losses) / len(losses)
            print(f"  {'Avg loss':<32} {RED}{avg_l:.2f}R{R}")

        if not compact:
            print(f"  {'─'*42}")
            max_rr = "∞" if profile.max_rr == 0 else profile.max_rr
            print(f"  {'  R:R window':<32} [{profile.min_rr}, {max_rr}]")
            print(f"  {'  Wick ratio':<32} [{self.cfg.min_wick_ratio}, ∞]")

        print(f"  {'─'*42}")
        for dir_label, subset in [("LONG", longs), ("SHORT", shorts)]:
            if not subset:
                continue
            w  = [x for x in subset if x.outcome == SignalOutcome.WIN_FULL]
            b  = [x for x in subset if x.outcome == SignalOutcome.BREAKEVEN]
            l  = [x for x in subset if x.outcome == SignalOutcome.LOSS]
            cl = w + b + l
            wr = len(w) / len(cl) * 100 if cl else 0.0
            tr = sum(x.realized_rr for x in subset)
            col = GREEN if tr >= 0 else RED
            print(f"  {dir_label:<8}  W={len(w)} BE={len(b)} L={len(l)}  WR={wr:.0f}%  {col}{tr:+.2f}R{R}")
        print(f"{W}{sep}{R}")

    def save_csv(self, path: str) -> None:
        if not self.results:
            return
        fields = list(self.results[0].to_dict().keys())
        with open(path, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            w.writerows(r.to_dict() for r in self.results)
        print(f"  {GREEN}Saved → {path}{RESET}")

    def save_json(self, path: str) -> None:
        with open(path, "w") as f:
            json.dump([r.to_dict() for r in self.results], f, indent=2)
        print(f"  {GREEN}Saved → {path}{RESET}")


# ── MultiPairBacktester ───────────────────────────────────────────────────────

class MultiPairBacktester:

    def __init__(
        self,
        cfg:          Settings,
        symbol:       str,
        pairs:        list[tuple[str, str]],
        htf_candles:  dict[str, list[Candle]],
        ltf_candles:  dict[str, list[Candle]],
        htf_lookback: Optional[int] = None,
    ) -> None:
        self.cfg          = cfg
        self.symbol       = symbol
        self.pairs        = pairs
        self.htf_candles  = htf_candles
        self.ltf_candles  = ltf_candles
        self.htf_lookback = htf_lookback or cfg.htf_lookback
        self._registry    = AssetRegistry(cfg)
        self.profile      = self._registry.get(symbol)
        self.results:     list[BacktestResult] = []
        self._ltf_ts:     dict[str, list[int]] = {
            ltf: [c.timestamp for c in candles]
            for ltf, candles in ltf_candles.items()
        }

    def run(self) -> BacktestReport:
        profile      = self.profile
        htf_lookback = self.htf_lookback

        ltf_intervals = list(dict.fromkeys(ltf for _, ltf in self.pairs))
        master_ltf     = min(ltf_intervals, key=interval_to_minutes)
        master_candles = self.ltf_candles[master_ltf]
        master_ts_list = self._ltf_ts[master_ltf]

        max_htf_mins = max(interval_to_minutes(htf) for htf, _ in self.pairs)
        min_ltf_mins = interval_to_minutes(master_ltf)
        ltf_lb       = max(100, htf_lookback * (max_htf_mins // min_ltf_mins))
        n            = len(master_candles)
        total_steps  = n - ltf_lb

        pairs_str = "  |  ".join(f"{h}/{l}" for h, l in self.pairs)
        print(f"\n{'='*64}")
        print(f"{BOLD}  BACKTEST  ·  {self.symbol}  ·  {pairs_str}{RESET}")
        print(f"  Master LTF : {master_ltf}  ({n:,} bars)  warm-up={ltf_lb}")
        print(f"{'='*64}\n")

        dead_zones: set[tuple]     = set()
        seen_ltf:   set[tuple]     = set()
        open_dir:   dict[tuple, int] = {}

        for ltf_i in range(ltf_lb, n):
            step = ltf_i - ltf_lb
            if step % 200 == 0:
                pct = step / max(total_steps, 1) * 100
                bar = "█" * int(pct / 5) + "░" * (20 - int(pct / 5))
                print(f"\r  [{bar}] {pct:5.1f}%  signals={len(self.results)}", end="", flush=True)

            current_ts = master_candles[ltf_i].timestamp

            for dk, close_ts in list(open_dir.items()):
                if close_ts <= current_ts:
                    del open_dir[dk]

            for htf_interval, ltf_interval in self.pairs:
                htf_all = self.htf_candles[htf_interval]
                htf_w   = [c for c in htf_all if c.timestamp <= current_ts][-htf_lookback:]
                if len(htf_w) < htf_lookback // 3:
                    continue

                ltf_all     = self.ltf_candles[ltf_interval]
                ltf_ts_idx  = self._ltf_ts[ltf_interval]
                stale_ms = int(self.cfg.rejection_stale_hours(ltf_interval) * 3_600_000)

                if profile.use_trend_filter:
                    structure = MarketStructure.detect(htf_w, pivot_bars=self.cfg.pivot_bars)
                    if structure.bias.value == "NEUTRAL":
                        continue
                else:
                    structure = None

                htf_interval_ms = interval_to_minutes(htf_interval) * 60 * 1000
                for htf_range in SwingDetector.find_htf_ranges(
                    htf_w,
                    pivot_bars        = self.cfg.pivot_bars,
                    htf_interval_ms   = htf_interval_ms,
                    max_zones_per_dir = self.cfg.max_htf_zones_per_dir,
                ):
                    zone_key = (htf_range.timestamp, htf_range.bos_direction.value)
                    if zone_key in dead_zones:
                        continue

                    zone_start = htf_range.broken_at or htf_range.timestamp
                    lo       = _bisect_ts(ltf_ts_idx, zone_start)
                    hi       = bisect.bisect_right(ltf_ts_idx, current_ts)
                    ltf_zone = ltf_all[lo:hi]

                    ltf_range = SwingDetector.find_ltf_range(ltf_zone, htf_range, ltf_all)
                    if not ltf_range:
                        continue

                    direction = ltf_range.direction.value
                    if structure is not None and not structure.allows(direction):
                        continue

                    ltf_key = (ltf_range.timestamp, direction)
                    if ltf_key in seen_ltf:
                        continue

                    entries = SwingDetector.candles_entering_ltf(ltf_zone, ltf_range, htf_range)
                    if not entries:
                        continue

                    rej_result = RejectionDetector.find_most_recent(
                        entries, ltf_range, min_wick_ratio=self.cfg.min_wick_ratio
                    )
                    if not rej_result:
                        continue
                    rejection, _ = rej_result

                    pair_dir_key = (
                        (self.symbol, htf_interval, ltf_interval, direction)
                        if profile.multi_tf_independent_positions
                        else (self.symbol, direction)
                    )
                    if pair_dir_key in open_dir:
                        continue

                    if current_ts - rejection.timestamp > stale_ms:
                        continue

                    signal = build_signal(
                        symbol       = self.symbol,
                        htf_interval = htf_interval,
                        ltf_interval = ltf_interval,
                        htf_range    = htf_range,
                        ltf_range    = ltf_range,
                        rejection    = rejection,
                        signal_id    = f"{self.symbol}_{htf_interval}_{ltf_interval}_{rejection.timestamp}",
                        profile      = profile,
                        session_tz   = self.cfg.session_tz,
                    )
                    if signal is None:
                        continue

                    cur_ltf_i = bisect.bisect_right(ltf_ts_idx, current_ts) - 1
                    future    = ltf_all[cur_ltf_i + 1:]
                    result    = self._simulate(signal, future)

                    seen_ltf.add(ltf_key)
                    dead_zones.add(zone_key)

                    expiry_ms = int(profile.signal_expiry_hours * 3_600_000)
                    open_dir[pair_dir_key] = result.close_ts or (signal.created_at + expiry_ms)

                    result.ltf_index  = ltf_i
                    result.htf_window = list(htf_w)
                    self.results.append(result)
                    self._print_result(result)

                    signal.chart_data = build_chart_data(
                        signal, ltf_all, htf_w,
                        htf_interval=htf_interval, ltf_interval=ltf_interval,
                    )
                    break

        print(f"\r  [{'█'*20}] 100.0%  signals={len(self.results)}\n")
        report = BacktestReport(self.symbol, self.results, self.cfg)
        report.print()
        return report

    def _simulate(self, signal: TradeSignal, future: list[Candle]) -> BacktestResult:
        is_short  = signal.direction == SignalDirection.SHORT
        profile   = self.profile
        use_be    = profile.use_breakeven
        use_inv   = profile.use_invalidation
        tp1_hit   = False
        inv_logged = False
        outcome   = SignalOutcome.EXPIRED
        close_ts: Optional[int]   = None
        close_px: Optional[float] = None
        expiry_ms = int(profile.signal_expiry_hours * 3_600_000)

        for c in future:
            if c.timestamp - signal.created_at > expiry_ms:
                break
            short_inv = is_short     and c.close > signal.ltf_range.range_high
            long_inv  = not is_short and c.close < signal.ltf_range.range_low

            if short_inv or long_inv:
                if not use_inv:
                    inv_logged = True
                else:
                    outcome  = SignalOutcome.BREAKEVEN if (tp1_hit and use_be) else SignalOutcome.LOSS
                    close_px = signal.entry_price if (tp1_hit and use_be) else c.close
                    close_ts = c.timestamp
                    break

            sl_hit = c.high >= signal.stop_loss if is_short else c.low <= signal.stop_loss
            if sl_hit:
                outcome  = SignalOutcome.BREAKEVEN if (tp1_hit and use_be) else SignalOutcome.LOSS
                close_px = signal.entry_price if (tp1_hit and use_be) else signal.stop_loss
                close_ts = c.timestamp
                break

            if not tp1_hit:
                if (c.low <= signal.tp1 if is_short else c.high >= signal.tp1):
                    tp1_hit = True

            if tp1_hit:
                if (c.low <= signal.tp2 if is_short else c.high >= signal.tp2):
                    outcome  = SignalOutcome.WIN_FULL
                    close_ts = c.timestamp
                    close_px = signal.tp2
                    break

        realized = {
            SignalOutcome.WIN_FULL:   signal.risk_reward_ratio,
            SignalOutcome.BREAKEVEN:  signal.risk_reward_ratio * profile.tp1_multiplier,
        }.get(outcome, None)
        if realized is None:
            if outcome == SignalOutcome.LOSS and close_px == signal.stop_loss:
                realized = -1.0
            elif outcome == SignalOutcome.LOSS and close_px is not None:
                realized = -(abs(signal.entry_price - close_px) / signal.risk_pips)
            else:
                realized = 0.0

        return BacktestResult(signal, outcome, realized, close_ts, close_px)

    def _print_result(self, r: BacktestResult) -> None:
        s      = r.signal
        is_s   = s.direction == SignalDirection.SHORT
        arrow  = DOWN if is_s else UP
        tf_tag = f"{DIM}[{s.htf_interval}/{s.ltf_interval}]{RESET}"
        outcome_str = {
            SignalOutcome.WIN_FULL:   f"{GREEN}WIN   +{r.realized_rr:.2f}R{RESET}",
            SignalOutcome.BREAKEVEN:  f"{YELLOW}BE    +{r.realized_rr:.2f}R{RESET}",
            SignalOutcome.LOSS:       f"{RED}LOSS  {r.realized_rr:.2f}R{RESET}",
            SignalOutcome.INVALIDATED: f"{DIM}VOID  0.0R{RESET}",
            SignalOutcome.EXPIRED:    f"{DIM}EXPD  0.0R{RESET}",
        }.get(r.outcome, f"{DIM}?{RESET}")
        print(f"\r{' '*80}\r", end="")
        print(
            f"  {arrow} {BOLD}{s.direction.value:5s}{RESET}  {tf_tag}  "
            f"{CYAN}{self.cfg.dt_ms(s.triggered_at)}{RESET}  "
            f"E={s.entry_price:.5f}  SL={s.stop_loss:.5f}  TP2={s.tp2:.5f}  "
            f"RR={s.risk_reward_ratio:.2f}  → {outcome_str}  "
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
                    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M:%S"):
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


def load_from_api(symbol: str, interval: str, outputsize: int, cfg: Settings) -> list[Candle]:
    from infrastructure.data_providers.market_data import MarketDataClient
    client = MarketDataClient(cfg.local_base_url)
    try:
        return client.fetch_candles(symbol, interval, outputsize, "ASC")
    finally:
        client.close()


def load_from_api_range(
    symbol: str, interval: str, start_ts: int,
    end_ts: Optional[int], cfg: Settings,
) -> list[Candle]:
    from infrastructure.data_providers.market_data import MarketDataClient
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
    p.add_argument("--min-rr",     type=float, default=None)
    p.add_argument("--max-rr",     type=float, default=None)
    p.add_argument("--max-wick",   type=float, default=None)
    p.add_argument("--stale-hours", type=float, default=None)
    p.add_argument("--max-sl-mult", type=float, default=None)
    p.add_argument("--no-breakeven",     action="store_true")
    p.add_argument("--no-invalidation",  action="store_true")
    p.add_argument("--no-trend-filter",  action="store_true")
    p.add_argument("--no-session-filter", action="store_true")
    p.add_argument("--from-date", dest="from_date", metavar="YYYY-MM-DD")
    p.add_argument("--to-date",   dest="to_date",   metavar="YYYY-MM-DD")
    p.add_argument("--verbose", action="store_true")
    p.add_argument("--cache-dir", default=None)
    args = p.parse_args()

    cfg = Settings.from_env()
    overrides: dict = {}
    if args.no_breakeven:     overrides["use_breakeven"]     = False
    if args.no_invalidation:  overrides["use_invalidation"]  = False
    if args.no_trend_filter:  overrides["use_trend_filter"]  = False
    if args.no_session_filter: overrides["use_session_filter"] = False
    if args.min_rr is not None: overrides["min_rr"] = args.min_rr
    if args.max_rr is not None: overrides["max_rr"] = args.max_rr
    if overrides:
        cfg = dc_replace(cfg, **overrides)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING,
                        format="%(levelname)s %(name)s: %(message)s")

    tf_pairs_to_run = [(args.tf_pair.split(":")[0].strip(), args.tf_pair.split(":")[1].strip())] \
        if args.tf_pair else list(cfg.tf_pairs)

    _date_fmt = "%Y-%m-%d"
    range_start_ts: Optional[int] = None
    range_end_ts:   Optional[int] = None
    if args.from_date:
        _dt = datetime.datetime.strptime(args.from_date, _date_fmt).replace(tzinfo=cfg.session_tz)
        range_start_ts = int(_dt.timestamp() * 1000)
    if args.to_date:
        _dt = datetime.datetime.strptime(args.to_date, _date_fmt).replace(
            tzinfo=cfg.session_tz, hour=23, minute=59, second=59)
        range_end_ts = int(_dt.timestamp() * 1000)

    if args.symbol:
        symbol      = args.symbol
        unique_htf  = list(dict.fromkeys(htf for htf, _ in tf_pairs_to_run))
        unique_ltf  = list(dict.fromkeys(ltf for _, ltf in tf_pairs_to_run))
        htf_cache: dict[str, list] = {}
        ltf_cache: dict[str, list] = {}

        for htf_tf in unique_htf:
            if range_start_ts is not None:
                candles = load_from_api_range(symbol, htf_tf, range_start_ts, range_end_ts, cfg)
            else:
                candles = load_from_api(symbol, htf_tf, cfg.htf_outputsize, cfg)
            if not candles:
                print(f"  ERROR: no HTF candles for {htf_tf}")
                raise SystemExit(1)
            htf_cache[htf_tf] = candles
            print(f"  HTF {htf_tf}: {len(candles)} bars  {cfg.dt_ms(candles[0].timestamp)} → {cfg.dt_ms(candles[-1].timestamp)}")

        earliest_ts = min(c[0].timestamp for c in htf_cache.values())
        for ltf_tf in unique_ltf:
            candles = load_from_api_range(symbol, ltf_tf, earliest_ts, range_end_ts, cfg)
            if not candles:
                print(f"  ERROR: no LTF candles for {ltf_tf}")
                raise SystemExit(1)
            ltf_cache[ltf_tf] = candles
            print(f"  LTF {ltf_tf}: {len(candles)} bars  {cfg.dt_ms(candles[0].timestamp)} → {cfg.dt_ms(candles[-1].timestamp)}")
    else:
        if not args.csv_ltf:
            p.error("--csv-ltf required with --csv-htf")
        symbol    = Path(args.csv_htf).stem
        htf_cache = {tf_pairs_to_run[0][0]: load_csv(args.csv_htf)}
        ltf_cache = {tf_pairs_to_run[0][1]: load_csv(args.csv_ltf)}

    bt     = MultiPairBacktester(cfg=cfg, symbol=symbol, pairs=tf_pairs_to_run,
                                  htf_candles=htf_cache, ltf_candles=ltf_cache,
                                  htf_lookback=args.htf_lookback)
    report = bt.run()

    if args.output:
        if args.output.endswith(".json"):
            report.save_json(args.output)
        else:
            report.save_csv(args.output)


if __name__ == "__main__":
    main()
