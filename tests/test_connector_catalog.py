from __future__ import annotations

import json
from pathlib import Path

from services.connector_catalog import ConnectorCatalog
from services.journal_store import write_json


def _config(props_env: str = "TIGER_OPENAPI_CONFIG_PATH") -> dict:
    return {
        "broker": {"profile": "tiger_openapi_paper"},
        "binance_usdm_1m_feed": {"symbol": "XAUUSDT", "output_symbol": "GOLD", "interval": "1m"},
        "gold_5m_backfill": {"yahoo_symbol": "GC=F", "range": "5d"},
        "tiger_futures_feed": {
            "props_path_env": props_env,
            "contract": "MGCmain",
            "output_symbol": "MGCmain",
            "period": "1m",
        },
        "broker_profiles": {
            "binance_usdm": {
                "provider": "binance_usdm",
                "environment": "demo",
                "api_key_env": "BINANCE_API_KEY",
                "api_secret_env": "BINANCE_API_SECRET",
            },
            "binance_usdm_testnet": {
                "provider": "binance_usdm",
                "environment": "testnet",
                "api_key_env": "BINANCE_API_KEY",
                "api_secret_env": "BINANCE_API_SECRET",
            },
            "tiger_openapi_paper": {
                "provider": "tiger_openapi",
                "environment": "paper",
                "props_path_env": props_env,
                "allowed_symbols": ["MGCmain", "MGC2608"],
            },
        },
    }


def _by_id(payload: dict) -> dict:
    return {item["id"]: item for item in payload["connectors"]}


def _write_price_feed_ready(root: Path) -> None:
    write_json(
        root / "tiger_price_feed_readiness" / "current.json",
        [
            {
                "status": "ready_for_price_feed",
                "ready_for_price_feed": True,
                "blockers": [],
                "checked_at": "2026-07-06T06:03:00+08:00",
            }
        ],
    )


def _write_price_feed_acceptance(root: Path, *, status: str = "accepted", exit_code: int = 0) -> None:
    ready = status == "accepted"
    next_window = {
        "start": "2026-07-05T22:00:00+00:00",
        "end": "2026-07-06T21:00:00+00:00",
        "trading_date": "2026-07-06",
    }
    write_json(
        root / "tiger_price_feed_acceptance" / "current.json",
        [
            {
                "schema_version": "tiger-price-feed-acceptance-v1",
                "status": status,
                "exit_code": exit_code,
                "ready_for_price_feed": ready,
                "can_enable_broker_orders_from_this_gate": False,
                "checked_at": "2026-07-05T22:03:00+00:00" if ready else "2026-07-05T16:18:30+00:00",
                "operator_next_action": {
                    "status": "accepted" if ready else "waiting_market_open",
                    "summary": "Tiger price-feed acceptance passed." if ready else "Wait until 2026-07-05T22:00:00+00:00 to rerun Tiger price-feed acceptance.",
                    "next_command": "" if ready else "python3 -m pipelines.tiger_price_feed_acceptance --date 2026-07-05 --contract MGCmain --poll-seconds 75 --json",
                    "next_trading_window": next_window,
                },
                "steps": {
                    "realtime_validation": {
                        "status": "pass" if ready else "pending_market_open",
                        "market_hours_gate": {
                            "operator_action": "passed" if ready else "rerun_after_next_trading_window",
                            "next_trading_window": next_window,
                        },
                    }
                },
                "blockers": [] if ready else [{"name": "realtime_market_hours_gate", "status": "fail"}],
            }
        ],
    )


def test_connector_catalog_reports_tiger_ready_without_exposing_props_path(monkeypatch, tmp_path: Path):
    props = tmp_path / "tiger_openapi_config.properties"
    props.write_text("tiger_id=placeholder\n", encoding="utf-8")
    props.chmod(0o600)
    monkeypatch.setenv("TIGER_OPENAPI_CONFIG_PATH", str(props))
    output_root = tmp_path / "outputs"
    _write_price_feed_ready(output_root)
    _write_price_feed_acceptance(output_root)

    payload = ConnectorCatalog(config=_config(), output_root=output_root).snapshot()
    tiger = _by_id(payload)["tiger_openapi"]
    credential = tiger["credential_requirements"][0]
    capabilities = {cap["name"]: cap for cap in tiger["capabilities"]}

    assert payload["schema_version"] == "connector-catalog-v1"
    assert payload["safety"]["read_only"] is True
    assert payload["safety"]["credential_values_exposed"] is False
    assert tiger["active"] is True
    assert {cap["name"]: cap["status"] for cap in tiger["capabilities"]} == {
        "price_feed": "ready",
        "broker_order": "ready",
    }
    assert capabilities["price_feed"]["acceptance"]["status"] == "accepted"
    assert capabilities["price_feed"]["acceptance"]["ready_for_price_feed"] is True
    assert capabilities["price_feed"]["acceptance"]["exit_code"] == 0
    assert capabilities["price_feed"]["acceptance"]["operator_status"] == "accepted"
    assert capabilities["price_feed"]["acceptance"]["operator_summary"] == "Tiger price-feed acceptance passed."
    assert capabilities["price_feed"]["acceptance"]["can_enable_broker_orders_from_this_gate"] is False
    assert credential["name"] == "TIGER_OPENAPI_CONFIG_PATH"
    assert credential["file_exists"] is True
    assert credential["owner_only"] is True
    assert credential["value_exposed"] is False
    assert str(props) not in json.dumps(payload)


def test_connector_catalog_can_load_tiger_props_path_from_live_env_file(monkeypatch, tmp_path: Path):
    props = tmp_path / "tiger_openapi_config.properties"
    props.write_text("tiger_id=placeholder\n", encoding="utf-8")
    props.chmod(0o600)
    env_file = tmp_path / "live.env"
    env_file.write_text(f"TIGER_OPENAPI_CONFIG_PATH={props}\n", encoding="utf-8")
    monkeypatch.delenv("TIGER_OPENAPI_CONFIG_PATH", raising=False)
    monkeypatch.setenv("TRADING_ORCHESTRATOR_LIVE_ENV", str(env_file))
    output_root = tmp_path / "outputs"
    _write_price_feed_ready(output_root)
    _write_price_feed_acceptance(output_root)

    payload = ConnectorCatalog(config=_config(), output_root=output_root, load_live_env=True).snapshot()
    tiger = _by_id(payload)["tiger_openapi"]
    credential = tiger["credential_requirements"][0]
    capabilities = {cap["name"]: cap for cap in tiger["capabilities"]}

    assert capabilities["price_feed"]["status"] == "ready"
    assert capabilities["broker_order"]["status"] == "ready"
    assert credential["present"] is True
    assert credential["file_exists"] is True
    assert credential["owner_only"] is True
    assert credential["value_exposed"] is False
    assert str(props) not in json.dumps(payload)


def test_connector_catalog_blocks_tiger_price_feed_until_readiness_gate_passes(monkeypatch, tmp_path: Path):
    props = tmp_path / "tiger_openapi_config.properties"
    props.write_text("tiger_id=placeholder\n", encoding="utf-8")
    props.chmod(0o600)
    monkeypatch.setenv("TIGER_OPENAPI_CONFIG_PATH", str(props))

    payload = ConnectorCatalog(config=_config(), output_root=tmp_path / "outputs").snapshot()
    tiger = _by_id(payload)["tiger_openapi"]
    capabilities = {cap["name"]: cap for cap in tiger["capabilities"]}

    assert capabilities["price_feed"]["status"] == "blocked"
    assert capabilities["price_feed"]["readiness"]["status"] == "missing"
    assert capabilities["price_feed"]["acceptance"]["status"] == "missing"
    assert capabilities["broker_order"]["status"] == "ready"
    assert payload["summary"]["ready_price_feed_count"] == 3


def test_connector_catalog_reports_tiger_price_feed_acceptance_pending_market_open(monkeypatch, tmp_path: Path):
    props = tmp_path / "tiger_openapi_config.properties"
    props.write_text("tiger_id=placeholder\n", encoding="utf-8")
    props.chmod(0o600)
    monkeypatch.setenv("TIGER_OPENAPI_CONFIG_PATH", str(props))
    output_root = tmp_path / "outputs"
    _write_price_feed_acceptance(output_root, status="pending_market_open", exit_code=75)

    payload = ConnectorCatalog(config=_config(), output_root=output_root).snapshot()
    tiger = _by_id(payload)["tiger_openapi"]
    price_feed = {cap["name"]: cap for cap in tiger["capabilities"]}["price_feed"]

    assert price_feed["status"] == "blocked"
    assert price_feed["acceptance"]["status"] == "pending_market_open"
    assert price_feed["acceptance"]["exit_code"] == 75
    assert price_feed["acceptance"]["blocker_count"] == 1
    assert price_feed["acceptance"]["operator_action"] == "rerun_after_next_trading_window"
    assert price_feed["acceptance"]["operator_status"] == "waiting_market_open"
    assert price_feed["acceptance"]["operator_summary"] == "Wait until 2026-07-05T22:00:00+00:00 to rerun Tiger price-feed acceptance."
    assert price_feed["acceptance"]["next_command"].startswith("python3 -m pipelines.tiger_price_feed_acceptance")
    assert price_feed["acceptance"]["next_trading_window"]["start"] == "2026-07-05T22:00:00+00:00"
    assert "waiting for the next market-hours validation window" in price_feed["summary"]
    assert str(props) not in json.dumps(payload)


def test_connector_catalog_blocks_tiger_when_props_file_is_not_owner_only(monkeypatch, tmp_path: Path):
    props = tmp_path / "tiger_openapi_config.properties"
    props.write_text("tiger_id=placeholder\n", encoding="utf-8")
    props.chmod(0o644)
    monkeypatch.setenv("TIGER_OPENAPI_CONFIG_PATH", str(props))

    payload = ConnectorCatalog(config=_config(), output_root=tmp_path / "outputs").snapshot()
    tiger = _by_id(payload)["tiger_openapi"]
    credential = tiger["credential_requirements"][0]

    assert {cap["name"]: cap["status"] for cap in tiger["capabilities"]} == {
        "price_feed": "needs_credentials",
        "broker_order": "needs_credentials",
    }
    assert credential["file_exists"] is True
    assert credential["owner_only"] is False
    assert credential["mode"] == "0o644"


def test_connector_catalog_reports_binance_env_presence_without_secret_values(monkeypatch):
    monkeypatch.setenv("BINANCE_API_KEY", "real-key-value")
    monkeypatch.setenv("BINANCE_API_SECRET", "real-secret-value")

    payload = ConnectorCatalog(config=_config()).snapshot()
    binance = _by_id(payload)["binance_usdm"]

    assert {cap["name"]: cap["status"] for cap in binance["capabilities"]}["broker_order"] == "ready"
    assert [item["name"] for item in binance["credential_requirements"]] == ["BINANCE_API_KEY", "BINANCE_API_SECRET"]
    assert all(item["present"] is True for item in binance["credential_requirements"])
    assert all(item["value_exposed"] is False for item in binance["credential_requirements"])
    serialized = json.dumps(payload)
    assert "real-key-value" not in serialized
    assert "real-secret-value" not in serialized


def test_connector_catalog_dashboard_response_is_read_only(monkeypatch):
    from pipelines import dashboard_server

    class FakeConnectorCatalog:
        def snapshot(self):
            return {"schema_version": "connector-catalog-v1", "safety": {"read_only": True}}

    monkeypatch.setattr(dashboard_server, "ConnectorCatalog", FakeConnectorCatalog)

    response = dashboard_server.build_connector_catalog_response()

    assert response == {"schema_version": "connector-catalog-v1", "safety": {"read_only": True}}
    assert "/api/connectors/catalog" not in dashboard_server._DUALTRACK_POST_ENDPOINTS
