from __future__ import annotations

import sys
from pathlib import Path

from pipelines.dualtrack_nautilus_attended_cutover import build_attended_cutover_precheck
from services.dualtrack_execution_adapter import NAUTILUS_PAPER_GATE_OVERRIDE_ACKNOWLEDGEMENT
from services.dualtrack_config import dualtrack_config
from services.journal_store import write_json


def _ready_output(output: Path, cycle_id: str, config: dict) -> None:
    write_json(
        output / "dualtrack" / "nautilus" / "parity" / "current.json",
        [{"status": "pass", "blockers": []}],
    )
    for index in range(7):
        qualified_cycle = f"2026-07-{index + 1:02d}_DAY"
        write_json(
            output / "dualtrack" / "reconciliation" / f"{qualified_cycle}.json",
            [{
                "cycle_id": qualified_cycle,
                "status": "pass",
                "shadow_evidence": {
                    "qualification_contract_version": "dualtrack-shadow-qualification-v2",
                    "qualification_checks": {
                        "cycle_complete": True,
                        "cycle_close_artifact": True,
                    },
                    "qualifies_for_cutover": True,
                },
            }],
        )
    write_json(
        output / "dualtrack" / "nautilus" / "instrument_preflight.json",
        [{
            "status": "ready_for_paper_shadow",
            "fee_model": {
                **dict(config["paper_fee_model"]),
                "funding_rate": "0",
                "funding_time": 1,
            },
        }],
    )
    write_json(
        output / "dualtrack" / "strategy_control" / "runtime.json",
        [{
            "cycle_id": cycle_id,
            "desired_state": "stopped",
            "actual_state": "stopped",
            "accepted_order_count": 0,
        }],
    )


def test_attended_cutover_precheck_is_ready_only_when_gate_services_and_ledgers_are_safe(tmp_path: Path) -> None:
    output = tmp_path / "outputs"
    cycle_id = "2026-07-08_DAY"
    config = dualtrack_config()
    config["execution_engine"] = {
        "authoritative": "legacy_paper",
        "shadow": "nautilus_paper",
        "real_money_eligible": False,
    }
    _ready_output(output, cycle_id, config)

    result = build_attended_cutover_precheck(
        output,
        config=config,
        cycle_id=cycle_id,
        environ={
            "TRADING_ORCHESTRATOR_NAUTILUS_PAPER_SWITCH_APPROVED": "1",
            "TRADING_ORCHESTRATOR_NAUTILUS_PYTHON": sys.executable,
        },
    )

    assert result["status"] == "ready_for_operator_cutover"
    assert result["blockers"] == []
    assert result["legacy"]["reconciliation"]["status"] == "ok"
    assert result["nautilus_authoritative"]["status"] == "ok"
    assert result["configured_engine_changed"] is False
    assert result["config_write_performed"] is False
    assert result["orders_submitted"] is False
    assert result["real_money_eligible"] is False


def test_attended_cutover_precheck_blocks_running_robot_and_missing_service_approval(tmp_path: Path) -> None:
    output = tmp_path / "outputs"
    cycle_id = "2026-07-08_DAY"
    config = dualtrack_config()
    config["execution_engine"] = {
        "authoritative": "legacy_paper",
        "shadow": "nautilus_paper",
        "real_money_eligible": False,
    }
    _ready_output(output, cycle_id, config)
    write_json(
        output / "dualtrack" / "strategy_control" / "runtime.json",
        [{"cycle_id": cycle_id, "desired_state": "running", "actual_state": "running"}],
    )
    write_json(
        output / "dualtrack" / "orders" / f"{cycle_id}_human.json",
        [{
            "order_id": "legacy-open-order",
            "state": "accepted",
            "side": "buy",
            "event": "entry",
            "order_type": "limit",
            "price": 100.0,
            "quantity": 1.0,
        }],
    )

    result = build_attended_cutover_precheck(
        output,
        config=config,
        cycle_id=cycle_id,
        environ={},
    )

    codes = {row["code"] for row in result["blockers"]}
    assert result["status"] == "blocked"
    assert "production_runtime_not_stopped" in codes
    assert "legacy_orders_not_flat" in codes
    assert "attended_service_approval_missing" in codes
    assert "isolated_nautilus_runtime_missing" in codes


def test_attended_cutover_can_override_only_shadow_history_when_fixed_fixture_is_green(tmp_path: Path) -> None:
    output = tmp_path / "outputs"
    cycle_id = "2026-07-08_DAY"
    config = dualtrack_config()
    config["execution_engine"] = {
        "authoritative": "legacy_paper",
        "shadow": "nautilus_paper",
        "real_money_eligible": False,
    }
    _ready_output(output, cycle_id, config)
    # Keep the deterministic parity fixture green, but remove historical 7-cycle evidence.
    for path in (output / "dualtrack" / "reconciliation").glob("*.json"):
        path.unlink()

    result = build_attended_cutover_precheck(
        output,
        config=config,
        cycle_id=cycle_id,
        allow_shadow_gate_override=True,
        environ={
            "TRADING_ORCHESTRATOR_NAUTILUS_PAPER_SWITCH_APPROVED": "1",
            "TRADING_ORCHESTRATOR_NAUTILUS_PYTHON": sys.executable,
            "TRADING_ORCHESTRATOR_NAUTILUS_PAPER_GATE_OVERRIDE": NAUTILUS_PAPER_GATE_OVERRIDE_ACKNOWLEDGEMENT,
        },
    )

    assert result["status"] == "ready_for_operator_cutover"
    assert result["shadow_gate_override"] == {
        "requested": True,
        "used": True,
        "fixed_fixture_status": "pass",
        "original_gate_status": "blocked",
    }


def test_attended_cutover_override_still_blocks_when_fixed_fixture_is_not_green(tmp_path: Path) -> None:
    output = tmp_path / "outputs"
    cycle_id = "2026-07-08_DAY"
    config = dualtrack_config()
    config["execution_engine"] = {
        "authoritative": "legacy_paper",
        "shadow": "nautilus_paper",
        "real_money_eligible": False,
    }
    _ready_output(output, cycle_id, config)
    write_json(output / "dualtrack" / "nautilus" / "parity" / "current.json", [{"status": "drift"}])
    for path in (output / "dualtrack" / "reconciliation").glob("*.json"):
        path.unlink()

    result = build_attended_cutover_precheck(
        output,
        config=config,
        cycle_id=cycle_id,
        allow_shadow_gate_override=True,
        environ={
            "TRADING_ORCHESTRATOR_NAUTILUS_PAPER_SWITCH_APPROVED": "1",
            "TRADING_ORCHESTRATOR_NAUTILUS_PYTHON": sys.executable,
            "TRADING_ORCHESTRATOR_NAUTILUS_PAPER_GATE_OVERRIDE": NAUTILUS_PAPER_GATE_OVERRIDE_ACKNOWLEDGEMENT,
        },
    )

    assert result["status"] == "blocked"
    assert "shadow_gate_not_ready" in {row["code"] for row in result["blockers"]}
