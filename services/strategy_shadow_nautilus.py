"""Nautilus implementation of the Strategy Shadow replay port."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from services.dualtrack_nautilus_execution_adapter import NautilusExecutionAdapter, ReplayExecutor
from services.execution_conformance import (
    EXECUTION_SCENARIO_SCHEMA,
    build_candidate_execution_receipt,
)


class NautilusStrategyShadowReplay:
    """Replay one content-addressed candidate in a non-authoritative namespace."""

    def __init__(
        self,
        output_root: Path,
        *,
        nautilus_python: str | Path,
        preflight_path: str | Path,
        config: dict[str, Any],
        replay_executor: ReplayExecutor | None = None,
    ) -> None:
        self.output_root = Path(output_root)
        self.nautilus_python = Path(nautilus_python)
        self.preflight_path = Path(preflight_path)
        self.config = dict(config)
        self.replay_executor = replay_executor

    def replay(self, scenario: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(scenario, dict) or scenario.get("schema_version") != EXECUTION_SCENARIO_SCHEMA:
            raise ValueError("Nautilus Strategy Shadow requires an execution scenario")
        scenario_id = str(scenario.get("scenario_id") or "")
        cycle_id = str(scenario.get("cycle_id") or "")
        if not scenario_id or not cycle_id:
            raise ValueError("Nautilus Strategy Shadow scenario identity is missing")
        namespace = f"strategy_shadow_{scenario_id.rsplit('-', 1)[-1][:20]}"
        adapter = NautilusExecutionAdapter(
            self.output_root,
            nautilus_python=self.nautilus_python,
            storage_namespace=namespace,
            preflight_path=self.preflight_path,
            replay_executor=self.replay_executor,
            defer_replay=True,
            config=self.config,
        )
        for command in scenario.get("commands") or []:
            adapter.submit_order(dict(command))
        for event in scenario.get("market_events") or []:
            adapter.process_market_event(dict(event))
        adapter.flush(cycle_id)
        snapshot = adapter.snapshot(cycle_id)
        reconciliation = adapter.reconcile(cycle_id)
        receipt = build_candidate_execution_receipt(
            scenario=scenario,
            snapshot=snapshot,
            reconciliation=reconciliation,
            storage_namespace=namespace,
        )
        return {
            "storage_namespace": namespace,
            "snapshot": snapshot,
            "reconciliation": reconciliation,
            "receipt": receipt,
        }
