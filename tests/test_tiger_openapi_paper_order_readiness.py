from __future__ import annotations

from pathlib import Path

from services.journal_store import load_json, write_json
from services.tiger_openapi_paper_order_readiness import TigerOpenApiPaperOrderReadiness


def _write_ready_artifacts(root: Path) -> None:
    write_json(
        root / "tiger_reconciliation" / "current.json",
        [
            {
                "confirmation_status": "confirmed_flat",
                "system_state": "READY",
                "reason_code": "confirmed_flat",
                "drift_count": 0,
                "can_open_new_orders": True,
                "exchange_positions": [],
                "exchange_open_orders": [],
                "checked_at": "2026-07-05T11:16:11+00:00",
            }
        ],
    )
    write_json(
        root / "tiger_order_sync" / "current.json",
        [
            {
                "sync_status": "synced",
                "error": "",
                "open_order_count": 0,
                "filled_order_count": 0,
                "exchange_open_orders": [],
                "exchange_filled_orders": [],
                "checked_at": "2026-07-05T11:17:14+00:00",
            }
        ],
    )
    write_json(
        root / "tiger_kill_switch" / "current.json",
        [
            {
                "status": "dry_run",
                "network_order_created": False,
                "network_cancel_created": False,
                "network_actions": {"enabled": False, "ready": False},
                "checked_at": "2026-07-05T11:16:11+00:00",
            }
        ],
    )
    write_json(
        root / "tiger_contracts" / "current.json",
        [
            {
                "resolution": {
                    "requested_symbol": "MGCmain",
                    "execution_symbol": "MGC2608",
                    "status": "ready",
                    "ready": True,
                    "days_to_contract_month": 27,
                    "block_reason": "",
                },
                "checked_at": "2026-07-05T11:18:00+00:00",
            }
        ],
    )
    write_json(
        root / "tiger_account_sync" / "current.json",
        [
            {
                "sync_status": "synced",
                "error": "",
                "account_observation": {
                    "account_observed": True,
                    "balance_present": True,
                    "accounting_observed": True,
                    "segment_key": "C",
                },
                "exchange_balance": {"asset": "USD", "balance": 25000.0, "available": 24000.0, "balance_present": True},
                "exchange_accounting": {"net_realized_pnl_estimate": 0.0, "unrealized_pnl_estimate": 0.0},
                "checked_at": "2026-07-05T11:18:30+00:00",
            }
        ],
    )
    write_json(
        root / "tiger_paper_order_drill" / "current.json",
        [
            {
                "status": "pass",
                "run_id": "drilltest",
                "mode": "local_fake_tradeclient",
                "real_tiger_network_call_attempted": False,
                "scenarios": [{"name": "default_guardrail_block", "status": "pass"}, {"name": "simulated_green_order", "status": "pass"}],
                "finished_at": "2026-07-05T11:19:00+00:00",
            }
        ],
    )


def test_tiger_paper_order_readiness_reports_ready_from_artifacts(tmp_path: Path):
    root = tmp_path / "outputs"
    _write_ready_artifacts(root)

    result = TigerOpenApiPaperOrderReadiness(root).run("2026-07-05")

    assert result["status"] == "ready_for_attended_paper_order"
    assert result["ready_for_attended_paper_order"] is True
    assert result["can_submit_without_explicit_operator_authorization"] is False
    assert result["real_tiger_network_call_attempted"] is False
    assert result["blockers"] == []
    assert {item["name"] for item in result["checks"]} == {
        "venue_status",
        "dated_contract",
        "reconciliation_flat",
        "account_sync",
        "order_sync",
        "kill_switch_clear",
        "paper_order_drill",
        "checked_in_profile_safe",
        "operational_invariants",
    }
    assert result["required_runtime_overrides_for_attended_canary"]["network_order_submission"] == "paper_tradeclient"
    assert result["operator_authorization_required"]["required"] is True
    assert load_json(root / "tiger_paper_order_readiness" / "current.json")[-1]["status"] == "ready_for_attended_paper_order"
    assert (root / "tiger_paper_order_readiness" / "2026-07-05.md").exists()


def test_tiger_paper_order_readiness_blocks_when_drill_is_missing(tmp_path: Path):
    root = tmp_path / "outputs"
    _write_ready_artifacts(root)
    (root / "tiger_paper_order_drill" / "current.json").unlink()

    result = TigerOpenApiPaperOrderReadiness(root).run("2026-07-05")

    assert result["status"] == "blocked"
    assert result["ready_for_attended_paper_order"] is False
    blockers = {item["name"]: item for item in result["blockers"]}
    assert "paper_order_drill" in blockers
    assert "missing" in str(blockers["paper_order_drill"]["evidence"])


def test_tiger_paper_order_readiness_blocks_stale_order_evidence(tmp_path: Path):
    root = tmp_path / "outputs"
    _write_ready_artifacts(root)

    result = TigerOpenApiPaperOrderReadiness(root).run("2026-07-06")

    assert result["status"] == "blocked"
    assert result["ready_for_attended_paper_order"] is False
    blockers = {item["name"]: item for item in result["blockers"]}
    assert {
        "dated_contract",
        "reconciliation_flat",
        "account_sync",
        "order_sync",
        "kill_switch_clear",
        "paper_order_drill",
    }.issubset(blockers)
    assert blockers["order_sync"]["evidence"]["required_run_date"] == "2026-07-06"
    assert blockers["order_sync"]["evidence"]["current_for_run_date"] is False
    assert blockers["reconciliation_flat"]["evidence"]["checked_at"].startswith("2026-07-05")


def test_tiger_paper_order_readiness_refresh_runbook_writes_artifact_only_package(tmp_path: Path):
    root = tmp_path / "outputs"
    _write_ready_artifacts(root)
    service = TigerOpenApiPaperOrderReadiness(root)
    readiness = service.run("2026-07-06")

    result = service.refresh_runbook("2026-07-06")

    assert readiness["status"] == "blocked"
    assert result["schema_version"] == "tiger-paper-order-readiness-refresh-runbook-v1"
    assert result["status"] == "ready_for_operator_refresh"
    assert result["readiness_status"] == "blocked"
    assert result["blocker_count"] == 6
    assert result["stale_evidence_count"] == 6
    assert result["blocker_names"] == [
        "dated_contract",
        "reconciliation_flat",
        "account_sync",
        "order_sync",
        "kill_switch_clear",
        "paper_order_drill",
    ]
    assert [row["name"] for row in result["command_sequence"]] == [
        "refresh_contract_status",
        "refresh_reconciliation",
        "refresh_account_sync",
        "refresh_order_sync",
        "refresh_local_drill",
        "refresh_readiness",
    ]
    assert result["generation_safety"]["opens_trade_client"] is False
    assert result["generation_safety"]["submits_orders"] is False
    assert result["generation_safety"]["writes_runtime_config"] is False
    assert result["command_sequence_safety"]["opens_trade_client_read_only"] is True
    assert result["command_sequence_safety"]["submits_orders"] is False
    assert result["command_sequence_safety"]["writes_runtime_config"] is False
    assert load_json(root / "tiger_paper_order_readiness" / "refresh_runbook_current.json")[-1]["status"] == "ready_for_operator_refresh"
    markdown = (root / "tiger_paper_order_readiness" / "refresh_runbook_current.md").read_text(encoding="utf-8")
    assert "Tiger Paper-Order Evidence Refresh Runbook" in markdown
    assert "opens_trade_client_read_only: `True`" in markdown
    assert "submits_orders: `False`" in markdown


def test_tiger_paper_order_readiness_blocks_if_checked_in_profile_is_not_fail_closed(monkeypatch, tmp_path: Path):
    from services import tiger_openapi_paper_order_readiness as readiness_module

    root = tmp_path / "outputs"
    _write_ready_artifacts(root)
    unsafe_profile = {
        "provider": "tiger_openapi",
        "environment": "paper",
        "dry_run": False,
        "network_order_submission": "paper_tradeclient",
        "confirm_tiger_paper_orders": True,
        "require_reconciliation_before_entry": True,
        "require_live_money_guardrails_before_entry": True,
        "require_attached_protection_before_entry": True,
        "enable_tiger_kill_switch_network_actions": False,
    }
    monkeypatch.setattr(
        readiness_module,
        "load_pipeline_config",
        lambda: {"output_root": str(root), "broker_profiles": {"tiger_openapi_paper": unsafe_profile}},
    )

    result = TigerOpenApiPaperOrderReadiness(root).run("2026-07-05")

    assert result["status"] == "blocked"
    blockers = {item["name"]: item for item in result["blockers"]}
    assert "checked_in_profile_safe" in blockers
    assert blockers["checked_in_profile_safe"]["evidence"]["dry_run"] is False
