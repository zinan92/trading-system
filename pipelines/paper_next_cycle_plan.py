"""Cloud one-shot entrypoint for next-cycle Paper plan pre-generation."""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pipelines.dashboard_server import build_strategy_console_control_response
from services.config_loader import ROOT, load_pipeline_config
from services.dualtrack_config import dualtrack_config
from services.execution_plugin_composition import (
    build_configured_execution_engine_adapter,
)
from services.paper_next_cycle_plan import (
    NextCyclePlanError,
    NextCyclePlanPrecomputer,
)
from services.strategy_control_plane import StrategyControlPlane
from services.supervisor_execution_profile import (
    resolve_supervisor_execution_profile,
)


def run(
    *,
    output_root: Path | None = None,
    observed_at: str | None = None,
) -> dict[str, Any]:
    output = Path(
        output_root
        or os.getenv("TRADING_ORCHESTRATOR_OUTPUT_ROOT")
        or ROOT / str(load_pipeline_config().get("output_root", "outputs"))
    )
    config = dualtrack_config()
    execution = build_configured_execution_engine_adapter(
        output,
        config=config,
    )
    profile = resolve_supervisor_execution_profile(
        config,
        execution_name=str(getattr(execution, "name", "")),
    )
    convergence = dict(config.get("convergence") or {})
    precompute = dict(convergence.get("next_cycle_precompute") or {})
    lead_minutes = int(precompute.get("lead_minutes") or 60)
    provider_timeout_seconds = int(
        convergence.get("provider_timeout_seconds") or 25
    )
    if provider_timeout_seconds != 25:
        raise NextCyclePlanError("next_cycle_precompute_configuration_invalid")
    plane = StrategyControlPlane(output, execution_profile=profile)

    def candidate_builder(
        target_cycle_id: str,
        as_of: str,
    ) -> dict[str, Any]:
        return build_strategy_console_control_response(
            {
                "cycle_id": target_cycle_id,
                "as_of": as_of,
                "action": "refresh_recommendation",
            },
            output_root=output,
            actor={
                "type": "scheduler",
                "id": "paper-next-cycle-plan",
                "source": "paper_next_cycle_plan",
            },
            recommendation_timeout_seconds=provider_timeout_seconds,
            execution_profile=profile,
        )

    return NextCyclePlanPrecomputer(
        output,
        plane=plane,
        candidate_builder=candidate_builder,
        execution_profile=profile,
        lead_minutes=lead_minutes,
    ).run(
        observed_at=(
            observed_at
            or datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        )
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Pre-generate one verified next-cycle Paper plan."
    )
    parser.add_argument("--as-of")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    try:
        result = run(observed_at=args.as_of)
        exit_code = 0
    except Exception as exc:  # noqa: BLE001 - oneshot must emit one typed result.
        result = {
            "status": "blocked",
            "machine_code": str(getattr(exc, "code", str(exc))),
            "control_actions_executed": 0,
            "orders_created": 0,
            "plans_activated": 0,
            "prepared_starts_created": 0,
        }
        exit_code = 1
    if args.json:
        print(json.dumps(result, sort_keys=True, ensure_ascii=False))
    else:
        print(
            "paper_next_cycle_plan: "
            f"{result.get('status')} "
            f"target={result.get('target_cycle_id')}"
        )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
