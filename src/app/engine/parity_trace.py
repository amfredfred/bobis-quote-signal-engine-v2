"""JSONL parity trace records shared by live and backtest."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from domain.entities.enums import SignalOutcome
from domain.entities.trade import TradeSignal


def config_hash(config: Any) -> str:
    raw = repr(config)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class ParityTraceRecord:
    mode: str
    symbol: str
    timeframe: str
    timestamp: str
    spread_pct: float
    strategy: str
    signal: str
    entry: float | None
    stop_loss: float | None
    take_profit: float | None
    rr: float | None
    risk_amount: float | None
    position_size: float | None
    decision_reason: str
    blocked_reason: str | None
    account_balance: float | None
    daily_budget: float | None
    risk_per_trade: float | None
    config_hash: str
    trade_outcome: str | None = None
    pnl: float | None = None

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))


def _iso_ms(ts_ms: int) -> str:
    return (
        dt.datetime.fromtimestamp(ts_ms / 1000, tz=dt.timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


def trace_from_signal(
    *,
    mode: str,
    signal: TradeSignal,
    cfg: Any,
    decision_reason: str = "valid_signal",
    blocked_reason: str | None = None,
    spread_pct: float = 0.0,
    account_balance: float | None = None,
    risk_percent: float | None = None,
    outcome: SignalOutcome | None = None,
    pnl: float | None = None,
) -> ParityTraceRecord:
    risk_amount = (
        account_balance * (risk_percent / 100.0)
        if account_balance is not None and risk_percent is not None
        else None
    )
    return ParityTraceRecord(
        mode=mode,
        symbol=signal.symbol,
        timeframe=signal.ltf_interval,
        timestamp=_iso_ms(signal.triggered_at or signal.created_at),
        spread_pct=spread_pct,
        strategy="shared_decision_engine",
        signal=signal.direction.value,
        entry=signal.entry_price,
        stop_loss=signal.stop_loss,
        take_profit=signal.tp2,
        rr=signal.risk_reward_ratio,
        risk_amount=risk_amount,
        position_size=None,
        decision_reason=decision_reason,
        blocked_reason=blocked_reason,
        account_balance=account_balance,
        daily_budget=risk_amount,
        risk_per_trade=risk_amount,
        config_hash=config_hash(cfg),
        trade_outcome=(outcome.value if outcome else (signal.outcome.value if signal.outcome else None)),
        pnl=pnl,
    )


class ParityTraceWriter:
    def __init__(self, path: str | Path | None) -> None:
        self.path = Path(path) if path else None
        self._fh = None

    def __enter__(self) -> "ParityTraceWriter":
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._fh = self.path.open("w", encoding="utf-8")
        return self

    def write(self, record: ParityTraceRecord) -> None:
        if self._fh:
            self._fh.write(record.to_json() + "\n")

    def __exit__(self, *_exc: object) -> None:
        if self._fh:
            self._fh.close()
