from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)


def _load_yaml(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _get(d: dict, dotpath: str, default=None):
    for key in dotpath.split("."):
        if not isinstance(d, dict):
            return default
        d = d.get(key, default)
    return d


@dataclass(frozen=True)
class Settings:
    # Broker sources — one in-process pipeline engine per entry.
    sources:              tuple = ()

    # signal-engine project location (resolved to absolute path at load time).
    signal_engine_path:   str  = ""
    signal_engine_config: str  = "config.yaml"

    # External WS server that the NestJS gateway connects to.
    gateway_host:         str  = "0.0.0.0"
    gateway_port:         int  = 8765
    gateway_secret:       str  = ""

    consensus_window_ms:  int  = 0 #60_000
    symbols:              tuple = ("XAUUSD", "XAGUSD")
    log_level:            str  = "INFO"

    @classmethod
    def from_yaml(cls, path: Path | None = None) -> "Settings":
        # Locate config file.
        _env_cfg = os.getenv("MANAGER_CONFIG")
        candidates = [
            path,
            Path(_env_cfg) if _env_cfg else None,
            Path.cwd() / "config.yaml",
        ]
        cfg: dict = {}
        cfg_file: Path | None = None
        for p in candidates:
            if p and p.exists():
                cfg = _load_yaml(p)
                cfg_file = p
                logger.info("Loaded config from %s", p)
                break

        raw_sources = _get(cfg, "sources") or []
        raw_symbols = _get(cfg, "symbols") or ["XAUUSD","XAGUSD"]

        # Resolve signal_engine path relative to the config file's directory.
        engine_path_raw = str(_get(cfg, "signal_engine.path", "") or "")
        if engine_path_raw:
            base = cfg_file.parent if cfg_file else Path.cwd()
            engine_path = str((base / engine_path_raw).resolve())
        else:
            engine_path = ""

        engine_config = str(_get(cfg, "signal_engine.config", "config.yaml") or "config.yaml")

        return cls(
            sources=tuple(str(s) for s in raw_sources),
            signal_engine_path=engine_path,
            signal_engine_config=engine_config,
            gateway_host=str(_get(cfg, "gateway_server.host", "0.0.0.0")),
            gateway_port=int(_get(cfg, "gateway_server.port", 8765)),
            gateway_secret=str(_get(cfg, "gateway_server.secret", "") or ""),
            consensus_window_ms=int(_get(cfg, "consensus.window_ms", 0)),
            symbols=tuple(str(s).upper() for s in raw_symbols),
            log_level=str(_get(cfg, "logging.level", "INFO")).upper(),
        )
