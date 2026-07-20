import json
from dataclasses import FrozenInstanceError

import pytest

from services.broker_port import (
    BrokerCancelRequest,
    BrokerCapabilities,
    BrokerCapability,
    BrokerOrderRequest,
    BrokerProtectiveRecoveryRequest,
    broker_port_descriptor,
    execution_capabilities_for,
)


def test_broker_requests_are_frozen_and_normalized():
    order = BrokerOrderRequest(
        run_date="2026-07-18",
        ticket={"ticket_id": "ticket-1", "asset": "GOLD"},
        latest_price=4000.0,
        actual_size=0.01,
    )
    cancel = BrokerCancelRequest(
        run_date="2026-07-18",
        asset="GOLD",
        client_order_id="client-1",
        broker_order_id="venue-1",
    )
    recovery = BrokerProtectiveRecoveryRequest(
        run_date="2026-07-18",
        lifecycle_record={"order_id": "order-1", "ticket_id": "ticket-1"},
        exchange_position={"symbol": "XAUUSDT", "position_amt": "0.01"},
    )

    with pytest.raises(FrozenInstanceError):
        order.run_date = "2026-07-19"
    with pytest.raises(FrozenInstanceError):
        cancel.asset = "BTC"
    with pytest.raises(FrozenInstanceError):
        recovery.source = "changed"

    assert cancel.asset == "GOLD"
    assert recovery.source == "order_recovery"


def test_broker_request_validation_fails_closed():
    with pytest.raises(ValueError, match="run_date"):
        BrokerOrderRequest(run_date="", ticket={"ticket_id": "ticket-1"})
    with pytest.raises(ValueError, match="ticket"):
        BrokerOrderRequest(run_date="2026-07-18", ticket={})
    with pytest.raises(ValueError, match="order identity"):
        BrokerCancelRequest(run_date="2026-07-18", asset="GOLD")
    with pytest.raises(ValueError, match="lifecycle_record"):
        BrokerProtectiveRecoveryRequest(run_date="2026-07-18", lifecycle_record={})


def test_capability_set_is_closed_and_deterministic():
    capabilities = BrokerCapabilities(
        frozenset({BrokerCapability.PREFLIGHT, BrokerCapability.SUBMIT_ORDER})
    )

    assert capabilities.names == ("preflight", "submit_order")
    assert capabilities.supports(BrokerCapability.SUBMIT_ORDER) is True
    assert capabilities.supports(BrokerCapability.CANCEL_ORDER) is False
    with pytest.raises(ValueError, match="unknown broker capability"):
        BrokerCapabilities(frozenset({"submit_order", "wire_anything"}))


def test_capabilities_are_provider_scoped_not_inherited_method_scoped():
    binance = execution_capabilities_for(provider="binance_usdm", adapter_name="live")
    tiger_subclass = execution_capabilities_for(
        provider="tiger_openapi",
        adapter_name="tiger_openapi_paper",
    )
    tiger_legacy_base = execution_capabilities_for(
        provider="tiger_openapi",
        adapter_name="live",
    )

    assert binance.supports(BrokerCapability.CANCEL_ORDER)
    assert binance.supports(BrokerCapability.PROTECTIVE_RECOVERY)
    for capabilities in (tiger_subclass, tiger_legacy_base):
        assert not capabilities.supports(BrokerCapability.CANCEL_ORDER)
        assert not capabilities.supports(BrokerCapability.PROTECTIVE_RECOVERY)


def test_port_descriptor_exposes_env_names_never_secret_values():
    class _Adapter:
        name = "secret_safe"
        provider = "binance_usdm"
        broker_config = {
            "environment": "live",
            "api_key_env": "BINANCE_API_KEY",
            "api_secret_env": "BINANCE_API_SECRET",
            "api_key": "must-not-leak-key",
            "api_secret": "must-not-leak-secret",
        }
        capabilities = execution_capabilities_for(
            provider="binance_usdm",
            adapter_name="secret_safe",
        )

    payload = broker_port_descriptor(_Adapter()).to_dict()
    rendered = json.dumps(payload, sort_keys=True)

    assert payload["credential_env_names"] == [
        "BINANCE_API_KEY",
        "BINANCE_API_SECRET",
    ]
    assert "must-not-leak" not in rendered


def test_binance_public_capability_methods_keep_provider_details_inside_adapter(tmp_path, monkeypatch):
    from services.broker_adapter import LiveBrokerAdapter

    adapter = LiveBrokerAdapter(
        tmp_path / "outputs",
        True,
        {
            "provider": "binance_usdm",
            "environment": "demo",
            "dry_run": True,
            "instrument_map": {"GOLD": "XAUUSDT"},
        },
    )
    cancel_calls = []
    recovery_calls = []
    monkeypatch.setattr(
        adapter,
        "cancel_binance_order",
        lambda symbol, **kwargs: cancel_calls.append((symbol, kwargs)) or {"status": "CANCELED"},
    )
    monkeypatch.setattr(
        adapter,
        "recover_missing_protective_orders",
        lambda run_date, lifecycle, position, *, source: recovery_calls.append(
            (run_date, lifecycle, position, source)
        )
        or {"status": "recovered"},
    )

    cancel = adapter.cancel_order(
        BrokerCancelRequest(
            run_date="2026-07-18",
            asset="GOLD",
            client_order_id="client-1",
        )
    )
    recovery = adapter.recover_protective_orders(
        BrokerProtectiveRecoveryRequest(
            run_date="2026-07-18",
            lifecycle_record={"order_id": "order-1"},
            exchange_position={"symbol": "XAUUSDT", "position_amt": 1},
            source="test_recovery",
        )
    )

    assert cancel == {"status": "CANCELED"}
    assert cancel_calls == [
        (
            "XAUUSDT",
            {"orig_client_order_id": "client-1", "order_id": ""},
        )
    ]
    assert recovery == {"status": "recovered"}
    assert recovery_calls[0][0] == "2026-07-18"
    assert recovery_calls[0][3] == "test_recovery"


def test_tiger_cannot_gain_binance_cancel_by_inheritance_or_provider_mutation(tmp_path, monkeypatch):
    from services.broker_adapter import LiveBrokerAdapter

    adapter = LiveBrokerAdapter(
        tmp_path / "outputs",
        True,
        {"provider": "tiger_openapi", "environment": "paper", "dry_run": True},
    )
    network_calls = []
    monkeypatch.setattr(
        adapter,
        "_binance_signed_request",
        lambda *args, **kwargs: network_calls.append((args, kwargs)),
    )

    with pytest.raises(RuntimeError, match="does not support cancel_order"):
        adapter.cancel_binance_order("XAUUSDT", orig_client_order_id="client-1")
    adapter.provider = "binance_usdm"
    with pytest.raises(RuntimeError, match="does not support cancel_order"):
        adapter.cancel_order(
            BrokerCancelRequest(
                run_date="2026-07-18",
                asset="GOLD",
                client_order_id="client-1",
            )
        )

    assert network_calls == []
