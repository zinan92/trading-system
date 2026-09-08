from __future__ import annotations

from pathlib import Path

import pytest

from services.testnet_automation_coordinator import TestnetAutomationCoordinator
from services.testnet_execution import PaperExecutionPort, TestnetExecutionError, map_grid_orders
from services.testnet_execution import ExternalTestnetExecutionPort


class _Paper:
    class _Transport:
        local_only = True

    transport = _Transport()

    def __init__(self, responses=None):
        self.calls = []
        self.responses = responses or {}

    def request(self, port, operation, payload):
        self.calls.append((port, operation, payload))
        response = self.responses.get(operation, {"accepted": True})
        if isinstance(response, BaseException):
            raise response
        return response

    def preflight(self):
        return {
            "environment": "paper",
            "network_io": False,
            "real_money_eligible": False,
            "ports": ("market_data", "instrument", "account", "order_execution", "protection_order", "fee"),
        }


class _External:
    transport_state = "external_testnet"
    broker_config = {
        "transport_profile": "hyperliquid-testnet-position-protection",
        "environment": "testnet",
        "real_money_eligible": False,
        "live_trading_enabled": False,
    }

    def __init__(self, *, fail_submit: bool = False):
        self.calls = []
        self.fail_submit = fail_submit

    def preflight(self, **kwargs):
        self.calls.append(("preflight", kwargs))
        return {"ready": True, "environment": "testnet", "real_money_eligible": False}

    def submit_order(self, request):
        self.calls.append(("submit", request))
        if self.fail_submit:
            raise TimeoutError("ambiguous submit")
        return {"receipt_id": "r-1", "state": "partially_filled", "accepted": True}

    def request(self, port, operation, payload):
        self.calls.append((port, operation, payload))
        if port == "order_execution" and operation == "query":
            return {"state": "resting", "accepted": True}
        return {"state": "active", "accepted": True, "observation_digest": "sha256:" + "a" * 64}

    def read_facts(self, **kwargs):
        self.calls.append(("facts", kwargs))
        return {"fills": [{"quantity": "0.04", "fee": "0.01"}], "positions": [{"quantity": "0.04"}]}


def _plan() -> dict:
    return {
        "plan_digest": "sha256:" + "a" * 64,
        "instrument_id": "BTC-USD-PERP",
        "grid": {"rungs": [{"rung": i, "side": "buy", "quantity": "0.1", "price": str(60000 + i)} for i in range(1, 11)]},
    }


def test_grid_maps_ten_rungs_to_unique_identity_bound_requests() -> None:
    requests = map_grid_orders(_plan(), activation_id="activation", slice_id="slice", command_id="grid-1")

    assert len(requests) == 10
    assert len({request.idempotency_key for request in requests}) == 10
    assert all(request.activation_id == "activation" and request.slice_id == "slice" for request in requests)
    assert all(request.plan_digest == _plan()["plan_digest"] for request in requests)


def test_paper_port_replays_idempotently_and_uses_order_execution_port() -> None:
    broker = _Paper()
    request = map_grid_orders(_plan(), activation_id="activation", slice_id="slice", command_id="grid-1")[0]
    port = PaperExecutionPort(broker)

    first = port.submit(request)
    replay = port.submit(request)

    assert replay == first
    assert len(broker.calls) == 1
    assert broker.calls[0][0] == "order_execution"
    assert first["network_io"] is False


def test_unknown_side_effect_allows_one_identity_query_then_stops() -> None:
    broker = _Paper({"submit": TimeoutError("transport lost"), "query": {"accepted": False}})
    request = map_grid_orders(_plan(), activation_id="activation", slice_id="slice", command_id="grid-1")[0]
    port = PaperExecutionPort(broker)

    unknown = port.submit(request)
    reconciled = port.reconcile_unknown(request)
    stopped = port.reconcile_unknown(request)

    assert unknown["unknown"] is True
    assert reconciled["operation"] == "query"
    assert stopped["reconcile_allowed"] is False
    assert [call[1] for call in broker.calls] == ["submit", "query"]


def test_coordinator_enables_local_paper_without_network(tmp_path: Path) -> None:
    coordinator = TestnetAutomationCoordinator(tmp_path / "outputs")
    activation = {
        "strategy_family": "grid", "strategy_session_id": "session", "strategy_revision_id": "revision",
        "plan_digest": _plan()["plan_digest"], "account_fingerprint": "sha256:" + "b" * 64,
        "broker_id": "hyperliquid", "environment": "testnet", "transport_profile": "hyperliquid-testnet-default",
        "instrument_id": "BTC-USD-PERP", "runtime_id": "runtime", "release_sha": "c" * 40,
        "capability_revision": "paper-v1",
    }
    coordinator.activate(activation, command_id="activate")
    result = coordinator.enable_paper_execution(_Paper())

    assert result["execution_enabled"] is True
    assert result["execution_blocker"] is None
    assert result["paper_network_io"] is False

    coordinator._record({**result, "execution_slice": {"execution_slice_id": "slice-1"}})
    submitted = coordinator.submit_grid_orders(_plan(), broker=_Paper(), command_id="grid-1")
    assert submitted["canonical_order_count"] == 10


def test_grid_requires_rungs() -> None:
    with pytest.raises(TestnetExecutionError, match="grid_rungs_required"):
        map_grid_orders({"plan_digest": "sha256:" + "a" * 64, "instrument_id": "BTC"}, activation_id="a", slice_id="s", command_id="c")


def test_external_port_uses_exact_profile_and_preserves_partial_fill_facts() -> None:
    broker = _External()
    request = map_grid_orders(_plan(), activation_id="activation", slice_id="slice", command_id="grid-1")[0]
    port = ExternalTestnetExecutionPort(broker)

    receipt = port.submit(request)
    facts = port.read_facts(instrument_id="BTC-USD-PERP", order_id=request.order_id)

    assert receipt["accepted"] is True
    assert receipt["network_io"] is True
    assert facts["fills"][0]["quantity"] == "0.04"
    assert [call[0] for call in broker.calls] == ["submit", "facts"]


def test_external_unknown_submit_is_fail_closed_and_never_retried() -> None:
    broker = _External(fail_submit=True)
    request = map_grid_orders(_plan(), activation_id="activation", slice_id="slice", command_id="grid-1")[0]
    port = ExternalTestnetExecutionPort(broker)

    first = port.submit(request)
    replay = port.submit(request)

    assert first["unknown"] is True
    assert replay == first
    assert [call[0] for call in broker.calls] == ["submit"]


def test_external_profile_rejects_mainnet_or_default_profile() -> None:
    broker = _External()
    broker.broker_config = {**broker.broker_config, "transport_profile": "hyperliquid-testnet-default"}
    with pytest.raises(TestnetExecutionError, match="external_protection_profile_required"):
        ExternalTestnetExecutionPort(broker)
    broker.broker_config = {**broker.broker_config, "transport_profile": "hyperliquid-testnet-position-protection", "environment": "mainnet"}
    with pytest.raises(TestnetExecutionError, match="external_testnet_environment_required"):
        ExternalTestnetExecutionPort(broker)


def test_external_cancel_race_and_protection_use_public_ports() -> None:
    broker = _External()
    request = map_grid_orders(_plan(), activation_id="activation", slice_id="slice", command_id="grid-1")[0]
    port = ExternalTestnetExecutionPort(broker)
    group = type("Group", (), {"protection_id": "protection-1"})()

    cancelled = port.cancel(request)
    protection = port.submit_protection(group)
    confirmed = port.query_protection(group)

    assert cancelled["operation"] == "cancel"
    assert protection["state"] == "active"
    assert confirmed["accepted"] is True
    assert [(call[0], call[1]) for call in broker.calls if len(call) > 1] == [
        ("order_execution", "cancel"),
        ("protection_order", "submit"),
        ("protection_order", "query"),
    ]


def test_coordinator_binds_external_profile_and_submits_grid_slice(tmp_path: Path) -> None:
    coordinator = TestnetAutomationCoordinator(tmp_path / "outputs")
    activation = {
        "strategy_family": "grid", "strategy_session_id": "session", "strategy_revision_id": "revision",
        "plan_digest": _plan()["plan_digest"], "account_fingerprint": "sha256:" + "b" * 64,
        "broker_id": "hyperliquid", "environment": "testnet",
        "transport_profile": "hyperliquid-testnet-position-protection", "instrument_id": "BTC-USD-PERP",
        "runtime_id": "runtime", "release_sha": "c" * 40,
        "capability_revision": "hyperliquid-testnet-position-protection-runtime-v1",
    }
    broker = _External()
    coordinator.activate(activation, command_id="activate")
    enabled = coordinator.enable_testnet_execution(broker)
    assert enabled["status"] == "testnet_execution_ready"
    coordinator._record({**enabled, "execution_slice": {"execution_slice_id": "slice-1"}})

    submitted = coordinator.submit_grid_orders(_plan(), broker=broker, command_id="grid-1")

    assert submitted["status"] == "testnet_execution_active"
    assert submitted["network_operation_invoked"] is True
    assert submitted["execution_mutation"] is True
