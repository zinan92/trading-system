from __future__ import annotations

import json
from pathlib import Path

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
    return load_json_yaml(config_path)


def load_strategy_config(config_path: Path | None = None) -> dict:
    config_path = config_path or ROOT / "configs" / "strategy.yaml"
    return load_json_yaml(config_path)
