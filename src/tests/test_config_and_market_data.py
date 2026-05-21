from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from config.settings import Settings
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


def test_parse_rates_keeps_mt5_utc_epoch_seconds():
    rates = [
        {
            "time": 1_700_000_000,
            "open": 1.1,
            "high": 1.2,
            "low": 1.0,
            "close": 1.15,
            "tick_volume": 12,
        }
    ]

    candles = _parse_rates(rates)

    assert candles[0].timestamp == 1_700_000_000_000
    assert candles[0].volume == 12.0
