from __future__ import annotations

import json

from services.broker_port import BrokerPortDescriptor
from services.broker_read_model import (
    broker_reconciliation_block_reason,
    broker_reconciliation_status,
    project_broker_read_model,
)


class DiagnosticAdapter:
    name = "diagnostic_adapter"
    provider = "venue-a"
    live_trading_enabled = True
    broker_config = {
        "environment": "demo",
        "dry_run": False,
        "base_url": "https://demo.example.test",
        "instrument_map": {"GOLD": "XAUUSD"},
        "api_key": "literal-secret-must-not-leak",
        "api_key_env": "VENUE_A_KEY",
        "api_secret_env": "VENUE_A_SECRET",
    }
    descriptor = BrokerPortDescriptor(
        adapter_name=name,
        provider=provider,
        environment="demo",
        capabilities=("preflight", "submit_order"),
        credential_env_names=("VENUE_A_KEY", "VENUE_A_SECRET"),
    )

    def preflight(self):
        raise AssertionError("the projector must never run preflight")


def test_broker_read_model_is_safe_generic_and_does_not_call_adapter(monkeypatch) -> None:
    monkeypatch.setenv("VENUE_A_KEY", "key-secret")
    monkeypatch.setenv("VENUE_A_SECRET", "secret-secret")

    model = project_broker_read_model(
        DiagnosticAdapter(),
        readiness={
            "ready": True,
            "dry_run": False,
            "live_trading_enabled": True,
            "checked_at": "2026-07-18T02:00:00+00:00",
            "env_file": "/private/secret.env",
            "raw_acknowledgement": "secret-secret",
        },
        strategy_id="grid",
        profile="demo-profile",
        asset="GOLD",
    )

    assert model["schema_version"] == "broker-read-model-v1"
    assert model["adapter"] == "diagnostic_adapter"
    assert model["provider"] == "venue-a"
    assert model["mode"] == "demo_broker_port"
    assert model["endpoint"] == "https://demo.example.test"
    assert model["symbol"] == "XAUUSD"
    assert model["credentials_present"] is True
    assert model["armed"] is True
    assert model["live_endpoint_allowed"] is False
    encoded = json.dumps(model)
    for secret in ("literal-secret-must-not-leak", "key-secret", "secret-secret", "/private/secret.env"):
        assert secret not in encoded


def test_broker_reconciliation_projection_is_provider_neutral() -> None:
    drift = {"provider": "venue-a", "drifts": [{"reason": "position mismatch"}]}
    naked = {"provider": "venue-b", "suspected_naked_position": True, "reason_code": "missing_protection"}

    assert broker_reconciliation_status(drift) == "drift"
    assert broker_reconciliation_block_reason(drift) == "broker reconciliation drift: position mismatch"
    assert broker_reconciliation_status(naked) == "naked_position_suspected"
    assert broker_reconciliation_block_reason(naked) == "broker suspected naked position: missing_protection"
