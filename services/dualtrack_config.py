from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

from services.config_loader import ROOT, load_dualtrack_config as _load_dualtrack_config


DEFAULT_DUALTRACK_CONFIG: dict[str, Any] = {
    "capital_per_track_usd": 10_000,
    "max_leverage": 10,
    "cycle_hours_utc": {"day_start": 1, "night_start": 13},
    "market_data": {
        "symbol": "GOLD",
        "timeframe": "1m",
        "provider": "",
    },
    "market_session": {
        "enabled": False,
        "venue": "",
        "timezone": "UTC",
    },
    "human_fill_sync": {
        "enabled": False,
        "provider": "",
        "run_before_close": True,
        "refresh_order_sync_before_import": False,
        "require_success_before_close": True,
    },
    "plan_lock_deadline_min_before_cycle": 0,
    "grid": {
        "spacing_bp": 20.0,
        "range_k": 1.0,
        "tp_mult_base": 1.0,
        "tp_mult_trend": 2.0,
        "re_arm_max": 1,
        "trend_leg_budget_pct": 20.0,
    },
    "cost_per_side_bp": 0.5,
    "scoreboard": {"gate_threshold": 0.60, "window_cycles": 30},
    "census": {"reversal_bp": 10.0, "min_run_pct": 0.3},
    "weekly_target_usd": [1000, 1500],
}


def dualtrack_config(config_path: Path | None = None) -> dict[str, Any]:
    path = config_path or ROOT / "configs" / "dualtrack.yaml"
    try:
        loaded = _load_dualtrack_config(path)
    except FileNotFoundError:
        loaded = {}
    return _deep_merge(DEFAULT_DUALTRACK_CONFIG, loaded)


def track_notional_budget(config: dict[str, Any]) -> float:
    return float(config["capital_per_track_usd"]) * float(config["max_leverage"])


def base_rung_notional(config: dict[str, Any], *, max_rungs: int = 10) -> float:
    return track_notional_budget(config) / max(1, max_rungs)


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged
