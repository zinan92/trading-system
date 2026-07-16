"""Read-only precheck for the attended Nautilus paper-engine cutover.

This command never edits config, restarts a service, submits an order, or
changes either ledger. It turns the M4 evidence gate and the current flat-state
requirements into one operator-facing Go/No-Go artifact.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from pipelines.dualtrack_shadow_cutover_status import build_cutover_status
from services.config_loader import ROOT, load_pipeline_config
from services.dualtrack_clock import cycle_window
from services.dualtrack_config import dualtrack_config
from services.dualtrack_execution_adapter import LegacyPaperExecutionAdapter
from services.dualtrack_nautilus_execution_adapter import NautilusExecutionAdapter
from services.journal_store import load_json, write_json
from services.strategy_control_plane import StrategyControlPlane


def build_attended_cutover_precheck(
    output_root: Path,
    *,
    config: dict[str, Any] | None = None,
    environ: dict[str, str] | None = None,
    cycle_id: str | None = None,
) -> dict[str, Any]:
    output = Path(output_root)
    cfg = dict(config or dualtrack_config())
    environment = dict(os.environ if environ is None else environ)
    selected_cycle = str(cycle_id or cycle_window().cycle_id)
    gate = build_cutover_status(output)
    runtime = StrategyControlPlane(output).runtime_state(selected_cycle)
    legacy = LegacyPaperExecutionAdapter(output, config=cfg)
    legacy_snapshot = legacy.snapshot(selected_cycle)
    legacy_reconciliation = legacy.reconcile(selected_cycle)
    accepted_orders = [
        row for row in legacy_snapshot.get("orders") or []
        if str(row.get("state") or "").lower() == "accepted"
    ]
    open_positions = [
        row for row in legacy_snapshot.get("positions") or []
        if str(row.get("status") or "").lower() == "open"
    ]
    settings = dict(cfg.get("execution_engine") or {})
    approved = environment.get("TRADING_ORCHESTRATOR_NAUTILUS_PAPER_SWITCH_APPROVED") == "1"
    runtime_value = str(environment.get("TRADING_ORCHESTRATOR_NAUTILUS_PYTHON") or "").strip()
    runtime_path = Path(runtime_value) if runtime_value else None
    preflight_path = output / "dualtrack" / "nautilus" / "instrument_preflight.json"
    preflight_rows = load_json(preflight_path)
    preflight = preflight_rows[-1] if preflight_rows and isinstance(preflight_rows[-1], dict) else {}

    blockers: list[dict[str, Any]] = []
    _require(blockers, settings.get("authoritative") == "legacy_paper", "legacy_not_authoritative")
    _require(blockers, settings.get("real_money_eligible") is not True, "real_money_mode_forbidden")
    _require(blockers, gate.get("status") == "ready_for_attended_paper_switch", "shadow_gate_not_ready", gate)
    _require(
        blockers,
        runtime.get("desired_state") == "stopped" and runtime.get("actual_state") == "stopped",
        "production_runtime_not_stopped",
        {"desired_state": runtime.get("desired_state"), "actual_state": runtime.get("actual_state")},
    )
    _require(blockers, not accepted_orders, "legacy_orders_not_flat", {"accepted_order_count": len(accepted_orders)})
    _require(blockers, not open_positions, "legacy_positions_not_flat", {"open_position_count": len(open_positions)})
    _require(
        blockers,
        legacy_reconciliation.get("status") == "ok",
        "legacy_reconciliation_not_ok",
        legacy_reconciliation,
    )
    _require(blockers, approved, "attended_service_approval_missing")
    _require(blockers, runtime_path is not None and runtime_path.exists(), "isolated_nautilus_runtime_missing")
    _require(
        blockers,
        preflight.get("status") == "ready_for_paper_shadow",
        "nautilus_preflight_not_ready",
        {"status": preflight.get("status"), "blockers": preflight.get("blockers")},
    )

    candidate_state: dict[str, Any] = {"status": "not_checked"}
    if approved and runtime_path is not None and runtime_path.exists() and preflight.get("status") == "ready_for_paper_shadow":
        try:
            candidate = NautilusExecutionAdapter(
                output,
                nautilus_python=runtime_path,
                storage_namespace="nautilus_authoritative",
                config=cfg,
            )
            snapshot = candidate.snapshot(selected_cycle)
            candidate_reconciliation = candidate.reconcile(selected_cycle)
            candidate_accepted = sum(
                str(row.get("state") or "").lower() == "accepted"
                for row in snapshot.get("orders") or []
            )
            candidate_open = sum(
                str(row.get("status") or "").lower() == "open"
                for row in snapshot.get("positions") or []
            )
            candidate_state = {
                "status": "ok" if candidate_reconciliation.get("status") == "ok" else "drift",
                "accepted_order_count": candidate_accepted,
                "open_position_count": candidate_open,
                "reconciliation": candidate_reconciliation,
            }
            _require(blockers, candidate_accepted == 0, "nautilus_orders_not_flat", candidate_state)
            _require(blockers, candidate_open == 0, "nautilus_positions_not_flat", candidate_state)
            _require(
                blockers,
                candidate_reconciliation.get("status") == "ok",
                "nautilus_reconciliation_not_ok",
                candidate_reconciliation,
            )
        except Exception as exc:
            candidate_state = {"status": "blocked", "error": str(exc)}
            blockers.append({"code": "nautilus_runtime_validation_failed", "detail": candidate_state})

    ready = not blockers
    return {
        "schema_version": "dualtrack-nautilus-attended-cutover-precheck-v1",
        "scope": "paper_only",
        "cycle_id": selected_cycle,
        "status": "ready_for_operator_cutover" if ready else "blocked",
        "blockers": blockers,
        "shadow_gate": gate,
        "runtime": {
            "desired_state": runtime.get("desired_state"),
            "actual_state": runtime.get("actual_state"),
        },
        "legacy": {
            "accepted_order_count": len(accepted_orders),
            "open_position_count": len(open_positions),
            "reconciliation": legacy_reconciliation,
        },
        "nautilus_authoritative": candidate_state,
        "attended_service_approval": approved,
        "isolated_runtime_path": runtime_value,
        "configured_engine_changed": False,
        "config_write_performed": False,
        "orders_submitted": False,
        "real_money_eligible": False,
        "rollback_boundary": {
            "required": True,
            "restore_authoritative": "legacy_paper",
            "preserve_namespaces": ["legacy", "nautilus_paper", "nautilus_authoritative"],
        },
    }


def _require(
    blockers: list[dict[str, Any]],
    condition: bool,
    code: str,
    detail: dict[str, Any] | None = None,
) -> None:
    if not condition:
        blockers.append({"code": code, "detail": detail or {}})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Evaluate the attended Nautilus paper cutover precheck.")
    parser.add_argument("--output-root", default="")
    parser.add_argument("--cycle-id", default="")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    output = Path(args.output_root) if args.output_root else ROOT / str(load_pipeline_config().get("output_root", "outputs"))
    result = build_attended_cutover_precheck(output, cycle_id=args.cycle_id or None)
    write_json(output / "dualtrack" / "cutover" / "attended_precheck_current.json", [result])
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        codes = ",".join(str(row.get("code") or "") for row in result["blockers"]) or "none"
        print(f"dualtrack_nautilus_attended_cutover: status={result['status']} blockers={codes}")
    return 0 if result["status"] == "ready_for_operator_cutover" else 2


if __name__ == "__main__":
    raise SystemExit(main())
