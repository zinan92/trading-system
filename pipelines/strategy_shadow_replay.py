"""Composition root for one isolated Nautilus Strategy Shadow replay."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from services.backtest_plugin_composition import compose_strategy_shadow_backtest
from services.backtest_plugin_registry import BacktestPluginRegistry
from services.config_loader import ROOT, load_pipeline_config
from services.dualtrack_config import dualtrack_config
from services.dualtrack_nautilus_execution_adapter import ReplayExecutor
from services.journal_store import load_json
from services.strategy_shadow import StrategyShadowRunner


def run_strategy_shadow_replay(
    *,
    output_root: Path,
    cycle_id: str,
    variant_id: str,
    plan: dict[str, Any],
    market_events: list[dict[str, Any]],
    nautilus_python: str | Path,
    preflight_path: str | Path,
    config: dict[str, Any] | None = None,
    replay_executor: ReplayExecutor | None = None,
    backtest_plugin_registry: BacktestPluginRegistry | None = None,
) -> dict[str, Any]:
    settings = dict(config or dualtrack_config())
    runtime = compose_strategy_shadow_backtest(
        settings,
        output_root=output_root,
        nautilus_python=nautilus_python,
        preflight_path=preflight_path,
        replay_executor=replay_executor,
        registry=backtest_plugin_registry,
    )
    return StrategyShadowRunner(
        output_root,
        replay_port=runtime.port,
        config=settings,
        plugin_audit=runtime.audit_dict(),
    ).run(
        cycle_id=cycle_id,
        variant_id=variant_id,
        plan=plan,
        market_events=market_events,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Replay a StrategyPlan in isolated Nautilus Shadow storage.")
    parser.add_argument("--cycle-id", required=True)
    parser.add_argument("--variant-id", required=True)
    parser.add_argument("--plan-path", required=True)
    parser.add_argument("--events-path", required=True)
    parser.add_argument("--nautilus-python", required=True)
    parser.add_argument("--preflight-path", required=True)
    parser.add_argument("--output-root", default="")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    output_root = (
        Path(args.output_root)
        if args.output_root
        else ROOT / str(load_pipeline_config().get("output_root", "outputs"))
    )
    plans = load_json(Path(args.plan_path))
    events = load_json(Path(args.events_path))
    if not plans or not isinstance(plans[-1], dict):
        raise ValueError("Strategy Shadow plan artifact is empty")
    if not events or any(not isinstance(row, dict) for row in events):
        raise ValueError("Strategy Shadow market event artifact is empty or invalid")
    result = run_strategy_shadow_replay(
        output_root=output_root,
        cycle_id=args.cycle_id,
        variant_id=args.variant_id,
        plan=dict(plans[-1]),
        market_events=[dict(row) for row in events],
        nautilus_python=args.nautilus_python,
        preflight_path=args.preflight_path,
    )
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(
            "strategy_shadow_replay: "
            f"status={result['status']} scenario_id={result['scenario_id']}"
        )
    return 0 if result["status"] == "pass" else 2


if __name__ == "__main__":
    raise SystemExit(main())
