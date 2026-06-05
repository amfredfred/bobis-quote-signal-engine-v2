from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from config.settings import Settings
from domain.assets.profiles import AssetRegistry
from infrastructure.observability.metrics import MetricsCollector
from infrastructure.data_providers.market_data import _parse_rates


def test_settings_loads_yaml_config(tmp_path, monkeypatch):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        """
features:
  entry_model: crt
timeframes:
  pairs:
    - htf: 1h
      ltf: 5min
""",
        encoding="utf-8",
    )

    monkeypatch.setenv("APEX_CONFIG", str(cfg))
    monkeypatch.setenv("APEX_ENV", "paper")

    settings = Settings.from_env()

    assert settings.entry_model == "crt"
    assert settings.tf_pairs == (("1h", "5min"),)
    assert settings.breakeven_spread_multiplier == 1.5
    assert settings.breakeven_max_buffer_pct_of_risk == 10.0


def test_settings_allows_equal_timeframe_pairs():
    settings = Settings(tf_pairs=(("1h", "1h"),))

    assert settings.tf_pairs == (("1h", "1h"),)


def test_settings_rejects_breakeven_multiplier_below_one() -> None:
    with pytest.raises(ValueError, match="must be 0 or >= 1"):
        Settings(breakeven_spread_multiplier=0.5)


def test_asset_profile_applies_timeframe_trade_management_overrides():
    settings = Settings(
        tp1_trigger_pct=10.0,
        tp1_close_pct=0.0,
        trade_management_tf_overrides={
            "1/1": {"tp1_trigger_pct": 5.0},
            "30/30": {
                "tp1_trigger_pct": 7.5,
                "tp1_close_pct": 25.0,
                "breakeven_spread_multiplier": 2.0,
                "breakeven_max_buffer_pct_of_risk": 15.0,
            },
        },
    )

    registry = AssetRegistry(settings)

    assert registry.get("XAUUSD", "1min", "1min").tp1_trigger_pct == 5.0
    thirty = registry.get("XAUUSD", "30min", "30min")
    assert thirty.tp1_trigger_pct == 7.5
    assert thirty.tp1_close_pct == 25.0
    assert thirty.breakeven_spread_multiplier == 2.0
    assert thirty.breakeven_max_buffer_pct_of_risk == 15.0
    assert registry.get("XAUUSD", "5min", "5min").tp1_trigger_pct == 10.0


def test_metrics_active_zones_are_timeframe_aware_and_pruned(tmp_path):
    metrics = MetricsCollector(Settings(base_dir=tmp_path))
    old_ts = 1_700_000_000_000
    object.__setattr__(metrics._cfg, "now_ms", lambda: old_ts)

    base_zone = {
        "symbol": "XAUUSD",
        "direction": "LONG",
        "ltfTimestamp": old_ts,
        "pendingAt": old_ts,
        "htfRange": {},
        "ltfRange": {},
    }
    metrics.upsert_active_zone({**base_zone, "htfInterval": "5min", "ltfInterval": "5min"})
    metrics.upsert_active_zone({**base_zone, "htfInterval": "15min", "ltfInterval": "15min"})

    assert metrics.gauge("signals.active_zones") == 2.0

    object.__setattr__(metrics._cfg, "now_ms", lambda: old_ts + (25 * 3_600_000))
    metrics.build_snapshot()

    assert metrics.gauge("signals.active_zones") == 0.0


def test_settings_rejects_inverted_timeframe_pairs():
    with pytest.raises(ValueError, match="must be at least as large"):
        Settings(tf_pairs=(("5min", "1h"),))


def test_settings_formats_timestamps_in_broker_time():
    settings = Settings(broker_time_offset_ms=10_800_000)

    assert settings.dt_ms(1_700_000_000_000) == "2023-11-15 01:13:20"


def test_parse_rates_normalizes_broker_epoch_seconds_to_utc():
    rates = [
        {
            "time": 1_700_010_800,
            "open": 1.1,
            "high": 1.2,
            "low": 1.0,
            "close": 1.15,
            "tick_volume": 12,
        }
    ]

    candles = _parse_rates(rates, broker_time_offset_ms=10_800_000)

    assert candles[0].timestamp == 1_700_000_000_000
    assert candles[0].volume == 12.0
