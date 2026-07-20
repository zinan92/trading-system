"""Connector control-plane response builders (config status/apply/rollback, runbooks)."""

from __future__ import annotations

from pathlib import Path

from services.config_loader import ROOT, load_pipeline_config
from services.connector_config_apply import ConnectorConfigApply
from services.journal_store import load_json


_CONNECTOR_PRICE_FEED_REFRESH_RUNBOOK_ENDPOINT_SAFETY = {
    "read_only": True,
    "generates_runbook": False,
    "opens_network_clients": False,
    "opens_quote_client": False,
    "opens_trade_client": False,
    "submits_orders": False,
    "writes_runtime_config": False,
    "credential_values_exposed": False,
    "raw_command_text_exposed": False,
}


def build_connector_config_status_response(*, output_root: Path | None = None) -> dict:
    return ConnectorConfigApply(output_root=output_root).status()


def build_connector_price_feed_refresh_runbook_response(*, output_root: Path | None = None) -> dict:
    config = load_pipeline_config()
    root = Path(output_root) if output_root else ROOT / str(config.get("output_root", "outputs"))
    path = root / "connector_config_apply" / "price_feed_refresh_runbook_current.json"
    rows = load_json(path)
    if rows and isinstance(rows[-1], dict):
        receipt = dict(rows[-1])
        receipt.setdefault("served_from", str(path))
        receipt["command_sequence"] = _redacted_runbook_steps(receipt.get("command_sequence"))
        receipt["status_receipt"] = _redacted_connector_status_snapshot(receipt.get("status_receipt"))
        receipt["redaction"] = {
            "raw_command_text_exposed": False,
            "credential_values_exposed": False,
        }
        receipt["endpoint_safety"] = dict(_CONNECTOR_PRICE_FEED_REFRESH_RUNBOOK_ENDPOINT_SAFETY)
        return receipt
    return {
        "schema_version": "connector-price-feed-refresh-runbook-v1",
        "status": "missing",
        "served_from": str(path),
        "command_sequence": [],
        "safety": {
            "read_only": True,
            "opens_network_clients": False,
            "opens_quote_client": False,
            "opens_trade_client": False,
            "submits_orders": False,
            "writes_runtime_config": False,
            "credential_values_exposed": False,
        },
        "endpoint_safety": dict(_CONNECTOR_PRICE_FEED_REFRESH_RUNBOOK_ENDPOINT_SAFETY),
    }


def _redacted_runbook_steps(rows: object) -> list[dict]:
    steps = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        steps.append(
            {
                "name": str(row.get("name") or ""),
                "purpose": str(row.get("purpose") or ""),
                "opens_quote_client": row.get("opens_quote_client") is True,
                "opens_trade_client": row.get("opens_trade_client") is True,
                "submits_orders": row.get("submits_orders") is True,
                "writes_runtime_config": row.get("writes_runtime_config") is True,
                "writes_market_db": row.get("writes_market_db") is True,
                "writes_plan_artifact": row.get("writes_plan_artifact") is True,
            }
        )
    return steps


def _redacted_connector_status_snapshot(status: object) -> dict:
    if not isinstance(status, dict):
        return {}
    operator_stage = status.get("operator_stage", {}) if isinstance(status.get("operator_stage"), dict) else {}
    return {
        "schema_version": str(status.get("schema_version") or ""),
        "checked_at": str(status.get("checked_at") or ""),
        "status": str(status.get("status") or ""),
        "operator_stage": {
            "stage": str(operator_stage.get("stage") or ""),
            "next_action": str(operator_stage.get("next_action") or ""),
            "runtime_switched_to_tiger_mgc": operator_stage.get("runtime_switched_to_tiger_mgc") is True,
            "price_feed_ready": operator_stage.get("price_feed_ready") is True,
            "can_switch_config_with_operator_authorization": operator_stage.get("can_switch_config_with_operator_authorization") is True,
            "can_trade_machine_track": operator_stage.get("can_trade_machine_track") is True,
            "can_submit_tiger_orders": operator_stage.get("can_submit_tiger_orders") is True,
        },
    }


def build_connector_config_apply_response(
    payload: dict,
    *,
    output_root: Path | None = None,
    pipeline_config_path: Path | None = None,
    dualtrack_config_path: Path | None = None,
) -> dict:
    return ConnectorConfigApply(
        output_root=output_root,
        pipeline_config_path=pipeline_config_path,
        dualtrack_config_path=dualtrack_config_path,
    ).apply(payload)


def build_connector_config_rollback_response(
    payload: dict,
    *,
    output_root: Path | None = None,
    pipeline_config_path: Path | None = None,
    dualtrack_config_path: Path | None = None,
) -> dict:
    return ConnectorConfigApply(
        output_root=output_root,
        pipeline_config_path=pipeline_config_path,
        dualtrack_config_path=dualtrack_config_path,
    ).rollback(payload)
