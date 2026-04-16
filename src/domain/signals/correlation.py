"""
domain/signals/correlation.py — cross-asset currency exposure conflict detector.

Purpose
───────
Prevents the signal engine from emitting a new signal whose currency-leg
exposure directly contradicts an already-active signal.

Classic example the user cited:
  Buying  EUR/USD  →  long EUR, short USD
  Selling EUR/JPY  →  short EUR, long JPY
  Net EUR exposure: +1 and -1 simultaneously — the legs cancel.
  This engine catches that before the signal is emitted.

What "conflict" means here
──────────────────────────
A conflict is a *direct opposition on a shared currency leg*:

  LONG  EUR/USD  {EUR: +1, USD: -1}
  LONG  USD/JPY  {USD: +1, JPY: -1}
                       ↑
                 USD appears on opposite sides → conflict

What is intentionally NOT flagged
──────────────────────────────────
  • Correlated same-direction positions (GBP/USD + EUR/USD both long →
    both anti-USD, which is correlation risk but not a contradiction).
  • Overlapping exposure that amplifies rather than cancels.

Those are position-sizing concerns, not signal validity concerns.
If you want to surface them, extend score_correlation() below.

Architecture
────────────
  CURRENCY_EXPOSURE  — static map: canonical symbol → {currency: ±1 base vector}
  exposure_vector()  — applies the signal direction to flip signs as needed
  correlation_conflict() — hard gate: returns (True, reason) on first conflict
  score_correlation()    — soft gate: returns a 0.0–1.0 penalty score
                           (0.0 = no issue, 1.0 = full conflict)
                           Use this for scoring mode instead of blocking.

Integration point
─────────────────
  Call correlation_conflict() (or score_correlation()) inside
  SessionCoordinator.should_emit() before registering a new signal,
  passing self._watchlist.values() as active_signals.

  Example (hard block):
    conflicted, reason = correlation_conflict(
        symbol, direction, list(signal_service.get_active_signals())
    )
    if conflicted:
        logger.info("[%s] Correlation block: %s", symbol, reason)
        return False, reason

  Example (scoring / surfacing):
    penalty = score_correlation(
        symbol, direction, list(signal_service.get_active_signals())
    )
    signal.correlation_penalty = penalty   # attach to TradeSignal for WS payload

XAU/USD and US500
─────────────────
  Treated as independent assets with no currency-leg decomposition.
  A missing symbol maps to an empty exposure vector, so it never triggers
  a conflict — consistent with their role as macro instruments.
"""

from __future__ import annotations

from domain.assets.profiles import normalize_symbol
from domain.entities.enums import SignalDirection
from domain.entities.trade import TradeSignal


# ── Currency exposure map ─────────────────────────────────────────────────────
# Base vectors for a LONG position on each pair.
# SHORT positions are handled by exposure_vector() by flipping all signs.
#
# Convention: +1 = you are BUYING this currency
#             -1 = you are SELLING this currency
#
# XAU/USD and US500 are deliberately absent — they have no forex leg to
# conflict with. Missing entries return {} which never triggers a conflict.

CURRENCY_EXPOSURE: dict[str, dict[str, int]] = {
    "EUR/USD": {"EUR": +1, "USD": -1},
    "GBP/USD": {"GBP": +1, "USD": -1},
    "USD/JPY": {"USD": +1, "JPY": -1},
    "USD/CHF": {"USD": +1, "CHF": -1},
    "AUD/USD": {"AUD": +1, "USD": -1},
    "USD/CAD": {"USD": +1, "CAD": -1},
    "NZD/USD": {"NZD": +1, "USD": -1},
    "EUR/JPY": {"EUR": +1, "JPY": -1},
    "GBP/JPY": {"GBP": +1, "JPY": -1},
    "AUD/JPY": {"AUD": +1, "JPY": -1},
    "GBP/CHF": {"GBP": +1, "CHF": -1},
    "EUR/GBP": {"EUR": +1, "GBP": -1},
    "EUR/CHF": {"EUR": +1, "CHF": -1},
    "EUR/CAD": {"EUR": +1, "CAD": -1},
    "GBP/CAD": {"GBP": +1, "CAD": -1},
}


# ── Core helpers ──────────────────────────────────────────────────────────────


def exposure_vector(symbol: str, direction: SignalDirection) -> dict[str, int]:
    """
    Return the signed currency exposure for a signal.

    For a LONG signal the base vector is returned as-is.
    For a SHORT signal every value is negated — selling the base currency
    and buying the quote currency.

    Args:
        symbol:    Any normalizable symbol string ("EURUSD", "EUR/USD", …).
        direction: SignalDirection.LONG or SignalDirection.SHORT.

    Returns:
        Dict mapping currency code → +1 (buying) or -1 (selling).
        Returns {} for symbols not in CURRENCY_EXPOSURE (XAU/USD, US500, …).

    Examples:
        >>> exposure_vector("EUR/USD", SignalDirection.LONG)
        {"EUR": 1, "USD": -1}

        >>> exposure_vector("EUR/USD", SignalDirection.SHORT)
        {"EUR": -1, "USD": 1}

        >>> exposure_vector("XAU/USD", SignalDirection.LONG)
        {}
    """
    canonical = normalize_symbol(symbol)
    base = CURRENCY_EXPOSURE.get(canonical, {})
    if not base:
        return {}

    multiplier = +1 if direction == SignalDirection.LONG else -1
    return {ccy: value * multiplier for ccy, value in base.items()}


# ── Hard gate ─────────────────────────────────────────────────────────────────


def correlation_conflict(
    candidate_symbol: str,
    candidate_direction: SignalDirection,
    active_signals: list[TradeSignal],
) -> tuple[bool, str]:
    """
    Detect a direct currency-leg conflict between a candidate signal and all
    currently active signals.

    A conflict exists when the *same* currency appears on *opposite* sides
    simultaneously across two signals — meaning one trade buys what another
    sells, producing net-zero exposure on that currency while still paying
    full spread and risk on both legs.

    Args:
        candidate_symbol:    Symbol of the incoming signal.
        candidate_direction: Direction of the incoming signal.
        active_signals:      All currently open/active TradeSignal objects
                             (typically signal_service.get_active_signals()).

    Returns:
        (False, "")              — no conflict, signal may proceed.
        (True, "<reason string>") — conflict detected; reason names the
                                   offending currency and both signals.

    Notes:
        • Returns on the first conflict found. If you need all conflicts,
          collect them in a list rather than returning early.
        • Symbols absent from CURRENCY_EXPOSURE (XAU/USD, US500) return {}
          from exposure_vector() and will never trigger a conflict.
        • Same-direction same-currency positions (e.g. two USD-short trades)
          are NOT flagged — they are correlated risk, not contradictions.
    """
    candidate_exp = exposure_vector(candidate_symbol, candidate_direction)
    if not candidate_exp:
        # Independent asset — no forex legs to conflict with.
        return False, ""

    for signal in active_signals:
        active_exp = exposure_vector(signal.symbol, signal.direction)
        if not active_exp:
            continue

        for currency, candidate_side in candidate_exp.items():
            active_side = active_exp.get(currency)
            if active_side is not None and active_side != candidate_side:
                reason = (
                    f"{candidate_symbol} {candidate_direction.value} conflicts with "
                    f"active {signal.symbol} {signal.direction.value} "
                    f"— opposing {currency} exposure "
                    f"({'buying' if candidate_side > 0 else 'selling'} vs "
                    f"{'buying' if active_side > 0 else 'selling'})"
                )
                return True, reason

    return False, ""


# ── Soft gate (scoring mode) ──────────────────────────────────────────────────


def score_correlation(
    candidate_symbol: str,
    candidate_direction: SignalDirection,
    active_signals: list[TradeSignal],
) -> float:
    """
    Return a 0.0–1.0 correlation penalty score for a candidate signal.

    Unlike correlation_conflict(), this does not hard-block. It quantifies
    how much opposing currency exposure the candidate would introduce relative
    to the total active exposure, so the caller can surface the score in the
    WebSocket payload and let the consumer decide.

    Score interpretation:
        0.0  — no shared currency legs with any active signal at all.
        >0.0 — at least one currency leg overlaps.
        1.0  — every shared currency leg is fully opposed (complete conflict).

    Formula:
        conflicting_legs / total_shared_legs

    Args:
        candidate_symbol:    Symbol of the incoming signal.
        candidate_direction: Direction of the incoming signal.
        active_signals:      All currently open/active TradeSignal objects.

    Returns:
        Float in [0.0, 1.0]. Attach to TradeSignal as correlation_penalty
        and include in the WS payload for the client to display.

    Example usage in signal_service._analyze_pair():
        penalty = score_correlation(symbol, ltf_range.direction, self.get_active_signals())
        signal = build_signal(…)
        signal.correlation_penalty = penalty   # requires field on TradeSignal
        # Never hard-block — emit and let the client render a warning badge.
    """
    candidate_exp = exposure_vector(candidate_symbol, candidate_direction)
    if not candidate_exp:
        return 0.0

    shared_legs = 0
    conflicting_legs = 0

    for signal in active_signals:
        active_exp = exposure_vector(signal.symbol, signal.direction)
        if not active_exp:
            continue

        for currency, candidate_side in candidate_exp.items():
            active_side = active_exp.get(currency)
            if active_side is not None:
                shared_legs += 1
                if active_side != candidate_side:
                    conflicting_legs += 1

    if shared_legs == 0:
        return 0.0

    return conflicting_legs / shared_legs
