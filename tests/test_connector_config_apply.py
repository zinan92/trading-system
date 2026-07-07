from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from schemas.market_data import Bar
from services.config_loader import load_json_yaml
from services.connector_activation_plan import ConnectorActivationPlan
from services.connector_config_apply import ConnectorConfigApply, ROLLBACK_ACKNOWLEDGEMENT
from services.journal_store import load_json, write_json
from services.market_store import MarketStore
from tests.test_connector_activation_plan import _config, _dualtrack_config


def _write_config(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _activation_plan(tmp_path: Path, *, with_switch_support: bool = True) -> tuple[dict, Path, Path, Path]:
    output_root = tmp_path / "outputs"
    props = tmp_path / "tiger_openapi_config.properties"
    props.write_text("placeholder only\n", encoding="utf-8")
    props.chmod(0o600)
    os.environ["TIGER_OPENAPI_CONFIG_PATH"] = str(props)
    write_json(
        output_root / "tiger_price_feed_readiness" / "current.json",
        [{"status": "ready_for_price_feed", "ready_for_price_feed": True, "blockers": []}],
    )
    pipeline_config = _config()
    dualtrack_config = _dualtrack_config()
    pipeline_path = tmp_path / "configs" / "pipeline.yaml"
    dualtrack_path = tmp_path / "configs" / "dualtrack.yaml"
    _write_config(pipeline_path, pipeline_config)
    _write_config(dualtrack_path, dualtrack_config)
    plan = ConnectorActivationPlan(
        output_root,
        config=pipeline_config,
        dualtrack_runtime_config=dualtrack_config,
    ).evaluate(
        {
            "connector_id": "tiger_openapi",
            "requested_roles": ["price_feed", "broker_order"],
            "environment": "paper",
            "credential_confirmations": {
                "TIGER_OPENAPI_CONFIG_PATH": {"present": True, "file_exists": True, "owner_only": True}
            },
        },
    )
    if with_switch_support:
        _write_tiger_price_feed_ready_artifacts(output_root)
        _write_tiger_market_coverage(tmp_path)
    return plan, output_root, pipeline_path, dualtrack_path


def _write_tiger_price_feed_ready_artifacts(output_root: Path, *, checked_at: str | None = None) -> None:
    timestamp = checked_at or datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    write_json(
        output_root / "tiger_price_feed_acceptance" / "current.json",
        [{"status": "accepted", "ready_for_price_feed": True, "checked_at": timestamp}],
    )
    write_json(
        output_root / "tiger_price_feed_readiness" / "current.json",
        [{"status": "ready_for_price_feed", "ready_for_price_feed": True, "checked_at": timestamp}],
    )
    write_json(
        output_root / "tiger_realtime_validation" / "current.json",
        [{"status": "pass", "checked_at": timestamp}],
    )
    write_json(
        output_root / "tiger_futures_feed" / "current.json",
        [{"status": "pass", "ready": True, "checked_at": timestamp}],
    )


def _write_tiger_market_coverage(tmp_path: Path) -> None:
    db_path = tmp_path / "data" / "market_data.db"
    store = MarketStore(db_path)
    start = datetime(2026, 7, 6, 0, 0, tzinfo=timezone.utc)
    bars = []
    for index in range(500):
        close = 4100.0 + index * 0.1
        bars.append(
            Bar(
                symbol="MGCmain",
                timeframe="1m",
                timestamp=(start + timedelta(minutes=index)).isoformat(),
                open=close - 0.1,
                high=close + 0.2,
                low=close - 0.2,
                close=close,
                volume=1.0,
                provider="tiger_openapi:COMEX",
            )
        )
    store.upsert_bars(bars)


def test_connector_config_apply_dry_run_does_not_write_configs(tmp_path: Path) -> None:
    plan, output_root, pipeline_path, dualtrack_path = _activation_plan(tmp_path)
    before_pipeline = pipeline_path.read_text(encoding="utf-8")
    before_dualtrack = dualtrack_path.read_text(encoding="utf-8")

    result = ConnectorConfigApply(
        output_root,
        pipeline_config_path=pipeline_path,
        dualtrack_config_path=dualtrack_path,
    ).apply({"activation_plan": plan})

    assert result["status"] == "dry_run_ready"
    assert result["mode"] == "dry_run"
    assert result["backup"]["created"] is False
    assert result["safety"]["writes_runtime_config"] is False
    assert result["safety"]["opens_network_clients"] is False
    assert result["safety"]["submits_orders"] is False
    assert pipeline_path.read_text(encoding="utf-8") == before_pipeline
    assert dualtrack_path.read_text(encoding="utf-8") == before_dualtrack
    assert load_json(output_root / "connector_config_apply" / "current.json")[-1]["status"] == "dry_run_ready"


def test_connector_config_status_summarizes_apply_backup_and_rollback(tmp_path: Path) -> None:
    plan, output_root, pipeline_path, dualtrack_path = _activation_plan(tmp_path)
    service = ConnectorConfigApply(output_root, pipeline_config_path=pipeline_path, dualtrack_config_path=dualtrack_path)

    dry_run = service.apply({"activation_plan": plan})
    dry_status = service.status()

    assert dry_status["schema_version"] == "connector-config-status-v1"
    assert dry_status["status"] == "dry_run_ready"
    assert dry_status["latest_apply"]["apply_id"] == dry_run["apply_id"]
    assert dry_status["latest_apply"]["mode"] == "dry_run"
    assert dry_status["latest_apply"]["change_count"] == dry_run["change_counts"]["total"]
    assert dry_status["latest_check"]["status"] == "missing"
    assert dry_status["latest_rehearsal"]["status"] == "missing"
    assert dry_status["latest_authorization"]["status"] == "missing"
    assert dry_status["latest_readiness_audit"]["status"] == "missing"
    assert dry_status["latest_post_switch_validation"]["status"] == "missing"
    assert dry_status["current_runtime"]["broker_provider"] == "binance_usdm"
    assert dry_status["operator_stage"]["stage"] == "dry_run_ready_pending_handoff"
    assert dry_status["operator_stage"]["runtime_switched_to_tiger_mgc"] is False
    assert dry_status["operator_stage"]["price_feed_ready"] is True
    assert dry_status["backup"]["created"] is False
    assert dry_status["safety"]["read_only"] is True
    assert dry_status["safety"]["opens_network_clients"] is False
    assert dry_status["safety"]["submits_orders"] is False
    assert dry_status["safety"]["writes_runtime_config"] is False

    service.check_plan({"activation_plan": plan})
    service.handoff({"activation_plan": plan})
    service.rehearse({"activation_plan": plan})
    applied = service.apply(
        {
            "activation_plan": plan,
            "write": True,
            "accept_warnings": True,
            "acknowledgement": plan["config_apply_package"]["operator_acknowledgement"],
            "package_id": plan["config_apply_package"]["package_id"],
        }
    )
    rollback = service.rollback({"apply_receipt": applied, "acknowledgement": ROLLBACK_ACKNOWLEDGEMENT})
    status = service.status()

    assert status["status"] == "applied"
    assert status["latest_apply"]["apply_id"] == applied["apply_id"]
    assert status["latest_apply"]["package_id"] == applied["package_id"]
    assert status["latest_apply"]["mode"] == "write"
    assert status["latest_apply"]["backup_created"] is True
    assert status["backup"]["created"] is True
    assert status["backup"]["file_count"] == 2
    assert status["latest_rollback"]["status"] == "rolled_back"
    assert status["latest_rollback"]["rollback_id"] == rollback["rollback_id"]
    assert status["latest_rollback"]["restored_file_count"] == 2


def test_connector_config_status_reports_operator_stage_ready_for_attended_switch(tmp_path: Path) -> None:
    plan, output_root, pipeline_path, dualtrack_path = _activation_plan(tmp_path)
    service = ConnectorConfigApply(output_root, pipeline_config_path=pipeline_path, dualtrack_config_path=dualtrack_path)
    service.apply({"activation_plan": plan})
    service.check_plan({"activation_plan": plan})
    service.handoff({"activation_plan": plan})
    service.rehearse({"activation_plan": plan})
    service.authorization({"activation_plan": plan})
    service.readiness_audit({"activation_plan": plan})

    status = service.status()

    assert status["operator_stage"]["stage"] == "ready_for_attended_config_switch"
    assert status["operator_stage"]["price_feed_ready"] is True
    assert status["operator_stage"]["price_feed_status_ready"] is True
    assert status["operator_stage"]["price_feed_evidence_fresh"] is True
    assert status["operator_stage"]["readiness_audit_fresh"] is True
    assert status["operator_stage"]["runtime_switched_to_tiger_mgc"] is False
    assert status["operator_stage"]["can_switch_config_with_operator_authorization"] is True
    assert status["operator_stage"]["can_trade_machine_track"] is False
    assert status["operator_stage"]["can_submit_tiger_orders"] is False
    assert status["operator_stage"]["next_action"] == "operator_review_authorization_then_run_attended_config_apply_if_approved"
    switch_review = status["operator_stage"]["attended_switch_review"]
    assert switch_review["schema_version"] == "connector-attended-switch-review-v1"
    assert switch_review["status"] == "ready_for_operator_review"
    assert switch_review["package_id"] == plan["config_apply_package"]["package_id"]
    assert switch_review["can_switch_config_with_operator_authorization"] is True
    assert switch_review["requires_operator_command"] is True
    assert switch_review["requires_warning_acceptance"] is True
    assert switch_review["requires_acknowledgement"] is True
    assert switch_review["requires_package_id"] is True
    assert switch_review["rollback_required"] is True
    assert switch_review["post_apply_validation_count"] == 4
    assert switch_review["runtime_config_writes_from_status_endpoint"] is False
    assert switch_review["opens_network_clients_from_status_endpoint"] is False
    assert switch_review["submits_orders_from_status_endpoint"] is False
    assert switch_review["can_trade_machine_track_after_switch"] is False
    assert switch_review["can_submit_tiger_orders_after_switch"] is False
    assert status["current_runtime"]["broker_provider"] == "binance_usdm"
    assert status["current_runtime"]["dualtrack_symbol"] == "GOLD"
    assert status["price_feed"]["acceptance"]["status"] == "accepted"
    assert status["price_feed"]["acceptance"]["fresh"] is True
    assert status["price_feed"]["readiness"]["status"] == "ready_for_price_feed"
    assert status["price_feed"]["readiness"]["fresh"] is True
    assert status["price_feed"]["realtime_validation"]["status"] == "pass"
    assert status["price_feed"]["realtime_validation"]["fresh"] is True
    assert status["safety"]["writes_runtime_config"] is False
    assert load_json_yaml(pipeline_path)["broker"]["provider"] == "binance_usdm"


def test_connector_config_status_downgrades_ready_stage_when_price_feed_evidence_is_stale(tmp_path: Path) -> None:
    plan, output_root, pipeline_path, dualtrack_path = _activation_plan(tmp_path)
    service = ConnectorConfigApply(output_root, pipeline_config_path=pipeline_path, dualtrack_config_path=dualtrack_path)
    service.apply({"activation_plan": plan})
    service.check_plan({"activation_plan": plan})
    service.handoff({"activation_plan": plan})
    service.rehearse({"activation_plan": plan})
    service.authorization({"activation_plan": plan})
    service.readiness_audit({"activation_plan": plan})
    stale_checked_at = (datetime.now(timezone.utc) - timedelta(hours=2)).replace(microsecond=0).isoformat()
    _write_tiger_price_feed_ready_artifacts(output_root, checked_at=stale_checked_at)

    status = service.status()

    assert status["operator_stage"]["stage"] == "price_feed_evidence_stale_refresh_acceptance"
    assert status["operator_stage"]["price_feed_status_ready"] is True
    assert status["operator_stage"]["price_feed_evidence_fresh"] is False
    assert status["operator_stage"]["price_feed_ready"] is False
    assert status["operator_stage"]["can_switch_config_with_operator_authorization"] is False
    assert status["operator_stage"]["attended_switch_review"]["status"] == "not_ready"
    assert status["operator_stage"]["attended_switch_review"]["can_switch_config_with_operator_authorization"] is False
    assert status["operator_stage"]["attended_switch_review"]["runtime_config_writes_from_status_endpoint"] is False
    assert status["operator_stage"]["attended_switch_review"]["submits_orders_from_status_endpoint"] is False
    commands = {row["name"]: row for row in status["operator_stage"]["refresh_commands"]}
    assert set(commands) == {
        "preview_tiger_price_feed_acceptance_refresh",
        "refresh_tiger_price_feed_acceptance",
        "refresh_final_readiness_audit",
        "show_connector_switch_status",
    }
    today = datetime.now(timezone.utc).date().isoformat()
    assert commands["preview_tiger_price_feed_acceptance_refresh"]["command"] == (
        f"python3 -m pipelines.tiger_price_feed_acceptance --date {today} --contract MGCmain --poll-seconds 75 --plan-only --json"
    )
    assert commands["preview_tiger_price_feed_acceptance_refresh"]["opens_quote_client"] is False
    assert commands["preview_tiger_price_feed_acceptance_refresh"]["opens_trade_client"] is False
    assert commands["preview_tiger_price_feed_acceptance_refresh"]["submits_orders"] is False
    assert commands["preview_tiger_price_feed_acceptance_refresh"]["writes_runtime_config"] is False
    assert commands["preview_tiger_price_feed_acceptance_refresh"]["writes_plan_artifact"] is True
    assert commands["refresh_tiger_price_feed_acceptance"]["command"] == (
        f"python3 -m pipelines.tiger_price_feed_acceptance --date {today} --contract MGCmain --poll-seconds 75 --json"
    )
    assert commands["refresh_tiger_price_feed_acceptance"]["opens_quote_client"] is True
    assert commands["refresh_tiger_price_feed_acceptance"]["opens_trade_client"] is False
    assert commands["refresh_tiger_price_feed_acceptance"]["submits_orders"] is False
    assert commands["refresh_tiger_price_feed_acceptance"]["writes_runtime_config"] is False
    assert commands["refresh_final_readiness_audit"]["opens_quote_client"] is False
    assert commands["refresh_final_readiness_audit"]["submits_orders"] is False
    assert status["price_feed"]["acceptance"]["fresh"] is False
    assert status["price_feed"]["readiness"]["fresh"] is False
    assert status["price_feed"]["realtime_validation"]["fresh"] is False
    assert status["safety"]["writes_runtime_config"] is False
    assert load_json_yaml(pipeline_path)["broker"]["provider"] == "binance_usdm"


def test_connector_config_price_feed_refresh_runbook_writes_safe_operator_package(tmp_path: Path) -> None:
    plan, output_root, pipeline_path, dualtrack_path = _activation_plan(tmp_path)
    service = ConnectorConfigApply(output_root, pipeline_config_path=pipeline_path, dualtrack_config_path=dualtrack_path)
    service.apply({"activation_plan": plan})
    service.check_plan({"activation_plan": plan})
    service.handoff({"activation_plan": plan})
    service.rehearse({"activation_plan": plan})
    service.authorization({"activation_plan": plan})
    service.readiness_audit({"activation_plan": plan})
    stale_checked_at = (datetime.now(timezone.utc) - timedelta(hours=2)).replace(microsecond=0).isoformat()
    _write_tiger_price_feed_ready_artifacts(output_root, checked_at=stale_checked_at)

    result = service.price_feed_refresh_runbook(as_of="2026-07-06T21:30:00+00:00")

    assert result["schema_version"] == "connector-price-feed-refresh-runbook-v1"
    assert result["status"] == "ready_for_operator_refresh"
    assert result["status_receipt"]["operator_stage"]["stage"] == "price_feed_evidence_stale_refresh_acceptance"
    assert result["operator_stage"]["stage"] == "price_feed_evidence_stale_refresh_acceptance"
    assert result["operator_stage"]["runtime_switched_to_tiger_mgc"] is False
    assert result["operator_stage"]["can_switch_config_with_operator_authorization"] is False
    assert result["operator_stage"]["can_trade_machine_track"] is False
    assert result["operator_stage"]["can_submit_tiger_orders"] is False
    assert result["refresh_window_gate"]["status"] == "wait_for_comex_open"
    assert result["refresh_window_gate"]["operator_action"] == "wait_until_next_open"
    assert result["refresh_window_gate"]["is_open"] is False
    assert result["refresh_window_gate"]["next_open"] == "2026-07-06T22:00:00+00:00"
    assert result["refresh_window_gate"]["can_preview_now"] is True
    assert result["refresh_window_gate"]["can_run_quote_client_step"] is False
    assert result["refresh_window_gate"]["safety"]["opens_quote_client"] is False
    assert result["refresh_window_gate"]["safety"]["opens_trade_client"] is False
    assert result["refresh_window_gate"]["safety"]["submits_orders"] is False
    assert result["refresh_window_gate"]["safety"]["writes_runtime_config"] is False
    commands = {row["name"]: row for row in result["command_sequence"]}
    assert list(commands) == [
        "preview_tiger_price_feed_acceptance_refresh",
        "refresh_tiger_price_feed_acceptance",
        "refresh_final_readiness_audit",
        "show_connector_switch_status",
    ]
    assert commands["preview_tiger_price_feed_acceptance_refresh"]["opens_quote_client"] is False
    assert commands["refresh_tiger_price_feed_acceptance"]["opens_quote_client"] is True
    assert commands["refresh_tiger_price_feed_acceptance"]["opens_trade_client"] is False
    assert commands["refresh_tiger_price_feed_acceptance"]["submits_orders"] is False
    assert commands["refresh_tiger_price_feed_acceptance"]["writes_runtime_config"] is False
    assert result["safety"]["opens_network_clients"] is False
    assert result["safety"]["opens_quote_client"] is False
    assert result["safety"]["opens_trade_client"] is False
    assert result["safety"]["submits_orders"] is False
    assert result["safety"]["writes_runtime_config"] is False
    assert load_json(output_root / "connector_config_apply" / "price_feed_refresh_runbook_current.json")[-1]["status"] == "ready_for_operator_refresh"
    assert load_json(output_root / "connector_config_apply" / "status_current.json")[-1]["operator_stage"]["stage"] == "price_feed_evidence_stale_refresh_acceptance"
    status = service.status()
    runbook_summary = status["latest_price_feed_refresh_runbook"]
    assert runbook_summary["status"] == "ready_for_operator_refresh"
    assert runbook_summary["operator_stage"] == "price_feed_evidence_stale_refresh_acceptance"
    assert runbook_summary["command_count"] == 4
    assert runbook_summary["status_snapshot_exists"] is True
    assert runbook_summary["opens_network_clients"] is False
    assert runbook_summary["opens_quote_client"] is False
    assert runbook_summary["opens_trade_client"] is False
    assert runbook_summary["submits_orders"] is False
    assert runbook_summary["writes_runtime_config"] is False
    assert runbook_summary["refresh_window_status"] == "wait_for_comex_open"
    assert runbook_summary["refresh_window_next_open"] == "2026-07-06T22:00:00+00:00"
    assert runbook_summary["refresh_window_can_run_quote_client_step"] is False
    markdown = (output_root / "connector_config_apply" / "price_feed_refresh_runbook_current.md").read_text(encoding="utf-8")
    assert "Tiger/MGC Price-Feed Refresh Runbook" in markdown
    assert "Refresh Window Gate" in markdown
    assert "Status: `wait_for_comex_open`" in markdown
    assert "Can run QuoteClient acceptance step now: `False`" in markdown
    assert "--plan-only --json" in markdown
    assert "opens_trade_client=`False`" in markdown
    assert "submits_orders=`False`" in markdown
    assert "writes_runtime_config=`False`" in markdown
    assert load_json_yaml(pipeline_path)["broker"]["provider"] == "binance_usdm"


def test_connector_config_price_feed_refresh_runbook_marks_open_window_without_opening_clients(tmp_path: Path) -> None:
    plan, output_root, pipeline_path, dualtrack_path = _activation_plan(tmp_path)
    service = ConnectorConfigApply(output_root, pipeline_config_path=pipeline_path, dualtrack_config_path=dualtrack_path)
    service.apply({"activation_plan": plan})
    service.check_plan({"activation_plan": plan})
    service.handoff({"activation_plan": plan})
    service.rehearse({"activation_plan": plan})
    service.authorization({"activation_plan": plan})
    service.readiness_audit({"activation_plan": plan})
    stale_checked_at = (datetime.now(timezone.utc) - timedelta(hours=2)).replace(microsecond=0).isoformat()
    _write_tiger_price_feed_ready_artifacts(output_root, checked_at=stale_checked_at)

    result = service.price_feed_refresh_runbook(persist=False, as_of="2026-07-06T22:30:00+00:00")

    assert result["refresh_window_gate"]["status"] == "ready_to_run_acceptance_now"
    assert result["refresh_window_gate"]["operator_action"] == "run_price_feed_acceptance_sequence_now"
    assert result["refresh_window_gate"]["is_open"] is True
    assert result["refresh_window_gate"]["next_open"] is None
    assert result["refresh_window_gate"]["can_preview_now"] is True
    assert result["refresh_window_gate"]["can_run_quote_client_step"] is True
    assert result["refresh_window_gate"]["safety"]["uses_local_session_calendar"] is True
    assert result["refresh_window_gate"]["safety"]["opens_network_clients"] is False
    assert result["refresh_window_gate"]["safety"]["opens_quote_client"] is False
    assert result["refresh_window_gate"]["safety"]["opens_trade_client"] is False
    assert result["refresh_window_gate"]["safety"]["submits_orders"] is False
    assert result["refresh_window_gate"]["safety"]["writes_runtime_config"] is False


def test_connector_config_readiness_audit_blocks_stale_price_feed_evidence(tmp_path: Path) -> None:
    stale_checked_at = (datetime.now(timezone.utc) - timedelta(hours=2)).replace(microsecond=0).isoformat()
    plan, output_root, pipeline_path, dualtrack_path = _activation_plan(tmp_path, with_switch_support=False)
    _write_tiger_price_feed_ready_artifacts(output_root, checked_at=stale_checked_at)
    _write_tiger_market_coverage(tmp_path)
    service = ConnectorConfigApply(output_root, pipeline_config_path=pipeline_path, dualtrack_config_path=dualtrack_path)
    service.apply({"activation_plan": plan})
    service.check_plan({"activation_plan": plan})
    service.handoff({"activation_plan": plan})
    service.rehearse({"activation_plan": plan})
    service.authorization({"activation_plan": plan})

    result = service.readiness_audit({"activation_plan": plan})

    assert result["status"] == "blocked"
    assert any(row["name"] == "tiger_price_feed_evidence_stale" for row in result["blockers"])
    assert result["safety"]["writes_runtime_config"] is False
    assert load_json_yaml(pipeline_path)["broker"]["provider"] == "binance_usdm"


def test_connector_config_status_cli_reports_operator_stage_without_writing_config(tmp_path: Path) -> None:
    plan, output_root, pipeline_path, dualtrack_path = _activation_plan(tmp_path)
    service = ConnectorConfigApply(output_root, pipeline_config_path=pipeline_path, dualtrack_config_path=dualtrack_path)
    service.apply({"activation_plan": plan})
    service.check_plan({"activation_plan": plan})
    service.handoff({"activation_plan": plan})
    service.rehearse({"activation_plan": plan})
    service.authorization({"activation_plan": plan})
    service.readiness_audit({"activation_plan": plan})
    before_pipeline = pipeline_path.read_text(encoding="utf-8")
    before_dualtrack = dualtrack_path.read_text(encoding="utf-8")

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "pipelines.connector_config_apply",
            "status",
            "--pipeline-config",
            str(pipeline_path),
            "--dualtrack-config",
            str(dualtrack_path),
            "--output-root",
            str(output_root),
            "--json",
        ],
        cwd=Path(__file__).resolve().parents[1],
        check=True,
        capture_output=True,
        text=True,
    )
    status = json.loads(completed.stdout)

    assert status["operator_stage"]["stage"] == "ready_for_attended_config_switch"
    assert status["operator_stage"]["can_switch_config_with_operator_authorization"] is True
    assert status["operator_stage"]["can_trade_machine_track"] is False
    assert status["operator_stage"]["can_submit_tiger_orders"] is False
    assert status["operator_stage"]["attended_switch_review"]["status"] == "ready_for_operator_review"
    assert status["operator_stage"]["attended_switch_review"]["package_id"] == plan["config_apply_package"]["package_id"]
    assert status["operator_stage"]["attended_switch_review"]["runtime_config_writes_from_status_endpoint"] is False
    assert status["safety"]["writes_runtime_config"] is False
    assert pipeline_path.read_text(encoding="utf-8") == before_pipeline
    assert dualtrack_path.read_text(encoding="utf-8") == before_dualtrack


def test_connector_config_price_feed_refresh_runbook_cli_writes_without_config_change(tmp_path: Path) -> None:
    plan, output_root, pipeline_path, dualtrack_path = _activation_plan(tmp_path)
    service = ConnectorConfigApply(output_root, pipeline_config_path=pipeline_path, dualtrack_config_path=dualtrack_path)
    service.apply({"activation_plan": plan})
    service.check_plan({"activation_plan": plan})
    service.handoff({"activation_plan": plan})
    service.rehearse({"activation_plan": plan})
    service.authorization({"activation_plan": plan})
    service.readiness_audit({"activation_plan": plan})
    stale_checked_at = (datetime.now(timezone.utc) - timedelta(hours=2)).replace(microsecond=0).isoformat()
    _write_tiger_price_feed_ready_artifacts(output_root, checked_at=stale_checked_at)
    before_pipeline = pipeline_path.read_text(encoding="utf-8")
    before_dualtrack = dualtrack_path.read_text(encoding="utf-8")

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "pipelines.connector_config_apply",
            "price-feed-refresh-runbook",
            "--pipeline-config",
            str(pipeline_path),
            "--dualtrack-config",
            str(dualtrack_path),
            "--output-root",
            str(output_root),
            "--as-of",
            "2026-07-06T21:30:00+00:00",
            "--json",
        ],
        cwd=Path(__file__).resolve().parents[1],
        check=True,
        capture_output=True,
        text=True,
    )
    result = json.loads(completed.stdout)

    assert result["status"] == "ready_for_operator_refresh"
    assert result["command_sequence"][0]["name"] == "preview_tiger_price_feed_acceptance_refresh"
    assert result["command_sequence"][0]["opens_quote_client"] is False
    assert result["command_sequence"][1]["name"] == "refresh_tiger_price_feed_acceptance"
    assert result["command_sequence"][1]["opens_quote_client"] is True
    assert result["refresh_window_gate"]["status"] == "wait_for_comex_open"
    assert result["refresh_window_gate"]["next_open"] == "2026-07-06T22:00:00+00:00"
    assert result["refresh_window_gate"]["can_run_quote_client_step"] is False
    assert result["safety"]["opens_network_clients"] is False
    assert result["safety"]["writes_runtime_config"] is False
    summary_status = ConnectorConfigApply(
        output_root,
        pipeline_config_path=pipeline_path,
        dualtrack_config_path=dualtrack_path,
    ).status()
    assert summary_status["latest_price_feed_refresh_runbook"]["status"] == "ready_for_operator_refresh"
    assert summary_status["latest_price_feed_refresh_runbook"]["status_snapshot_exists"] is True
    assert summary_status["latest_price_feed_refresh_runbook"]["opens_network_clients"] is False
    assert (output_root / "connector_config_apply" / "price_feed_refresh_runbook_current.md").exists()
    assert (output_root / "connector_config_apply" / "status_current.json").exists()
    assert load_json(output_root / "connector_config_apply" / "status_current.json")[-1]["operator_stage"]["stage"] == "price_feed_evidence_stale_refresh_acceptance"
    assert pipeline_path.read_text(encoding="utf-8") == before_pipeline
    assert dualtrack_path.read_text(encoding="utf-8") == before_dualtrack


def test_connector_config_rehearsal_applies_and_rolls_back_only_sandbox_configs(tmp_path: Path) -> None:
    plan, output_root, pipeline_path, dualtrack_path = _activation_plan(tmp_path)
    service = ConnectorConfigApply(output_root, pipeline_config_path=pipeline_path, dualtrack_config_path=dualtrack_path)
    before_pipeline = pipeline_path.read_text(encoding="utf-8")
    before_dualtrack = dualtrack_path.read_text(encoding="utf-8")

    result = service.rehearse({"activation_plan": plan})
    status = service.status()

    assert result["status"] == "passed"
    assert result["package_id"] == plan["config_apply_package"]["package_id"]
    assert result["steps"]["dry_run"]["status"] == "dry_run_ready"
    assert result["steps"]["pre_handoff_check"]["status"] == "ready_for_handoff"
    assert result["steps"]["handoff"]["status"] == "ready_for_operator_review"
    assert result["steps"]["write_check"]["status"] == "ready_for_attended_config_write"
    assert result["steps"]["authorization"]["status"] == "ready_for_operator_authorization"
    assert result["steps"]["readiness_audit"]["status"] == "go_for_attended_config_switch"
    assert result["steps"]["sandbox_apply"]["status"] == "applied"
    assert result["steps"]["post_switch_validation"]["status"] == "validated_post_switch"
    assert result["steps"]["sandbox_rollback"]["status"] == "rolled_back"
    assert result["runtime_config"]["unchanged"] is True
    assert result["runtime_config"]["before"] == result["runtime_config"]["after"]
    assert result["sandbox_after_rollback"]["pipeline_sha256"] == result["runtime_config"]["before"]["pipeline_sha256"]
    assert result["sandbox_after_rollback"]["dualtrack_sha256"] == result["runtime_config"]["before"]["dualtrack_sha256"]
    assert result["safety"]["writes_runtime_config"] is False
    assert result["safety"]["writes_sandbox_config"] is True
    assert result["safety"]["opens_network_clients"] is False
    assert result["safety"]["submits_orders"] is False
    assert pipeline_path.read_text(encoding="utf-8") == before_pipeline
    assert dualtrack_path.read_text(encoding="utf-8") == before_dualtrack
    assert load_json(output_root / "connector_config_apply" / "rehearsal_current.json")[-1]["status"] == "passed"
    assert status["latest_rehearsal"]["status"] == "passed"
    assert status["latest_rehearsal"]["rehearsal_id"] == result["rehearsal_id"]
    assert status["latest_rehearsal"]["runtime_config_unchanged"] is True
    assert status["latest_rehearsal"]["writes_runtime_config"] is False
    assert status["latest_rehearsal"]["writes_sandbox_config"] is True


def test_connector_config_check_requires_current_dry_run(tmp_path: Path) -> None:
    plan, output_root, pipeline_path, dualtrack_path = _activation_plan(tmp_path)

    result = ConnectorConfigApply(
        output_root,
        pipeline_config_path=pipeline_path,
        dualtrack_config_path=dualtrack_path,
    ).check_plan({"activation_plan": plan})

    assert result["status"] == "blocked"
    assert {row["name"] for row in result["blockers"]} == {"config_apply_dry_run_missing"}
    assert result["safety"]["writes_runtime_config"] is False
    assert result["safety"]["opens_network_clients"] is False
    assert result["safety"]["submits_orders"] is False


def test_connector_config_check_reports_ready_for_handoff_after_matching_dry_run(tmp_path: Path) -> None:
    plan, output_root, pipeline_path, dualtrack_path = _activation_plan(tmp_path)
    service = ConnectorConfigApply(output_root, pipeline_config_path=pipeline_path, dualtrack_config_path=dualtrack_path)
    dry_run = service.apply({"activation_plan": plan})

    result = service.check_plan({"activation_plan": plan})
    status = service.status()

    assert result["status"] == "ready_for_handoff"
    assert result["package_id"] == plan["config_apply_package"]["package_id"]
    assert result["latest_dry_run"]["apply_id"] == dry_run["apply_id"]
    assert result["usable_for_attended_config_write"] is False
    assert result["operator_next_action"]["action"] == "generate_current_operator_handoff"
    assert result["operator_next_action"]["requires_accept_warnings"] is True
    assert result["write_preflight"]["status"] == "blocked"
    assert {row["name"] for row in result["write_preflight"]["blockers"]} == {"config_handoff_missing"}
    assert result["attended_apply_command"].startswith("python3 -m pipelines.connector_config_apply apply")
    assert "--package-id" in result["attended_apply_command"]
    assert "python3 -m pipelines.connector_catalog --json" in result["post_apply_validation_commands"]
    assert result["rollback_boundary"]["required"] is True
    assert result["safety"]["writes_runtime_config"] is False
    assert status["latest_check"]["status"] == "ready_for_handoff"
    assert status["latest_check"]["package_id"] == result["package_id"]
    assert status["latest_check"]["write_preflight_status"] == "blocked"
    assert status["latest_check"]["write_preflight_blocker_count"] == 1
    assert status["latest_check"]["post_apply_validation_count"] == len(result["post_apply_validation_commands"])
    assert status["latest_check"]["rollback_required"] is True


def test_connector_config_check_reports_ready_for_rehearsal_after_matching_handoff(tmp_path: Path) -> None:
    plan, output_root, pipeline_path, dualtrack_path = _activation_plan(tmp_path)
    service = ConnectorConfigApply(output_root, pipeline_config_path=pipeline_path, dualtrack_config_path=dualtrack_path)
    service.apply({"activation_plan": plan})
    service.check_plan({"activation_plan": plan})
    service.handoff({"activation_plan": plan})

    result = service.check_plan({"activation_plan": plan})
    status = service.status()

    assert result["status"] == "ready_for_rehearsal"
    assert result["usable_for_attended_config_write"] is False
    assert result["operator_next_action"]["action"] == "run_sandbox_config_rehearsal"
    assert result["write_preflight"]["status"] == "blocked"
    assert {row["name"] for row in result["write_preflight"]["blockers"]} == {"config_rehearsal_missing"}
    assert result["write_preflight"]["latest_handoff"]["status"] == "ready_for_operator_review"
    assert status["latest_check"]["status"] == "ready_for_rehearsal"
    assert status["latest_check"]["write_preflight_status"] == "blocked"
    assert status["latest_check"]["write_preflight_blocker_count"] == 1


def test_connector_config_check_reports_write_ready_after_matching_rehearsal(tmp_path: Path) -> None:
    plan, output_root, pipeline_path, dualtrack_path = _activation_plan(tmp_path)
    service = ConnectorConfigApply(output_root, pipeline_config_path=pipeline_path, dualtrack_config_path=dualtrack_path)
    service.apply({"activation_plan": plan})
    service.check_plan({"activation_plan": plan})
    service.handoff({"activation_plan": plan})
    rehearsal = service.rehearse({"activation_plan": plan})

    result = service.check_plan({"activation_plan": plan})
    status = service.status()

    assert rehearsal["status"] == "passed"
    assert result["status"] == "ready_for_attended_config_write"
    assert result["usable_for_attended_config_write"] is True
    assert result["operator_next_action"]["action"] == "authorize_attended_config_write_with_warning_acceptance"
    assert result["write_preflight"]["status"] == "ready"
    assert result["write_preflight"]["blockers"] == []
    assert result["write_preflight"]["latest_handoff"]["status"] == "ready_for_operator_review"
    assert result["write_preflight"]["latest_rehearsal"]["status"] == "passed"
    assert status["latest_check"]["status"] == "ready_for_attended_config_write"
    assert status["latest_check"]["write_preflight_status"] == "ready"
    assert status["latest_check"]["write_preflight_blocker_count"] == 0


def test_connector_config_authorization_writes_operator_package_without_config_write(tmp_path: Path) -> None:
    plan, output_root, pipeline_path, dualtrack_path = _activation_plan(tmp_path)
    service = ConnectorConfigApply(output_root, pipeline_config_path=pipeline_path, dualtrack_config_path=dualtrack_path)
    service.apply({"activation_plan": plan})
    service.check_plan({"activation_plan": plan})
    handoff = service.handoff({"activation_plan": plan})
    rehearsal = service.rehearse({"activation_plan": plan})
    before_pipeline = pipeline_path.read_text(encoding="utf-8")
    before_dualtrack = dualtrack_path.read_text(encoding="utf-8")

    result = service.authorization({"activation_plan": plan})
    status = service.status()

    assert result["schema_version"] == "connector-config-authorization-v1"
    assert result["status"] == "ready_for_operator_authorization"
    assert result["package_id"] == plan["config_apply_package"]["package_id"]
    assert result["check"]["status"] == "ready_for_attended_config_write"
    assert result["handoff"]["handoff_id"] == handoff["handoff_id"]
    assert result["rehearsal"]["rehearsal_id"] == rehearsal["rehearsal_id"]
    assert result["rehearsal"]["runtime_config_unchanged"] is True
    assert result["current_runtime"]["broker_provider"] == "binance_usdm"
    assert result["current_runtime"]["dualtrack_symbol"] == "GOLD"
    assert result["operator_next_action"]["action"] == "authorize_attended_config_write_with_warning_acceptance"
    assert "python3 -m pipelines.connector_config_apply apply" in result["attended_apply_command"]
    assert len(result["post_apply_validation_commands"]) == 4
    assert result["rollback_boundary"]["required"] is True
    assert result["blockers"] == []
    assert result["safety"]["writes_runtime_config"] is False
    assert result["safety"]["opens_network_clients"] is False
    assert result["safety"]["submits_orders"] is False
    assert pipeline_path.read_text(encoding="utf-8") == before_pipeline
    assert dualtrack_path.read_text(encoding="utf-8") == before_dualtrack

    markdown = (output_root / "connector_config_apply" / "authorization_current.md").read_text(encoding="utf-8")
    assert "Tiger/MGC Connector Config Authorization Package" in markdown
    assert "Active broker: `binance_usdm`" in markdown
    assert "Dualtrack symbol: `GOLD`" in markdown
    assert "writing runtime config from this authorization package" in markdown
    assert load_json(output_root / "connector_config_apply" / "authorization_current.json")[-1]["status"] == "ready_for_operator_authorization"
    assert status["latest_authorization"]["status"] == "ready_for_operator_authorization"
    assert status["latest_authorization"]["authorization_id"] == result["authorization_id"]
    assert status["latest_authorization"]["package_id"] == result["package_id"]
    assert status["latest_authorization"]["blocker_count"] == 0
    assert status["latest_authorization"]["post_apply_validation_count"] == 4
    assert status["latest_authorization"]["writes_runtime_config"] is False
    assert status["latest_authorization"]["opens_network_clients"] is False
    assert status["latest_authorization"]["submits_orders"] is False


def test_connector_config_authorization_blocks_without_rehearsal(tmp_path: Path) -> None:
    plan, output_root, pipeline_path, dualtrack_path = _activation_plan(tmp_path)
    service = ConnectorConfigApply(output_root, pipeline_config_path=pipeline_path, dualtrack_config_path=dualtrack_path)
    service.apply({"activation_plan": plan})
    service.check_plan({"activation_plan": plan})
    service.handoff({"activation_plan": plan})
    before_pipeline = pipeline_path.read_text(encoding="utf-8")
    before_dualtrack = dualtrack_path.read_text(encoding="utf-8")

    result = service.authorization({"activation_plan": plan})

    assert result["status"] == "blocked"
    blocker_names = {row["name"] for row in result["blockers"]}
    assert "config_rehearsal_not_passed" in blocker_names
    assert "config_write_preflight_not_ready" in blocker_names
    assert result["safety"]["writes_runtime_config"] is False
    assert result["safety"]["opens_network_clients"] is False
    assert result["safety"]["submits_orders"] is False
    assert pipeline_path.read_text(encoding="utf-8") == before_pipeline
    assert dualtrack_path.read_text(encoding="utf-8") == before_dualtrack


def test_connector_config_readiness_audit_reports_go_no_go_without_config_write(tmp_path: Path) -> None:
    plan, output_root, pipeline_path, dualtrack_path = _activation_plan(tmp_path)
    _write_tiger_price_feed_ready_artifacts(output_root)
    _write_tiger_market_coverage(tmp_path)
    service = ConnectorConfigApply(output_root, pipeline_config_path=pipeline_path, dualtrack_config_path=dualtrack_path)
    service.apply({"activation_plan": plan})
    service.check_plan({"activation_plan": plan})
    service.handoff({"activation_plan": plan})
    service.rehearse({"activation_plan": plan})
    service.authorization({"activation_plan": plan})
    before_pipeline = pipeline_path.read_text(encoding="utf-8")
    before_dualtrack = dualtrack_path.read_text(encoding="utf-8")

    result = service.readiness_audit({"activation_plan": plan})
    status = service.status()

    assert result["schema_version"] == "connector-final-readiness-audit-v1"
    assert result["status"] == "go_for_attended_config_switch"
    assert result["current_stage"] == "pre_switch_authorization_ready"
    assert result["can_switch_config_with_operator_authorization"] is True
    assert result["can_trade_machine_track"] is False
    assert result["can_submit_tiger_orders"] is False
    assert result["price_feed"]["acceptance"]["status"] == "accepted"
    assert result["price_feed"]["realtime_validation"]["status"] == "pass"
    assert result["price_feed"]["market_coverage"]["rows"] == 500
    assert result["config_authorization"]["status"] == "ready_for_operator_authorization"
    assert result["strategy_gate"]["requires_strategy_edge_approval"] is True
    assert result["broker_order_gate"]["status"] == "closed"
    assert result["blockers"] == []
    assert result["safety"]["writes_runtime_config"] is False
    assert result["safety"]["writes_market_db"] is False
    assert result["safety"]["opens_network_clients"] is False
    assert result["safety"]["submits_orders"] is False
    assert pipeline_path.read_text(encoding="utf-8") == before_pipeline
    assert dualtrack_path.read_text(encoding="utf-8") == before_dualtrack

    markdown = (output_root / "connector_config_apply" / "final_readiness_audit_current.md").read_text(encoding="utf-8")
    assert "Tiger/MGC Final Readiness Audit" in markdown
    assert "go_for_attended_config_switch" in markdown
    assert "Use Tiger/MGC price source" in markdown
    assert "Tiger order submission" in markdown
    assert load_json(output_root / "connector_config_apply" / "final_readiness_audit_current.json")[-1]["status"] == "go_for_attended_config_switch"
    assert status["latest_readiness_audit"]["status"] == "go_for_attended_config_switch"
    assert status["latest_readiness_audit"]["can_switch_config_with_operator_authorization"] is True
    assert status["latest_readiness_audit"]["can_trade_machine_track"] is False
    assert status["latest_readiness_audit"]["can_submit_tiger_orders"] is False


def test_connector_config_readiness_audit_blocks_without_price_feed_and_market_coverage(tmp_path: Path) -> None:
    plan, output_root, pipeline_path, dualtrack_path = _activation_plan(tmp_path, with_switch_support=False)
    service = ConnectorConfigApply(output_root, pipeline_config_path=pipeline_path, dualtrack_config_path=dualtrack_path)
    service.apply({"activation_plan": plan})
    service.check_plan({"activation_plan": plan})
    service.handoff({"activation_plan": plan})
    service.rehearse({"activation_plan": plan})
    service.authorization({"activation_plan": plan})

    result = service.readiness_audit({"activation_plan": plan})

    assert result["status"] == "blocked"
    blocker_names = {row["name"] for row in result["blockers"]}
    assert "tiger_price_feed_acceptance_not_ready" in blocker_names
    assert "tiger_realtime_validation_not_passed" in blocker_names
    assert "tiger_market_coverage_insufficient" in blocker_names
    assert result["can_switch_config_with_operator_authorization"] is False
    assert result["safety"]["writes_runtime_config"] is False
    assert result["safety"]["opens_network_clients"] is False
    assert result["safety"]["submits_orders"] is False


def test_connector_config_post_switch_validation_blocks_before_config_switch(tmp_path: Path) -> None:
    plan, output_root, pipeline_path, dualtrack_path = _activation_plan(tmp_path)
    _write_tiger_price_feed_ready_artifacts(output_root)
    _write_tiger_market_coverage(tmp_path)
    service = ConnectorConfigApply(output_root, pipeline_config_path=pipeline_path, dualtrack_config_path=dualtrack_path)
    service.apply({"activation_plan": plan})
    service.check_plan({"activation_plan": plan})
    service.handoff({"activation_plan": plan})
    service.rehearse({"activation_plan": plan})
    service.authorization({"activation_plan": plan})
    service.readiness_audit({"activation_plan": plan})

    result = service.post_switch_validate({"package_id": plan["config_apply_package"]["package_id"]})

    assert result["status"] == "blocked"
    blocker_names = {row["name"] for row in result["blockers"]}
    assert "config_apply_not_applied" in blocker_names
    assert "runtime_broker_provider_mismatch" in blocker_names
    assert "runtime_dualtrack_symbol_mismatch" in blocker_names
    assert result["safety"]["writes_runtime_config"] is False
    assert result["safety"]["opens_network_clients"] is False
    assert result["safety"]["submits_orders"] is False


def test_connector_config_post_switch_validation_passes_after_attended_config_write_in_sandbox(tmp_path: Path) -> None:
    plan, output_root, pipeline_path, dualtrack_path = _activation_plan(tmp_path)
    _write_tiger_price_feed_ready_artifacts(output_root)
    _write_tiger_market_coverage(tmp_path)
    service = ConnectorConfigApply(output_root, pipeline_config_path=pipeline_path, dualtrack_config_path=dualtrack_path)
    service.apply({"activation_plan": plan})
    service.check_plan({"activation_plan": plan})
    service.handoff({"activation_plan": plan})
    service.rehearse({"activation_plan": plan})
    service.authorization({"activation_plan": plan})
    service.readiness_audit({"activation_plan": plan})
    applied = service.apply(
        {
            "activation_plan": plan,
            "write": True,
            "accept_warnings": True,
            "acknowledgement": plan["config_apply_package"]["operator_acknowledgement"],
            "package_id": plan["config_apply_package"]["package_id"],
        }
    )

    result = service.post_switch_validate({"package_id": plan["config_apply_package"]["package_id"]})
    status = service.status()

    assert applied["status"] == "applied"
    assert result["schema_version"] == "connector-post-switch-validation-v1"
    assert result["status"] == "validated_post_switch"
    assert result["package_id"] == plan["config_apply_package"]["package_id"]
    assert result["current_runtime"]["broker_provider"] == "tiger_openapi"
    assert result["current_runtime"]["broker_dry_run"] is True
    assert result["current_runtime"]["dualtrack_symbol"] == "MGCmain"
    assert result["current_runtime"]["dualtrack_provider"] == "tiger_openapi:COMEX"
    assert result["current_runtime"]["market_session_enabled"] is True
    assert result["current_runtime"]["execution_cost_venue"] == "tiger_mgc"
    assert result["current_runtime"]["execution_quantity_mode"] == "integer_contracts"
    assert result["current_runtime"]["human_fill_sync_enabled"] is True
    assert result["current_runtime"]["can_enable_broker_orders"] is False
    assert result["rollback"]["backup_created"] is True
    assert result["rollback"]["available"] is True
    assert result["price_feed"]["market_coverage"]["rows"] == 500
    assert result["blockers"] == []
    assert result["safety"]["writes_runtime_config"] is False
    assert result["safety"]["writes_market_db"] is False
    assert result["safety"]["opens_network_clients"] is False
    assert result["safety"]["submits_orders"] is False

    markdown = (output_root / "connector_config_apply" / "post_switch_validation_current.md").read_text(encoding="utf-8")
    assert "Tiger/MGC Post-Switch Validation" in markdown
    assert "validated_post_switch" in markdown
    assert "Broker orders enabled: `False`" in markdown
    assert load_json(output_root / "connector_config_apply" / "post_switch_validation_current.json")[-1]["status"] == "validated_post_switch"
    assert status["latest_post_switch_validation"]["status"] == "validated_post_switch"
    assert status["latest_post_switch_validation"]["broker_provider"] == "tiger_openapi"
    assert status["latest_post_switch_validation"]["dualtrack_symbol"] == "MGCmain"
    assert status["latest_post_switch_validation"]["rollback_available"] is True
    assert status["latest_post_switch_validation"]["opens_network_clients"] is False
    assert status["latest_post_switch_validation"]["submits_orders"] is False


def test_connector_config_check_blocks_tampered_plan(tmp_path: Path) -> None:
    plan, output_root, pipeline_path, dualtrack_path = _activation_plan(tmp_path)
    service = ConnectorConfigApply(output_root, pipeline_config_path=pipeline_path, dualtrack_config_path=dualtrack_path)
    service.apply({"activation_plan": plan})
    tampered = json.loads(json.dumps(plan))
    tampered["config_patch_preview"][0]["planned"] = False

    result = service.check_plan({"activation_plan": tampered})

    assert result["status"] == "blocked"
    assert {row["name"] for row in result["blockers"]} == {"config_apply_package_id_stale"}
    assert result["safety"]["writes_runtime_config"] is False


def test_connector_config_handoff_writes_operator_markdown_without_config_write(tmp_path: Path) -> None:
    plan, output_root, pipeline_path, dualtrack_path = _activation_plan(tmp_path)
    service = ConnectorConfigApply(output_root, pipeline_config_path=pipeline_path, dualtrack_config_path=dualtrack_path)
    service.apply({"activation_plan": plan})
    service.check_plan({"activation_plan": plan})
    before_pipeline = pipeline_path.read_text(encoding="utf-8")
    before_dualtrack = dualtrack_path.read_text(encoding="utf-8")

    result = service.handoff({"activation_plan": plan})

    assert result["status"] == "ready_for_operator_review"
    assert result["package_id"] == plan["config_apply_package"]["package_id"]
    assert result["current_runtime"]["broker_provider"] == "binance_usdm"
    assert result["current_runtime"]["dualtrack_symbol"] == "GOLD"
    assert result["check"]["status"] == "ready_for_attended_config_write"
    assert result["evidence_chain"]["schema_version"] == "connector-config-handoff-evidence-v1"
    assert result["evidence_chain"]["evidence_count"] == 6
    assert {row["name"] for row in result["evidence_chain"]["sources"]} == {
        "activation_plan",
        "config_apply_dry_run",
        "persisted_config_check",
    }
    assert {row["name"] for row in result["evidence_chain"]["current_config"]} == {
        "pipeline_config",
        "dualtrack_config",
    }
    assert all(row["exists"] and row["sha256"] for row in result["evidence_chain"]["sources"])
    assert all(row["exists"] and row["sha256"] for row in result["evidence_chain"]["current_config"])
    assert result["evidence_chain"]["computed_check"]["payload_sha256"]
    assert "python3 -m pipelines.connector_config_apply apply" in result["attended_apply_command"]
    assert len(result["post_apply_validation_commands"]) == 4
    assert result["rollback_boundary"]["required"] is True
    assert result["safety"]["writes_runtime_config"] is False
    assert result["safety"]["opens_network_clients"] is False
    assert result["safety"]["submits_orders"] is False
    assert pipeline_path.read_text(encoding="utf-8") == before_pipeline
    assert dualtrack_path.read_text(encoding="utf-8") == before_dualtrack

    markdown = (output_root / "connector_config_apply" / "handoff_current.md").read_text(encoding="utf-8")
    assert "Tiger/MGC Connector Config Handoff" in markdown
    assert "Active broker: `binance_usdm`" in markdown
    assert "Dualtrack symbol: `GOLD`" in markdown
    assert "## Evidence Chain" in markdown
    assert "pipeline_config" in markdown
    assert "writing runtime config from this handoff command" in markdown
    assert load_json(output_root / "connector_config_apply" / "handoff_current.json")[-1]["status"] == "ready_for_operator_review"
    status = service.status()
    assert status["latest_handoff"]["status"] == "ready_for_operator_review"
    assert status["latest_handoff"]["handoff_id"] == result["handoff_id"]
    assert status["latest_handoff"]["package_id"] == result["package_id"]
    assert status["latest_handoff"]["broker_provider"] == "binance_usdm"
    assert status["latest_handoff"]["dualtrack_symbol"] == "GOLD"
    assert status["latest_handoff"]["evidence_count"] == 6
    assert status["latest_handoff"]["post_apply_validation_count"] == 4
    assert status["latest_handoff"]["writes_runtime_config"] is False
    assert status["latest_handoff"]["submits_orders"] is False


def test_connector_config_apply_requires_ack_for_write(tmp_path: Path) -> None:
    plan, output_root, pipeline_path, dualtrack_path = _activation_plan(tmp_path)

    result = ConnectorConfigApply(
        output_root,
        pipeline_config_path=pipeline_path,
        dualtrack_config_path=dualtrack_path,
    ).apply(
        {
            "activation_plan": plan,
            "write": True,
            "accept_warnings": True,
            "acknowledgement": "wrong",
            "package_id": plan["config_apply_package"]["package_id"],
        }
    )

    assert result["status"] == "blocked"
    assert {row["name"] for row in result["blockers"]} == {"operator_acknowledgement"}
    assert result["safety"]["writes_runtime_config"] is False
    assert load_json_yaml(pipeline_path)["broker"]["provider"] == "binance_usdm"


def test_connector_config_apply_requires_package_id_for_write(tmp_path: Path) -> None:
    plan, output_root, pipeline_path, dualtrack_path = _activation_plan(tmp_path)

    result = ConnectorConfigApply(
        output_root,
        pipeline_config_path=pipeline_path,
        dualtrack_config_path=dualtrack_path,
    ).apply(
        {
            "activation_plan": plan,
            "write": True,
            "accept_warnings": True,
            "acknowledgement": plan["config_apply_package"]["operator_acknowledgement"],
        }
    )

    assert result["status"] == "blocked"
    assert {row["name"] for row in result["blockers"]} == {"config_apply_package_id_required"}
    assert result["safety"]["writes_runtime_config"] is False
    assert load_json_yaml(pipeline_path)["broker"]["provider"] == "binance_usdm"


def test_connector_config_apply_blocks_mismatched_package_id(tmp_path: Path) -> None:
    plan, output_root, pipeline_path, dualtrack_path = _activation_plan(tmp_path)

    result = ConnectorConfigApply(
        output_root,
        pipeline_config_path=pipeline_path,
        dualtrack_config_path=dualtrack_path,
    ).apply(
        {
            "activation_plan": plan,
            "write": True,
            "accept_warnings": True,
            "acknowledgement": plan["config_apply_package"]["operator_acknowledgement"],
            "package_id": "stale-package-id",
        }
    )

    assert result["status"] == "blocked"
    assert {row["name"] for row in result["blockers"]} == {"config_apply_package_id_mismatch"}
    assert result["safety"]["writes_runtime_config"] is False
    assert load_json_yaml(pipeline_path)["broker"]["provider"] == "binance_usdm"


def test_connector_config_apply_requires_current_handoff_for_write(tmp_path: Path) -> None:
    plan, output_root, pipeline_path, dualtrack_path = _activation_plan(tmp_path)
    service = ConnectorConfigApply(output_root, pipeline_config_path=pipeline_path, dualtrack_config_path=dualtrack_path)
    service.apply({"activation_plan": plan})
    service.check_plan({"activation_plan": plan})

    result = service.apply(
        {
            "activation_plan": plan,
            "write": True,
            "accept_warnings": True,
            "acknowledgement": plan["config_apply_package"]["operator_acknowledgement"],
            "package_id": plan["config_apply_package"]["package_id"],
        }
    )

    assert result["status"] == "blocked"
    assert {row["name"] for row in result["blockers"]} == {"config_handoff_missing"}
    assert result["safety"]["writes_runtime_config"] is False
    assert load_json_yaml(pipeline_path)["broker"]["provider"] == "binance_usdm"


def test_connector_config_apply_requires_current_rehearsal_for_write(tmp_path: Path) -> None:
    plan, output_root, pipeline_path, dualtrack_path = _activation_plan(tmp_path)
    service = ConnectorConfigApply(output_root, pipeline_config_path=pipeline_path, dualtrack_config_path=dualtrack_path)
    service.apply({"activation_plan": plan})
    service.check_plan({"activation_plan": plan})
    service.handoff({"activation_plan": plan})

    result = service.apply(
        {
            "activation_plan": plan,
            "write": True,
            "accept_warnings": True,
            "acknowledgement": plan["config_apply_package"]["operator_acknowledgement"],
            "package_id": plan["config_apply_package"]["package_id"],
        }
    )

    assert result["status"] == "blocked"
    assert {row["name"] for row in result["blockers"]} == {"config_rehearsal_missing"}
    assert result["safety"]["writes_runtime_config"] is False
    assert load_json_yaml(pipeline_path)["broker"]["provider"] == "binance_usdm"


def test_connector_config_apply_blocks_stale_handoff_config_fingerprint(tmp_path: Path) -> None:
    plan, output_root, pipeline_path, dualtrack_path = _activation_plan(tmp_path)
    service = ConnectorConfigApply(output_root, pipeline_config_path=pipeline_path, dualtrack_config_path=dualtrack_path)
    service.apply({"activation_plan": plan})
    service.check_plan({"activation_plan": plan})
    service.handoff({"activation_plan": plan})
    pipeline = load_json_yaml(pipeline_path)
    pipeline["broker"]["dry_run"] = False
    _write_config(pipeline_path, pipeline)

    result = service.apply(
        {
            "activation_plan": plan,
            "write": True,
            "accept_warnings": True,
            "acknowledgement": plan["config_apply_package"]["operator_acknowledgement"],
            "package_id": plan["config_apply_package"]["package_id"],
        }
    )

    assert result["status"] == "blocked"
    assert {row["name"] for row in result["blockers"]} == {"config_handoff_pipeline_config_stale"}
    assert result["safety"]["writes_runtime_config"] is False
    assert load_json_yaml(pipeline_path)["broker"]["provider"] == "binance_usdm"
    assert load_json_yaml(pipeline_path)["broker"]["dry_run"] is False


def test_connector_config_apply_blocks_stale_rehearsal_config_fingerprint(tmp_path: Path) -> None:
    plan, output_root, pipeline_path, dualtrack_path = _activation_plan(tmp_path)
    service = ConnectorConfigApply(output_root, pipeline_config_path=pipeline_path, dualtrack_config_path=dualtrack_path)
    service.apply({"activation_plan": plan})
    service.check_plan({"activation_plan": plan})
    service.handoff({"activation_plan": plan})
    service.rehearse({"activation_plan": plan})
    pipeline = load_json_yaml(pipeline_path)
    pipeline["broker"]["dry_run"] = False
    _write_config(pipeline_path, pipeline)
    service.handoff({"activation_plan": plan})

    result = service.apply(
        {
            "activation_plan": plan,
            "write": True,
            "accept_warnings": True,
            "acknowledgement": plan["config_apply_package"]["operator_acknowledgement"],
            "package_id": plan["config_apply_package"]["package_id"],
        }
    )

    assert result["status"] == "blocked"
    assert {row["name"] for row in result["blockers"]} == {"config_rehearsal_runtime_config_stale"}
    assert result["safety"]["writes_runtime_config"] is False
    assert load_json_yaml(pipeline_path)["broker"]["provider"] == "binance_usdm"
    assert load_json_yaml(pipeline_path)["broker"]["dry_run"] is False


def test_connector_config_check_does_not_self_invalidate_handoff_for_write(tmp_path: Path) -> None:
    plan, output_root, pipeline_path, dualtrack_path = _activation_plan(tmp_path)
    service = ConnectorConfigApply(output_root, pipeline_config_path=pipeline_path, dualtrack_config_path=dualtrack_path)
    service.apply({"activation_plan": plan})
    service.check_plan({"activation_plan": plan})
    service.handoff({"activation_plan": plan})
    service.rehearse({"activation_plan": plan})

    check = service.check_plan({"activation_plan": plan})
    result = service.apply(
        {
            "activation_plan": plan,
            "write": True,
            "accept_warnings": True,
            "acknowledgement": plan["config_apply_package"]["operator_acknowledgement"],
            "package_id": plan["config_apply_package"]["package_id"],
        }
    )

    assert check["status"] == "ready_for_attended_config_write"
    assert check["write_preflight"]["status"] == "ready"
    assert result["status"] == "applied"
    assert result["safety"]["writes_runtime_config"] is True
    assert load_json_yaml(pipeline_path)["broker"]["provider"] == "tiger_openapi"


def test_connector_config_apply_recomputes_package_id_from_plan_contents(tmp_path: Path) -> None:
    plan, output_root, pipeline_path, dualtrack_path = _activation_plan(tmp_path)
    tampered = json.loads(json.dumps(plan))
    tampered["config_patch_preview"][0]["planned"] = False

    result = ConnectorConfigApply(
        output_root,
        pipeline_config_path=pipeline_path,
        dualtrack_config_path=dualtrack_path,
    ).apply(
        {
            "activation_plan": tampered,
            "write": True,
            "accept_warnings": True,
            "acknowledgement": tampered["config_apply_package"]["operator_acknowledgement"],
            "package_id": tampered["config_apply_package"]["package_id"],
        }
    )

    assert result["status"] == "blocked"
    assert {row["name"] for row in result["blockers"]} == {"config_apply_package_id_stale"}
    assert result["safety"]["writes_runtime_config"] is False
    assert load_json_yaml(pipeline_path)["broker"]["provider"] == "binance_usdm"


def test_connector_config_apply_writes_pipeline_and_dualtrack_with_backup(tmp_path: Path) -> None:
    plan, output_root, pipeline_path, dualtrack_path = _activation_plan(tmp_path)
    ack = plan["config_apply_package"]["operator_acknowledgement"]
    package_id = plan["config_apply_package"]["package_id"]
    service = ConnectorConfigApply(output_root, pipeline_config_path=pipeline_path, dualtrack_config_path=dualtrack_path)
    service.apply({"activation_plan": plan})
    service.check_plan({"activation_plan": plan})
    service.handoff({"activation_plan": plan})
    service.rehearse({"activation_plan": plan})

    result = service.apply(
        {
            "activation_plan": plan,
            "write": True,
            "accept_warnings": True,
            "acknowledgement": ack,
            "package_id": package_id,
        }
    )

    assert result["status"] == "applied"
    assert result["mode"] == "write"
    assert result["package_id"] == package_id
    assert result["backup"]["created"] is True
    assert len(result["backup"]["files"]) == 2
    assert result["rollback"]["available"] is True
    assert result["safety"]["writes_runtime_config"] is True
    assert result["safety"]["opens_network_clients"] is False
    assert result["safety"]["submits_orders"] is False

    pipeline = load_json_yaml(pipeline_path)
    assert pipeline["tiger_futures_feed"]["enabled"] is True
    assert pipeline["broker"]["provider"] == "tiger_openapi"
    assert pipeline["broker"]["profile"] == "tiger_openapi_paper"
    assert pipeline["broker"]["environment"] == "paper"
    assert pipeline["broker"]["dry_run"] is True
    assert "api_key_env" not in pipeline["broker"]
    assert pipeline["demo_trading"]["broker_profile"] == "tiger_openapi_paper"

    dualtrack = load_json_yaml(dualtrack_path)
    assert dualtrack["market_data"]["symbol"] == "MGCmain"
    assert dualtrack["market_data"]["provider"] == "tiger_openapi:COMEX"
    assert dualtrack["market_session"]["enabled"] is True
    assert dualtrack["market_session"]["venue"] == "comex_futures"
    assert dualtrack["execution_cost_model"]["venue"] == "tiger_mgc"
    assert dualtrack["execution_cost_model"]["quantity_mode"] == "integer_contracts"
    assert dualtrack["execution_cost_model"]["contract_multiplier"] == 10
    assert dualtrack["execution_cost_model"]["contracts_per_rung"] == 1
    assert dualtrack["grid"]["max_rungs"] == 2
    assert dualtrack["human_fill_sync"]["enabled"] is True
    assert dualtrack["human_fill_sync"]["provider"] == "tiger_openapi"
    assert dualtrack["human_fill_sync"]["refresh_order_sync_before_import"] is True


def test_connector_config_apply_rolls_back_from_backup(tmp_path: Path) -> None:
    plan, output_root, pipeline_path, dualtrack_path = _activation_plan(tmp_path)
    ack = plan["config_apply_package"]["operator_acknowledgement"]
    package_id = plan["config_apply_package"]["package_id"]
    service = ConnectorConfigApply(output_root, pipeline_config_path=pipeline_path, dualtrack_config_path=dualtrack_path)
    original_pipeline = load_json_yaml(pipeline_path)
    original_dualtrack = load_json_yaml(dualtrack_path)
    service.apply({"activation_plan": plan})
    service.check_plan({"activation_plan": plan})
    service.handoff({"activation_plan": plan})
    service.rehearse({"activation_plan": plan})
    applied = service.apply(
        {
            "activation_plan": plan,
            "write": True,
            "accept_warnings": True,
            "acknowledgement": ack,
            "package_id": package_id,
        }
    )

    result = service.rollback({"apply_receipt": applied, "acknowledgement": ROLLBACK_ACKNOWLEDGEMENT})

    assert result["status"] == "rolled_back"
    assert result["safety"]["writes_runtime_config"] is True
    assert load_json_yaml(pipeline_path) == original_pipeline
    assert load_json_yaml(dualtrack_path) == original_dualtrack
    assert load_json(output_root / "connector_config_apply" / "rollback" / "current.json")[-1]["status"] == "rolled_back"


def test_connector_config_apply_blocks_blocked_activation_plan(tmp_path: Path) -> None:
    output_root = tmp_path / "outputs"
    pipeline_path = tmp_path / "configs" / "pipeline.yaml"
    dualtrack_path = tmp_path / "configs" / "dualtrack.yaml"
    _write_config(pipeline_path, _config())
    _write_config(dualtrack_path, _dualtrack_config())
    blocked_plan = {
        "schema_version": "connector-activation-plan-v1",
        "status": "blocked",
        "connector_id": "tiger_openapi",
        "requested_roles": ["price_feed"],
        "blockers": [{"name": "role:price_feed", "summary": "blocked"}],
        "config_apply_package": {
            "operator_acknowledgement": "I_UNDERSTAND_CONNECTOR_CONFIG_WRITE_IS_SEPARATE_AND_REVERSIBLE"
        },
    }

    result = ConnectorConfigApply(
        output_root,
        pipeline_config_path=pipeline_path,
        dualtrack_config_path=dualtrack_path,
    ).apply({"activation_plan": blocked_plan, "write": True, "acknowledgement": blocked_plan["config_apply_package"]["operator_acknowledgement"]})

    assert result["status"] == "blocked"
    assert any(row["name"] == "activation_plan_blocked" for row in result["blockers"])
    assert result["safety"]["writes_runtime_config"] is False
