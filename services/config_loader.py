from __future__ import annotations

import json
import os
from copy import deepcopy
from pathlib import Path
from urllib.parse import urlsplit

from schemas.asset import Asset


ROOT = Path(__file__).resolve().parents[1]


def load_json_yaml(path: Path) -> dict:
    """Load JSON-compatible YAML without adding a runtime dependency."""
    return json.loads(path.read_text(encoding="utf-8"))


def load_assets(config_path: Path | None = None) -> list[Asset]:
    config_path = config_path or ROOT / "configs" / "assets.yaml"
    raw = load_json_yaml(config_path)
    return [Asset.from_dict(item) for item in raw["assets"] if item.get("enabled", True)]


def load_risk_rules(config_path: Path | None = None) -> dict:
    config_path = config_path or ROOT / "configs" / "risk_rules.yaml"
    return load_json_yaml(config_path)


def load_pipeline_config(config_path: Path | None = None) -> dict:
    config_path = config_path or ROOT / "configs" / "pipeline.yaml"
    config = load_json_yaml(config_path)
    datafeed_url = str(os.getenv("TRADING_ORCHESTRATOR_DATAFEED_URL") or "").strip()
    if datafeed_url:
        _require_loopback_http_url(datafeed_url, "TRADING_ORCHESTRATOR_DATAFEED_URL")
        config = deepcopy(config)
        config.setdefault("datafeed", {})["base_url"] = datafeed_url.rstrip("/")
    return config


def load_strategy_config(config_path: Path | None = None) -> dict:
    config_path = config_path or ROOT / "configs" / "strategy.yaml"
    return load_json_yaml(config_path)


def load_dualtrack_config(config_path: Path | None = None) -> dict:
    config_path = config_path or ROOT / "configs" / "dualtrack.yaml"
    config = load_json_yaml(config_path)
    datafeed_url = str(os.getenv("TRADING_ORCHESTRATOR_DATAFEED_URL") or "").strip()
    nautilus_python = str(os.getenv("TRADING_ORCHESTRATOR_NAUTILUS_PYTHON") or "").strip()
    if datafeed_url or nautilus_python:
        config = deepcopy(config)
    if datafeed_url:
        _require_loopback_http_url(datafeed_url, "TRADING_ORCHESTRATOR_DATAFEED_URL")
        shadow = config.setdefault("execution_shadow", {}).setdefault("nautilus", {})
        shadow["instrument_endpoint"] = (
            f"{datafeed_url.rstrip('/')}/api/instruments/commodity/XAUUSDT"
        )
    if nautilus_python:
        config.setdefault("execution_engine", {})["shadow_runtime_path"] = nautilus_python
    return config


def _require_loopback_http_url(value: str, name: str) -> None:
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or parsed.hostname not in {
        "127.0.0.1",
        "localhost",
        "::1",
    }:
        raise ValueError(f"{name} must use a loopback HTTP URL")
