from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

from services.config_loader import ROOT, load_dualtrack_config as _load_dualtrack_config


DEFAULT_DUALTRACK_CONFIG: dict[str, Any] = {
    "capital_per_track_usd": 10_000,
    "max_leverage": 10,
    "cycle_hours_utc": {"day_start": 1, "night_start": 13},
    "cycle_cadence": {
        "hours": 12,
        "day_start_hour_cst": 9,
        "night_start_hour_cst": 21,
        "accounting_day_hours": 24,
        "restored_at_cst": "2026-07-14T21:00:00+08:00",
    },
    "market_data": {
        "symbol": "GOLD",
        "timeframe": "1m",
        "provider": "binance_usdm_futures",
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
    "backtest_plugins": {
        "strategy_shadow": "nautilus_strategy_shadow",
    },
    "cycle_decision": {
        "enabled": False,
        "provider_timeout_seconds": 30,
        "terminal_deadline_seconds": 300,
    },
    "convergence": {
        "next_cycle_precompute": {
            "lead_minutes": 60,
            "timer_interval_seconds": 300,
            "provider_timeout_seconds": 60,
        },
    },
    "plan_lock_deadline_min_before_cycle": 0,
    "machine_planner": {
        "plugin": "codex_newsletter",
        "command": "codex",
        "model": "gpt-5.4",
        "timeout_seconds": 240,
        "newsletter_root": "inputs/newsletters",
        "volatility_lookback_cycles": 10,
        "minimum_active_cycle_samples": 3,
        "minimum_sample_coverage_pct": 0.8,
        "minimum_range_multiplier": 1.0,
        "exclude_weekends_from_range_reference": True,
        "range_reassessment": {
            "enabled": True,
            "confirmation_timeframe": "1m",
            "confirm_closes": 3,
            "cooldown_minutes": 60,
            "failure_retry_minutes": 5,
            "max_replans_per_cycle": 2,
            "minimum_remaining_minutes": 30,
        },
    },
    "grid": {
        "spacing_bp": 20.0,
        "range_k": 1.0,
        "tp_mult_base": 1.0,
        "tp_mult_trend": 2.0,
        "re_arm_max": 1,
        "trend_leg_budget_pct": 20.0,
    },
    "strategy_grid": {
        "range_timeframe": "1d",
        "range_atr_period": 14,
        "spacing_timeframe": "4h",
        "spacing_atr_period": 14,
        "execution_timeframe": "1m",
        "min_grid_count": 30,
        "max_grid_count": 70,
        "cost_spacing_multiple": 5.0,
        "default_mode": "arithmetic",
        "capital_utilization_cap": 1.0,
        "required_leverage": 10.0,
        "min_net_profit_per_grid_usd": 10.0,
        "styles": {
            "steady": {
                "range_atr_multiple": 2.0,
                "spacing_atr_multiple": 0.25,
            },
            "aggressive": {
                "range_atr_multiple": 1.0,
                "spacing_atr_multiple": 0.125,
            },
        },
    },
    "cost_per_side_bp": 0.5,
    "execution_contract": {
        "schema_version": "dualtrack-execution-contract-v1",
        "execution_instrument_id": "XAUUSDT",
        "price_precision": 2,
        "price_increment": "0.01",
        "quantity_precision": 3,
        "quantity_increment": "0.001",
    },
    "execution_shadow": {
        "nautilus": {
            "instrument_endpoint": "http://127.0.0.1:8100/api/instruments/commodity/XAUUSDT",
            "instrument_source": "binance_usdm_futures",
            # Chart/storage symbol and venue execution symbol are intentionally
            # distinct. The shadow runner must record this mapping explicitly.
            "source_symbol": "GOLD",
            "execution_instrument_id": "XAUUSDT",
            "fee_model": {
                "mode": "paper_assumption",
                "maker_fee_rate": "0.00005",
                "taker_fee_rate": "0.00005",
                "source": "dualtrack.cost_per_side_bp",
                "real_money_eligible": False,
            },
        },
    },
    "scoreboard": {"gate_threshold": 0.60, "window_cycles": 30},
    "review": {
        "directional_efficiency_neutral_threshold": 0.35,
        "max_changes_per_cycle": 1,
        "minimum_promotion_cycles": 10,
        "minimum_promotion_trades": 30,
        "preferred_promotion_trades": 100,
        "auto_promote": False,
    },
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
