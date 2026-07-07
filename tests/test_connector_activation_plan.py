from __future__ import annotations

import json
from pathlib import Path

import pytest

from services.connector_activation_plan import ConnectorActivationPlan
from services.journal_store import load_json, write_json


def _config(props_env: str = "TIGER_OPENAPI_CONFIG_PATH") -> dict:
    return {
        "output_root": "outputs",
        "local_market_db": "data/market_data.db",
        "execution_mode": "paper",
        "live_trading_enabled": False,
        "broker": {
            "provider": "binance_usdm",
            "dry_run": True,
            "environment": "demo",
            "base_url": "https://demo-fapi.binance.com",
            "api_key_env": "BINANCE_API_KEY",
            "api_secret_env": "BINANCE_API_SECRET",
            "allowed_symbols": ["GOLD", "XAUUSD"],
            "instrument_map": {"GOLD": "XAUUSDT"},
            "request_dir": "live_order_requests",
        },
        "demo_trading": {
            "enabled": True,
            "active_strategy_id": "gold_1m_macd",
            "broker_profile": "binance_usdm",
        },
        "tiger_futures_feed": {
            "enabled": False,
            "props_path_env": props_env,
            "contract": "MGCmain",
            "output_symbol": "MGCmain",
            "period": "1m",
        },
        "binance_usdm_1m_feed": {"symbol": "XAUUSDT", "output_symbol": "GOLD", "interval": "1m"},
        "gold_5m_backfill": {"provider": "yahoo_chart"},
        "broker_profiles": {
            "tiger_openapi_paper": {
                "provider": "tiger_openapi",
                "environment": "paper",
                "dry_run": True,
                "props_path_env": props_env,
                "allowed_symbols": ["MGCmain", "MGC2608"],
            },
            "binance_usdm": {
                "provider": "binance_usdm",
                "environment": "demo",
                "api_key_env": "BINANCE_API_KEY",
                "api_secret_env": "BINANCE_API_SECRET",
            },
        },
    }


def _dualtrack_config() -> dict:
    return {
        "capital_per_track_usd": 10_000,
        "max_leverage": 10,
        "market_data": {"symbol": "GOLD", "timeframe": "1m", "provider": ""},
        "market_session": {"enabled": False, "venue": "", "timezone": "UTC"},
        "human_fill_sync": {
            "enabled": False,
            "provider": "",
            "run_before_close": True,
            "refresh_order_sync_before_import": False,
            "require_success_before_close": True,
        },
        "grid": {"max_rungs": None},
        "cost_per_side_bp": 0.5,
    }


def _write_price_feed_acceptance_pending(root: Path) -> None:
    next_window = {
        "start": "2026-07-05T22:00:00+00:00",
        "end": "2026-07-06T21:00:00+00:00",
        "trading_date": "2026-07-06",
    }
    write_json(
        root / "tiger_price_feed_acceptance" / "current.json",
        [
            {
                "status": "pending_market_open",
                "ready_for_price_feed": False,
                "exit_code": 75,
                "blockers": [{"name": "realtime_market_hours_gate"}],
                "checked_at": "2026-07-05T16:18:30+00:00",
                "operator_next_action": {
                    "status": "waiting_market_open",
                    "summary": "Wait until 2026-07-05T22:00:00+00:00 to rerun Tiger price-feed acceptance.",
                    "next_command": "python3 -m pipelines.tiger_price_feed_acceptance --date 2026-07-05 --contract MGCmain --poll-seconds 75 --json",
                    "next_trading_window": next_window,
                },
                "steps": {
                    "realtime_validation": {
                        "market_hours_gate": {
                            "operator_action": "rerun_after_next_trading_window",
                            "next_trading_window": next_window,
                        }
                    }
                },
            }
        ],
    )


def test_connector_activation_plan_previews_tiger_feed_and_broker_without_writing_config(monkeypatch, tmp_path: Path):
    props = tmp_path / "tiger_openapi_config.properties"
    props.write_text("placeholder only\n", encoding="utf-8")
    props.chmod(0o600)
    monkeypatch.setenv("TIGER_OPENAPI_CONFIG_PATH", str(props))
    output_root = tmp_path / "outputs"
    write_json(
        output_root / "tiger_price_feed_readiness" / "current.json",
        [{"status": "ready_for_price_feed", "ready_for_price_feed": True, "blockers": []}],
    )

    result = ConnectorActivationPlan(output_root, config=_config(), dualtrack_runtime_config=_dualtrack_config()).evaluate(
        {
            "connector_id": "tiger_openapi",
            "requested_roles": ["price_feed", "broker_order"],
            "environment": "paper",
            "credential_confirmations": {
                "TIGER_OPENAPI_CONFIG_PATH": {"present": True, "file_exists": True, "owner_only": True}
            },
        }
    )

    assert result["status"] == "preview_ready"
    assert result["safety"]["writes_runtime_config"] is False
    assert result["safety"]["opens_network_clients"] is False
    assert result["safety"]["submits_orders"] is False
    assert result["activation_gate"]["status"] == "preview_ready_with_warnings"
    assert result["activation_gate"]["operator_status"] == "operator_warning_review"
    assert result["activation_gate"]["can_apply_config_from_this_endpoint"] is False
    assert result["activation_gate"]["can_enable_broker_orders_from_this_gate"] is False
    assert result["activation_runbook"]["status"] == "operator_review_required"
    assert result["activation_runbook"]["phase_count"] == 4
    assert result["activation_runbook"]["phases"][0]["name"] == "review_warnings"
    assert result["activation_runbook"]["phases"][1]["name"] == "config_apply"
    assert result["activation_runbook"]["phases"][1]["status"] == "not_included"
    assert result["activation_runbook"]["safety"]["writes_runtime_config"] is False
    assert result["activation_runbook"]["safety"]["opens_network_clients"] is False
    assert result["activation_runbook"]["safety"]["submits_orders"] is False
    assert result["config_apply_package"]["status"] == "operator_review_required"
    assert len(result["config_apply_package"]["package_id"]) == 16
    assert result["config_apply_package"]["package_id"] in result["config_apply_package"]["attended_apply_command"]
    package_changes = {item["path"]: item for item in result["config_apply_package"]["patch_digest"]}
    assert package_changes["tiger_futures_feed.enabled"]["current"] is False
    assert package_changes["tiger_futures_feed.enabled"]["planned"] is True
    assert "reason" in package_changes["tiger_futures_feed.enabled"]
    assert result["config_apply_package"]["change_count"] == len(result["config_patch_preview"])
    assert result["config_apply_package"]["operator_acknowledgement_required"] is True
    assert result["config_apply_package"]["config_write_boundary"]["can_apply_from_this_endpoint"] is False
    assert result["config_apply_package"]["config_write_boundary"]["requires_backup_before_write"] is True
    assert result["config_apply_package"]["config_write_boundary"]["requires_package_id"] is True
    assert result["config_apply_package"]["config_write_boundary"]["expected_package_id"] == result["config_apply_package"]["package_id"]
    assert result["config_apply_package"]["rollback"]["required"] is True
    assert result["config_apply_package"]["safety"]["writes_runtime_config"] is False
    assert result["config_apply_package"]["safety"]["opens_network_clients"] is False
    assert result["config_apply_package"]["safety"]["submits_orders"] is False
    assert result["switch_audit"]["status"] == "operator_review_required"
    assert result["switch_audit"]["check_count"] == 6
    assert result["switch_audit"]["can_switch_connector_from_this_endpoint"] is False
    assert result["switch_audit"]["can_apply_config_now"] is False
    assert result["switch_audit"]["can_enable_broker_orders"] is False
    assert result["switch_audit"]["requires_separate_config_write_milestone"] is True
    assert result["switch_audit"]["requires_rollback_evidence"] is True
    changes = {item["path"]: item for item in result["config_patch_preview"]}
    assert changes["tiger_futures_feed.enabled"]["planned"] is True
    assert changes["broker.provider"]["planned"] == "tiger_openapi"
    assert changes["broker.profile"]["planned"] == "tiger_openapi_paper"
    assert changes["broker.dry_run"]["planned"] is True
    assert changes["demo_trading.broker_profile"]["planned"] == "tiger_openapi_paper"
    assert changes["broker.allowed_symbols"]["op"] == "remove"
    assert all(item["writes_config"] is False for item in result["config_patch_preview"])
    profile = result["dualtrack_profile_preview"]
    assert profile["status"] == "operator_review_required"
    assert profile["profile_id"] == "tiger_mgc_dualtrack_paper"
    assert profile["config_file"] == "configs/dualtrack.yaml"
    assert profile["safety"]["writes_runtime_config"] is False
    assert profile["safety"]["opens_network_clients"] is False
    assert profile["safety"]["submits_orders"] is False
    profile_changes = {item["path"]: item for item in profile["dualtrack_config_patch_preview"]}
    assert profile_changes["market_data.symbol"]["planned"] == "MGCmain"
    assert profile_changes["market_data.provider"]["planned"] == "tiger_openapi:COMEX"
    assert profile_changes["market_session.enabled"]["planned"] is True
    assert profile_changes["market_session.venue"]["planned"] == "comex_futures"
    assert profile_changes["execution_cost_model.venue"]["planned"] == "tiger_mgc"
    assert profile_changes["execution_cost_model.quantity_mode"]["planned"] == "integer_contracts"
    assert profile_changes["execution_cost_model.contract_multiplier"]["planned"] == 10
    assert profile_changes["execution_cost_model.contracts_per_rung"]["planned"] == 1
    assert profile_changes["grid.max_rungs"]["planned"] == 2
    assert profile_changes["human_fill_sync.enabled"]["planned"] is True
    assert profile_changes["human_fill_sync.provider"]["planned"] == "tiger_openapi"
    assert profile_changes["human_fill_sync.refresh_order_sync_before_import"]["planned"] is True
    assert all(item["writes_config"] is False for item in profile["dualtrack_config_patch_preview"])
    assert result["config_apply_package"]["dualtrack_profile"]["status"] == "operator_review_required"
    assert result["config_apply_package"]["dualtrack_profile"]["requires_strategy_edge_approval"] is True
    assert result["config_apply_package"]["dualtrack_profile"]["can_enable_broker_orders_from_this_profile"] is False
    assert result["config_apply_package"]["dualtrack_change_count"] == len(profile["dualtrack_config_patch_preview"])
    assert result["config_apply_package"]["config_write_boundary"]["dualtrack_paths_previewed"]
    assert result["config_apply_package"]["rollback"]["dualtrack_paths_previewed"]
    switch_checks = {item["name"]: item for item in result["switch_audit"]["checks"]}
    assert switch_checks["dualtrack_profile"]["status"] == "operator_review_required"
    assert {item["name"] for item in result["warnings"]} == {"paper_network_order_not_armed"}
    assert str(props) not in json.dumps(result)
    current = load_json(output_root / "connector_activation_plan" / "current.json")[-1]
    assert current["connector_id"] == "tiger_openapi"


def test_connector_activation_plan_surfaces_tiger_acceptance_pending_market_open(monkeypatch, tmp_path: Path):
    props = tmp_path / "tiger_openapi_config.properties"
    props.write_text("placeholder only\n", encoding="utf-8")
    props.chmod(0o600)
    monkeypatch.setenv("TIGER_OPENAPI_CONFIG_PATH", str(props))
    output_root = tmp_path / "outputs"
    _write_price_feed_acceptance_pending(output_root)

    result = ConnectorActivationPlan(output_root, config=_config(), dualtrack_runtime_config=_dualtrack_config()).evaluate(
        {
            "connector_id": "tiger_openapi",
            "requested_roles": ["price_feed"],
            "environment": "paper",
            "credential_confirmations": {
                "TIGER_OPENAPI_CONFIG_PATH": {"present": True, "file_exists": True, "owner_only": True}
            },
        }
    )

    assert result["status"] == "blocked"
    role_blocker = next(item for item in result["blockers"] if item["name"] == "role:price_feed")
    assert "waiting for Tiger market-hours acceptance" in role_blocker["summary"]
    warning = next(item for item in result["warnings"] if item["name"] == "tiger_price_feed_acceptance_not_ready")
    assert warning["summary"] == "Wait until 2026-07-05T22:00:00+00:00 to rerun Tiger price-feed acceptance."
    assert warning["evidence"]["acceptance"]["status"] == "pending_market_open"
    assert warning["evidence"]["acceptance"]["exit_code"] == 75
    assert warning["evidence"]["acceptance"]["operator_status"] == "waiting_market_open"
    assert warning["evidence"]["acceptance"]["operator_summary"] == "Wait until 2026-07-05T22:00:00+00:00 to rerun Tiger price-feed acceptance."
    assert warning["evidence"]["acceptance"]["next_command"].startswith("python3 -m pipelines.tiger_price_feed_acceptance")
    assert warning["evidence"]["acceptance"]["next_trading_window"]["start"] == "2026-07-05T22:00:00+00:00"
    assert result["activation_gate"]["status"] == "blocked"
    assert result["activation_gate"]["operator_status"] == "waiting_market_open"
    assert result["activation_gate"]["summary"] == "Wait until 2026-07-05T22:00:00+00:00 to rerun Tiger price-feed acceptance."
    assert result["activation_gate"]["next_command"].startswith("python3 -m pipelines.tiger_price_feed_acceptance")
    assert result["activation_gate"]["can_apply_config_from_this_endpoint"] is False
    assert result["activation_gate"]["can_enable_broker_orders_from_this_gate"] is False
    assert result["activation_gate"]["writes_runtime_config"] is False
    assert result["activation_gate"]["opens_network_clients"] is False
    assert result["activation_gate"]["submits_orders"] is False
    assert result["activation_runbook"]["status"] == "blocked"
    assert result["activation_runbook"]["first_action"] == "Wait until 2026-07-05T22:00:00+00:00 to rerun Tiger price-feed acceptance."
    assert result["activation_runbook"]["phases"][0]["name"] == "resolve_activation_gate"
    assert result["activation_runbook"]["phases"][0]["commands"] == [
        "python3 -m pipelines.tiger_price_feed_acceptance --date 2026-07-05 --contract MGCmain --poll-seconds 75 --json"
    ]
    assert result["activation_runbook"]["phases"][2]["name"] == "post_apply_validation"
    assert "python3 -m pipelines.connector_catalog --json" in result["activation_runbook"]["phases"][2]["commands"]
    assert result["activation_runbook"]["safety"]["can_apply_config_from_this_endpoint"] is False
    assert result["activation_runbook"]["safety"]["can_enable_broker_orders_from_this_gate"] is False
    assert result["config_apply_package"]["status"] == "blocked"
    assert result["config_apply_package"]["summary"] == "Wait until 2026-07-05T22:00:00+00:00 to rerun Tiger price-feed acceptance."
    assert result["config_apply_package"]["pre_apply_checks"][0]["status"] == "blocked"
    assert result["config_apply_package"]["pre_apply_checks"][1]["status"] == "blocked"
    assert result["config_apply_package"]["config_write_boundary"]["requires_separate_config_write_milestone"] is True
    assert result["config_apply_package"]["config_write_boundary"]["can_apply_from_this_endpoint"] is False
    assert result["config_apply_package"]["safety"]["stores_credentials"] is False
    assert result["config_apply_package"]["safety"]["credential_values_exposed"] is False
    assert result["config_apply_package"]["safety"]["can_enable_broker_orders_from_this_gate"] is False
    assert result["switch_audit"]["status"] == "blocked"
    assert result["switch_audit"]["next_action_summary"] == "Wait until 2026-07-05T22:00:00+00:00 to rerun Tiger price-feed acceptance."
    assert result["switch_audit"]["next_command"].startswith("python3 -m pipelines.tiger_price_feed_acceptance")
    assert result["switch_audit"]["check_count"] == 5
    checks = {item["name"]: item for item in result["switch_audit"]["checks"]}
    assert checks["activation_gate"]["status"] == "blocked"
    assert checks["config_apply_package"]["status"] == "blocked"
    assert checks["dualtrack_profile"]["status"] == "blocked"
    assert result["dualtrack_profile_preview"]["status"] == "blocked"
    assert result["dualtrack_profile_preview"]["runtime_effect"]["human_fill_sync_before_close"] == "not_requested"
    profile_changes = {item["path"]: item for item in result["dualtrack_profile_preview"]["dualtrack_config_patch_preview"]}
    assert "human_fill_sync.enabled" not in profile_changes
    assert profile_changes["market_data.symbol"]["planned"] == "MGCmain"
    assert profile_changes["execution_cost_model.venue"]["planned"] == "tiger_mgc"
    assert result["switch_audit"]["can_switch_connector_from_this_endpoint"] is False
    assert result["switch_audit"]["can_apply_config_now"] is False
    assert result["switch_audit"]["can_enable_broker_orders"] is False
    assert result["switch_audit"]["safety"]["writes_runtime_config"] is False
    assert result["switch_audit"]["safety"]["opens_network_clients"] is False
    assert result["switch_audit"]["safety"]["submits_orders"] is False
    assert result["safety"]["writes_runtime_config"] is False
    assert result["safety"]["opens_network_clients"] is False
    assert str(props) not in json.dumps(result)


def test_connector_activation_plan_blocks_when_tiger_credentials_are_missing(monkeypatch, tmp_path: Path):
    monkeypatch.delenv("TIGER_OPENAPI_CONFIG_PATH", raising=False)

    result = ConnectorActivationPlan(tmp_path / "outputs", config=_config()).evaluate(
        {"connector_id": "tiger_openapi", "requested_roles": ["price_feed"], "credential_confirmations": {}}
    )

    assert result["status"] == "blocked"
    assert {item["name"] for item in result["blockers"]} == {
        "role:price_feed",
        "credential:TIGER_OPENAPI_CONFIG_PATH",
    }
    assert result["safety"]["writes_runtime_config"] is False


def test_connector_activation_plan_rejects_raw_secret_payload(tmp_path: Path):
    service = ConnectorActivationPlan(tmp_path / "outputs", config=_config())

    with pytest.raises(ValueError, match="raw credential field"):
        service.evaluate({"connector_id": "tiger_openapi", "api_secret": "should-not-be-sent"})

    assert not (tmp_path / "outputs" / "connector_activation_plan" / "current.json").exists()


def test_connector_activation_dashboard_endpoint_is_not_dualtrack_control(monkeypatch, tmp_path: Path):
    from pipelines import dashboard_server

    class FakeConnectorActivationPlan:
        def __init__(self, output_root=None):
            self.output_root = output_root

        def evaluate(self, payload):
            return {"status": "preview_ready", "connector_id": payload["connector_id"], "change_count": 1}

    monkeypatch.setattr(dashboard_server, "ConnectorActivationPlan", FakeConnectorActivationPlan)

    response = dashboard_server.build_connector_activation_plan_response(
        {"connector_id": "tiger_openapi"}, output_root=tmp_path / "outputs"
    )

    assert response == {"status": "preview_ready", "connector_id": "tiger_openapi", "change_count": 1}
    assert "/api/connectors/activation/plan" not in dashboard_server._DUALTRACK_POST_ENDPOINTS
