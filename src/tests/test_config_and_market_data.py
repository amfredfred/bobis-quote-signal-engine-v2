from __future__ import annotations

import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from config.settings import Settings
from domain.assets.profiles import AssetRegistry
from infrastructure.observability.metrics import MetricsCollector
from infrastructure.data_providers.market_data import _parse_rates
from infrastructure.data_providers.market_data import MarketDataClient, MarketDataError
import infrastructure.data_providers.market_data as market_data


def test_settings_loads_yaml_config(tmp_path, monkeypatch):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        """
features:
  entry_model: crt
crt:
  mode: previous_candle
signal_quality:
  stop_model: wick
  max_spread_to_sl_ratio: 0.4
timeframes:
  pairs:
    - htf: 1h
      ltf: 5min
      entry_model: crt
""",
        encoding="utf-8",
    )

    monkeypatch.setenv("APEX_CONFIG", str(cfg))
    monkeypatch.setenv("APEX_ENV", "paper")

    settings = Settings.from_env()

    assert settings.entry_model == "crt"
    assert settings.crt_mode == "previous_candle"
    assert settings.stop_placement_method == "wick"
    assert settings.max_spread_to_sl_ratio == 0.4
    assert settings.tf_pairs == (("1h", "5min"),)
    assert settings.entry_model_for("1h", "5min") == "crt"
    assert settings.entry_model_for("30min", "5min") == "crt"


def test_settings_allows_equal_timeframe_pairs():
    settings = Settings(tf_pairs=(("1h", "1h"),))

    assert settings.tf_pairs == (("1h", "1h"),)


def test_settings_rejects_invalid_pair_entry_model():
    with pytest.raises(ValueError, match="timeframes.pairs entry_model"):
        Settings(tf_entry_models={"5/5": "invalid"})


def test_settings_rejects_invalid_crt_mode():
    with pytest.raises(ValueError, match="crt.mode"):
        Settings(crt_mode="invalid")


def test_settings_rejects_invalid_stop_model():
    with pytest.raises(ValueError, match="signal_quality.stop_model"):
        Settings(stop_placement_method="invalid")


def test_settings_rejects_zone_signal_count_below_one():
    with pytest.raises(ValueError, match="zones.max_signal_count"):
        Settings(max_signal_count_per_zone=0)


def test_settings_rejects_negative_spread_to_sl_ratio():
    with pytest.raises(ValueError, match="signal_quality.max_spread_to_sl_ratio"):
        Settings(max_spread_to_sl_ratio=-0.01)


def test_asset_profile_applies_per_symbol_tf_trade_management_overrides():
    settings = Settings(
        tp1_trigger_pct=10.0,
        tp1_close_pct=0.0,
        trade_management_tf_overrides={
            "XAUUSD": {
                "1/1": {
                    "tp1_trigger_pct": 5.0,
                },
                "30/30": {
                    "tp1_trigger_pct": 7.5,
                    "tp1_close_pct": 25.0,
                },
            },
        },
    )

    registry = AssetRegistry(settings)

    # Exact symbol + exact TF
    assert registry.get("XAUUSD", "1min", "1min").tp1_trigger_pct == 5.0
    thirty = registry.get("XAUUSD", "30min", "30min")
    assert thirty.tp1_trigger_pct == 7.5
    assert thirty.tp1_close_pct == 25.0
    # Symbol present but no matching TF → global default
    assert registry.get("XAUUSD", "5min", "5min").tp1_trigger_pct == 10.0
    # Different symbol → global default
    assert registry.get("US100", "1min", "1min").tp1_trigger_pct == 10.0


def test_asset_profile_tf_overrides_wildcard_tf_fallback():
    settings = Settings(
        tp1_trigger_pct=10.0,
        tp1_close_pct=0.0,
        trade_management_tf_overrides={
            "XAUUSD": {
                "1/1": {"tp1_trigger_pct": 5.0},
                "*":   {"tp1_trigger_pct": 8.0},  # wildcard TF
            },
        },
    )
    registry = AssetRegistry(settings)

    # Exact TF wins
    assert registry.get("XAUUSD", "1min", "1min").tp1_trigger_pct == 5.0
    # Other TF falls back to "*"
    assert registry.get("XAUUSD", "5min", "5min").tp1_trigger_pct == 8.0
    # Different symbol → global default
    assert registry.get("US100", "5min", "5min").tp1_trigger_pct == 10.0


def test_asset_profile_tf_overrides_wildcard_symbol_fallback():
    settings = Settings(
        tp1_trigger_pct=10.0,
        tp1_close_pct=0.0,
        trade_management_tf_overrides={
            "XAUUSD": {"1/1": {"tp1_trigger_pct": 5.0}},
            "*":       {"1/1": {"tp1_trigger_pct": 7.0}},  # wildcard symbol
        },
    )
    registry = AssetRegistry(settings)

    # Exact symbol+TF
    assert registry.get("XAUUSD", "1min", "1min").tp1_trigger_pct == 5.0
    # Unknown symbol falls back to "*"
    assert registry.get("US100", "1min", "1min").tp1_trigger_pct == 7.0
    # Unknown symbol, unmatched TF → global default
    assert registry.get("US100", "5min", "5min").tp1_trigger_pct == 10.0


def test_symbol_rr_filter_symbol_plus_tf_wins():
    settings = Settings(
        min_rr=1.5,
        max_rr=9.0,
        symbol_rr_filter={
            "XAUUSD": {"60/5": {"min_rr": 2.0, "max_rr": 7.0}},
            "*": {"60/5": {"min_rr": 1.8, "max_rr": 5.0}},
        },
    )
    registry = AssetRegistry(settings)

    profile = registry.get("XAUUSD", "1h", "5min")
    assert profile.min_rr == 2.0
    assert profile.max_rr == 7.0


def test_symbol_rr_filter_symbol_wildcard_tf_fallback():
    settings = Settings(
        min_rr=1.5,
        max_rr=9.0,
        symbol_rr_filter={
            "XAUUSD": {"*": {"min_rr": 2.5, "max_rr": 6.0}},
        },
    )
    registry = AssetRegistry(settings)

    # TF not listed → falls to symbol+*
    profile = registry.get("XAUUSD", "4h", "5min")
    assert profile.min_rr == 2.5
    assert profile.max_rr == 6.0


def test_symbol_rr_filter_global_wildcard_applies_to_other_symbols():
    settings = Settings(
        min_rr=1.5,
        max_rr=9.0,
        symbol_rr_filter={
            "XAUUSD": {"*": {"max_rr": 6.0}},
            "*": {"*": {"min_rr": 2.0}},
        },
    )
    registry = AssetRegistry(settings)

    # US100 has no specific entry → *+* applies for min_rr
    # max_rr unchanged: INDICES class override gives 8.0, *+* didn't set max_rr
    profile = registry.get("US100", "1h", "5min")
    assert profile.min_rr == 2.0
    assert profile.max_rr == 8.0


def test_symbol_rr_filter_independent_resolution_per_value():
    # min_rr comes from symbol+*, max_rr comes from *+TF — independent lookup
    settings = Settings(
        min_rr=1.5,
        max_rr=9.0,
        symbol_rr_filter={
            "XAUUSD": {"*": {"min_rr": 3.0}},        # only min_rr
            "*": {"60/5": {"max_rr": 4.0}},           # only max_rr
        },
    )
    registry = AssetRegistry(settings)

    profile = registry.get("XAUUSD", "1h", "5min")
    assert profile.min_rr == 3.0   # from XAUUSD+*
    assert profile.max_rr == 4.0   # from *+60/5


def test_symbol_rr_filter_no_tf_falls_back_to_symbol_wildcard():
    settings = Settings(
        min_rr=1.5,
        max_rr=9.0,
        symbol_rr_filter={
            "XAUUSD": {
                "60/5": {"min_rr": 2.0},
                "*": {"min_rr": 1.8},
            },
        },
    )
    registry = AssetRegistry(settings)

    # Different TF — not 60/5, so falls to symbol+*
    profile = registry.get("XAUUSD", "4h", "15min")
    assert profile.min_rr == 1.8


def test_symbol_rr_filter_parsed_from_yaml(tmp_path, monkeypatch):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        """
signal_quality:
  min_rr: 1.5
  max_rr: 9.0
  rrr:
    XAUUSD:
      "60/5":
        min_rr: 2.0
        max_rr: 7.5
      "*":
        min_rr: 1.8
    "*":
      "*":
        max_rr: 5.0
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("APEX_CONFIG", str(cfg))
    monkeypatch.setenv("APEX_ENV", "paper")

    settings = Settings.from_env()

    assert settings.symbol_rr_filter["XAUUSD"]["60/5"]["min_rr"] == 2.0
    assert settings.symbol_rr_filter["XAUUSD"]["60/5"]["max_rr"] == 7.5
    assert settings.symbol_rr_filter["XAUUSD"]["*"]["min_rr"] == 1.8
    assert settings.symbol_rr_filter["*"]["*"]["max_rr"] == 5.0


def test_symbol_rr_filter_allows_max_rr_below_min_rr():
    # max_rr is a TP cap (adjusts tp2), not a filter — so max_rr < min_rr is valid:
    # require >= 3R natural setup, cap TP at 2R.
    settings = Settings(
        symbol_rr_filter={"XAUUSD": {"*": {"min_rr": 3.0, "max_rr": 2.0}}}
    )
    assert settings.symbol_rr_filter["XAUUSD"]["*"]["max_rr"] == 2.0


def test_symbol_rr_filter_rejects_zero_min_rr():
    with pytest.raises(ValueError, match="min_rr must be > 0"):
        Settings(
            symbol_rr_filter={"XAUUSD": {"*": {"min_rr": 0.0}}}
        )


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


def _market_data_client_without_mt5_init() -> MarketDataClient:
    client = object.__new__(MarketDataClient)
    client._resolved_symbols = {}
    return client


def test_market_data_resolves_broker_symbol_suffix_and_caches(monkeypatch):
    symbol_info_calls = []
    selected = []
    infos = {
        "XAUUSD.": SimpleNamespace(name="XAUUSD.", visible=False),
    }
    fake_mt5 = SimpleNamespace(
        symbol_info=lambda name: symbol_info_calls.append(name) or infos.get(name),
        symbols_get=lambda: [
            SimpleNamespace(name="XAUUSD_x100"),
            SimpleNamespace(name="XAUUSD."),
        ],
        symbol_select=lambda name, visible: selected.append((name, visible)) or True,
    )
    monkeypatch.setattr(market_data, "mt5", fake_mt5)
    client = _market_data_client_without_mt5_init()

    assert client._ensure_symbol("XAU/USD") == "XAUUSD."
    assert client._ensure_symbol("XAUUSD") == "XAUUSD."
    assert selected == [("XAUUSD.", True)]
    assert symbol_info_calls == ["XAUUSD", "XAUUSD."]


def test_market_data_prefers_exact_broker_symbol(monkeypatch):
    fake_mt5 = SimpleNamespace(
        symbol_info=lambda name: (
            SimpleNamespace(name="XAUUSD", visible=True) if name == "XAUUSD" else None
        ),
        symbols_get=lambda: pytest.fail("symbols_get should not run for exact match"),
        symbol_select=lambda *_: pytest.fail("visible exact symbol should not be selected"),
    )
    monkeypatch.setattr(market_data, "mt5", fake_mt5)
    client = _market_data_client_without_mt5_init()

    assert client._ensure_symbol("xau-usd") == "XAUUSD"


def test_market_data_missing_symbol_lists_related_candidates(monkeypatch):
    fake_mt5 = SimpleNamespace(
        symbol_info=lambda _name: None,
        symbols_get=lambda: [
            SimpleNamespace(name="XAUJPY."),
            SimpleNamespace(name="EURUSD."),
        ],
    )
    monkeypatch.setattr(market_data, "mt5", fake_mt5)
    client = _market_data_client_without_mt5_init()

    with pytest.raises(MarketDataError, match="related broker symbols.*XAUJPY"):
        client._ensure_symbol("XAUUSD")
