"""Strategy Shadow orchestration over an explicit execution replay port."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

from services.accounting_projection_core import project_execution_accounting
from services.backtest_port import StrategyShadowReplayPort
from services.execution_conformance import (
    build_execution_scenario,
    candidate_receipt_blockers,
)
from services.journal_store import load_json, write_json
from services.strategy_plan_execution import build_plan_grid_entry_commands


STRATEGY_SHADOW_SCHEMA = "strategy-shadow-run-v2"


class StrategyShadowRunner:
    """Build immutable inputs and project engine facts; never match orders."""

    def __init__(
        self,
        output_root: Path,
        *,
        replay_port: StrategyShadowReplayPort,
        config: dict[str, Any],
        plugin_audit: dict[str, Any] | None = None,
    ) -> None:
        self.output_root = Path(output_root)
        self.replay_port = replay_port
        self.config = dict(config)
        self.plugin_audit = deepcopy(plugin_audit or {})

    def run(
        self,
        *,
        cycle_id: str,
        variant_id: str,
        plan: dict[str, Any],
        market_events: list[dict[str, Any]],
    ) -> dict[str, Any]:
        if str(plan.get("cycle_id") or "") != str(cycle_id):
            raise ValueError("Strategy Shadow plan cycle_id mismatch")
        commands = build_plan_grid_entry_commands(plan)
        scenario = build_execution_scenario(
            candidate_id=str(variant_id),
            plan=plan,
            commands=commands,
            market_events=market_events,
            config=self.config,
        )
        replay = self.replay_port.replay(scenario)
        if not isinstance(replay, dict):
            raise ValueError("Strategy Shadow replay port returned an invalid result")
        snapshot = replay.get("snapshot") if isinstance(replay.get("snapshot"), dict) else {}
        receipt = replay.get("receipt") if isinstance(replay.get("receipt"), dict) else {}
        receipt_issues = candidate_receipt_blockers(
            receipt,
            expected_scenario_id=str(scenario["scenario_id"]),
        )

        accounting: dict[str, Any] | None = None
        metrics = _blocked_metrics()
        if not receipt_issues:
            accounting = project_execution_accounting(
                snapshot,
                source_type="strategy_shadow_execution",
                scope={
                    "cycle_id": cycle_id,
                    "scenario_id": scenario["scenario_id"],
                },
            ).to_dict()
            if str((accounting.get("reconciliation") or {}).get("status") or "") != "pass":
                receipt_issues.append("accounting_reconciliation_not_pass")
            else:
                metrics = _metrics_from_accounting(accounting)

        status = "pass" if not receipt_issues else "blocked"
        payload = {
            "schema_version": STRATEGY_SHADOW_SCHEMA,
            "status": status,
            "blockers": sorted(set(receipt_issues)),
            "cycle_id": cycle_id,
            "variant_id": str(variant_id),
            "scenario_id": scenario["scenario_id"],
            "input_hash": scenario["hashes"]["input_hash"],
            "plan": scenario["plan"],
            "scenario": scenario,
            "execution_receipt": receipt,
            "execution_snapshot": snapshot,
            "accounting_snapshot": accounting,
            "orders": list((accounting or {}).get("orders") or []),
            "fills": list((accounting or {}).get("fills") or []),
            "positions": list((accounting or {}).get("positions") or []),
            "pnl": dict((accounting or {}).get("pnl") or {}),
            "metrics": metrics,
            "review": {
                "future_function": False,
                "available_at": scenario["plan_identity"]["available_at"],
                "evaluation_started_at": scenario["evaluation_window"]["started_at"],
                "evaluation_ended_at": scenario["evaluation_window"]["ended_at"],
                "market_event_count": scenario["evaluation_window"]["event_count"],
                "input_hash": scenario["hashes"]["input_hash"],
            },
            "safety": {
                "execution_shadow": False,
                "writes_production_ledger": False,
                "writes_authority_gate_evidence": False,
                "real_orders": False,
                "storage_namespace": str(replay.get("storage_namespace") or ""),
            },
        }
        if self.plugin_audit:
            payload["backtest_plugin"] = deepcopy(self.plugin_audit)
        _persist_candidate_receipt(self.output_root, scenario["scenario_id"], receipt)
        path = self.output_root / "dualtrack" / "strategy_shadows" / f"{cycle_id}_{variant_id}.json"
        rows = load_json(path)
        existing = next(
            (
                row
                for row in rows
                if isinstance(row, dict)
                and row.get("schema_version") == STRATEGY_SHADOW_SCHEMA
                and row.get("scenario_id") == payload["scenario_id"]
                and (row.get("execution_receipt") or {}).get("receipt_id")
                == receipt.get("receipt_id")
            ),
            None,
        )
        if existing is not None:
            return existing
        rows.append(payload)
        write_json(path, rows)
        return payload


def load_strategy_shadow_runs(output_root: Path, cycle_id: str) -> list[dict[str, Any]]:
    """Return the latest v1 or v2 row for each variant without rewriting trace."""

    folder = Path(output_root) / "dualtrack" / "strategy_shadows"
    if not folder.exists():
        return []
    result: list[dict[str, Any]] = []
    for path in sorted(folder.glob(f"{cycle_id}_*.json")):
        rows = load_json(path)
        if rows and isinstance(rows[-1], dict):
            result.append(dict(rows[-1]))
    return result


def _persist_candidate_receipt(
    output_root: Path,
    scenario_id: str,
    receipt: dict[str, Any],
) -> None:
    receipt_id = str(receipt.get("receipt_id") or "")
    if not receipt_id:
        return
    path = (
        Path(output_root)
        / "dualtrack"
        / "strategy_shadows"
        / "receipts"
        / f"{scenario_id}.json"
    )
    rows = load_json(path)
    if any(isinstance(row, dict) and row.get("receipt_id") == receipt_id for row in rows):
        return
    rows.append(dict(receipt))
    write_json(path, rows)


def _metrics_from_accounting(accounting: dict[str, Any]) -> dict[str, Any]:
    pnl = dict(accounting.get("pnl") or {})
    counts = dict(accounting.get("counts") or {})
    closed = [row for row in accounting.get("trades") or [] if row.get("status") == "closed"]
    realized_rows = [float(row.get("realized_pnl") or 0.0) for row in closed]
    equity = peak = 0.0
    max_drawdown = 0.0
    for value in realized_rows:
        equity += value
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, peak - equity)
    return {
        "net_pnl": pnl.get("net_pnl"),
        "realized_pnl": pnl.get("net_realized_pnl"),
        "unrealized_pnl": pnl.get("unrealized_pnl"),
        "max_drawdown": round(max_drawdown, 8),
        "win_rate": (
            sum(1 for value in realized_rows if value > 0) / len(realized_rows)
            if realized_rows
            else 0.0
        ),
        "average_r": None,
        "trade_count": int(counts.get("completed_trade_count") or 0),
        "cost": pnl.get("fees"),
    }


def _blocked_metrics() -> dict[str, Any]:
    return {
        "net_pnl": None,
        "realized_pnl": None,
        "unrealized_pnl": None,
        "max_drawdown": None,
        "win_rate": None,
        "average_r": None,
        "trade_count": None,
        "cost": None,
    }
