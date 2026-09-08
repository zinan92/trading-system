from __future__ import annotations

from pathlib import Path

import pytest

from services.testnet_automation_coordinator import TestnetAutomationCoordinator
from services.testnet_execution import PaperExecutionPort, TestnetExecutionError, map_grid_orders


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
