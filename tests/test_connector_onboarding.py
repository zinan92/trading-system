from __future__ import annotations

import json
from pathlib import Path

import pytest

from services.connector_onboarding import ConnectorOnboardingDryRun
from services.journal_store import load_json, write_json


def _config(props_env: str = "TIGER_OPENAPI_CONFIG_PATH") -> dict:
    return {
        "output_root": "outputs",
        "broker": {"profile": "tiger_openapi_paper"},
        "tiger_futures_feed": {"props_path_env": props_env, "contract": "MGCmain", "output_symbol": "MGCmain", "period": "1m"},
        "binance_usdm_1m_feed": {"symbol": "XAUUSDT", "output_symbol": "GOLD", "interval": "1m"},
        "broker_profiles": {
            "tiger_openapi_paper": {
                "provider": "tiger_openapi",
                "environment": "paper",
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


def test_connector_onboarding_dry_run_ready_for_tiger_without_secret_values(monkeypatch, tmp_path: Path):
    props = tmp_path / "tiger_openapi_config.properties"
    props.write_text("placeholder only\n", encoding="utf-8")
    props.chmod(0o600)
    monkeypatch.setenv("TIGER_OPENAPI_CONFIG_PATH", str(props))
    output_root = tmp_path / "outputs"
    write_json(
        output_root / "tiger_price_feed_readiness" / "current.json",
        [{"status": "ready_for_price_feed", "ready_for_price_feed": True, "blockers": []}],
    )

    result = ConnectorOnboardingDryRun(output_root, config=_config()).evaluate(
        {
            "connector_id": "tiger_openapi",
            "requested_roles": ["price_feed", "broker_order"],
            "environment": "paper",
            "credential_confirmations": {
                "TIGER_OPENAPI_CONFIG_PATH": {"present": True, "file_exists": True, "owner_only": True}
            },
        }
    )

    assert result["status"] == "ready_for_operator_setup"
    assert result["safety"]["stores_credentials"] is False
    assert result["safety"]["opens_network_clients"] is False
    assert result["credential_requirements"][0]["name"] == "TIGER_OPENAPI_CONFIG_PATH"
    assert str(props) not in json.dumps(result)
    current = load_json(output_root / "connector_onboarding" / "current.json")[-1]
    assert current["connector_id"] == "tiger_openapi"


def test_connector_onboarding_dry_run_explains_tiger_acceptance_pending_market_open(monkeypatch, tmp_path: Path):
    props = tmp_path / "tiger_openapi_config.properties"
    props.write_text("placeholder only\n", encoding="utf-8")
    props.chmod(0o600)
    monkeypatch.setenv("TIGER_OPENAPI_CONFIG_PATH", str(props))
    output_root = tmp_path / "outputs"
    _write_price_feed_acceptance_pending(output_root)

    result = ConnectorOnboardingDryRun(output_root, config=_config()).evaluate(
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
    assert role_blocker["evidence"]["acceptance"]["status"] == "pending_market_open"
    assert role_blocker["evidence"]["acceptance"]["exit_code"] == 75
    assert role_blocker["evidence"]["acceptance"]["operator_status"] == "waiting_market_open"
    assert role_blocker["evidence"]["acceptance"]["operator_summary"] == "Wait until 2026-07-05T22:00:00+00:00 to rerun Tiger price-feed acceptance."
    assert role_blocker["evidence"]["acceptance"]["next_command"].startswith("python3 -m pipelines.tiger_price_feed_acceptance")
    assert role_blocker["evidence"]["acceptance"]["next_trading_window"]["start"] == "2026-07-05T22:00:00+00:00"
    assert role_blocker["evidence"]["acceptance"]["can_enable_broker_orders_from_this_gate"] is False
    assert str(props) not in json.dumps(result)


def test_connector_onboarding_dry_run_blocks_missing_binance_credentials(monkeypatch, tmp_path: Path):
    monkeypatch.delenv("BINANCE_API_KEY", raising=False)
    monkeypatch.delenv("BINANCE_API_SECRET", raising=False)

    result = ConnectorOnboardingDryRun(tmp_path / "outputs", config=_config()).evaluate(
        {"connector_id": "binance_usdm", "requested_roles": ["broker_order"], "credential_confirmations": {}}
    )

    assert result["status"] == "blocked"
    assert {item["name"] for item in result["blockers"]} == {
        "role:broker_order",
        "credential:BINANCE_API_KEY",
        "credential:BINANCE_API_SECRET",
    }
    assert result["safety"]["stores_credentials"] is False


def test_connector_onboarding_dry_run_rejects_raw_secret_fields(tmp_path: Path):
    service = ConnectorOnboardingDryRun(tmp_path / "outputs", config=_config())

    with pytest.raises(ValueError, match="raw credential field"):
        service.evaluate({"connector_id": "tiger_openapi", "api_key": "should-not-be-sent"})

    assert not (tmp_path / "outputs" / "connector_onboarding" / "current.json").exists()


def test_connector_onboarding_dashboard_endpoint_is_not_dualtrack_control(monkeypatch, tmp_path: Path):
    from pipelines import dashboard_server

    class FakeConnectorOnboardingDryRun:
        def __init__(self, output_root=None):
            self.output_root = output_root

        def evaluate(self, payload):
            return {"status": "ready_for_operator_setup", "connector_id": payload["connector_id"]}

    monkeypatch.setattr(dashboard_server, "ConnectorOnboardingDryRun", FakeConnectorOnboardingDryRun)

    response = dashboard_server.build_connector_onboarding_dry_run_response(
        {"connector_id": "tiger_openapi"}, output_root=tmp_path / "outputs"
    )

    assert response == {"status": "ready_for_operator_setup", "connector_id": "tiger_openapi"}
    assert "/api/connectors/onboarding/dry-run" not in dashboard_server._DUALTRACK_POST_ENDPOINTS
