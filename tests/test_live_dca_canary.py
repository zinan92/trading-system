from pathlib import Path

import pytest

from services.live_dca_canary import LiveDcaCanary, LiveDcaCanaryError, REQUIRED_TRANSPORT_CAPABILITIES
from tests.test_live_activation_gate import _preflight, _ready_gate


class FixtureTransport:
    network_io = False
    capabilities = set(REQUIRED_TRANSPORT_CAPABILITIES)

    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []
        self.next_order = 0

    def submit_entry(self, request):
        self.calls.append(("submit_entry", dict(request)))
        self.next_order += 1
        return {
            "status": "filled" if request["index"] == 0 else "submitted",
            "order_id": f"entry-{self.next_order}",
            "fill": {"quantity": request["quantity"], "price": request["price"]} if request["index"] == 0 else None,
        }

    def cancel_order(self, order_id):
        self.calls.append(("cancel_order", order_id))
        return {"status": "canceled", "order_id": order_id}

    def replace_protection(self, request):
        self.calls.append(("replace_protection", dict(request)))
        return {"status": "accepted", "group_id": "protection-1", "reduce_only": True}

    def query_order(self, order_id):
        self.calls.append(("query_order", order_id))
        return {"status": "filled", "order_id": order_id}

    def account_snapshot(self):
        self.calls.append(("account_snapshot", ""))
        return {"status": "ok"}

    def reconcile(self, expected):
        self.calls.append(("reconcile", dict(expected)))
        return {"status": "ok", "open_quantity": 0 if expected.get("require_flat") else expected.get("open_quantity", 0)}

    def flatten_reduce_only(self, request):
        self.calls.append(("flatten_reduce_only", dict(request)))
        return {"status": "accepted", "reduce_only": True}


def _activated_gate(tmp_path: Path):
    gate, _, _ = _ready_gate(tmp_path)
    preflight = _preflight(gate)
    proposal = gate.prepare_activation(preflight, plan_digest="sha256:" + "c" * 64, expires_at=4102444800)
    command = "confirm live " + " ".join([
        proposal["activation_digest"],
        preflight["release_sha"],
        preflight["account_id"],
        preflight["environment_fingerprint"],
        "dca",
        proposal["plan_digest"],
    ])
    receipt = {"event": "inbound_received", "update_id": 1, "message_id": 2, "sender_id": "park", "chat_id": "chat", "text": command, "text_digest": "sha256:" + __import__("hashlib").sha256(command.encode()).hexdigest()}
    confirmed = gate.confirm(
        activation_digest=proposal["activation_digest"],
        command_text=command,
        park_user_id="park",
        telegram_update_id=1,
        telegram_message_id=2,
        telegram_chat_id="chat",
        telegram_receipt=receipt,
        current_preflight=preflight,
        now=1787350000,
    )
    assert confirmed["event"] == "activation_confirmed"
    return gate


def _plan() -> dict:
    return {
        "strategy_type": "dca",
        "direction": "long",
        "plan_digest": "sha256:" + "c" * 64,
        "target_price": 4500,
        "stop_price": 3700,
        "account_equity": 2000,
        "risk_limits": {
            "max_acceptable_loss": 1000,
            "max_notional": 10000,
            "max_leverage": 5,
            "max_open_orders": 2,
            "max_positions": 2,
            "max_slippage": 10,
        },
        "entries": [
            {"price": 4000, "quantity": 1, "notional": 4000},
            {"price": 3900, "quantity": 1, "notional": 3900},
        ],
    }


def test_attended_dca_canary_keeps_fixture_non_network_and_distinguishes_stop_cancel_flatten(tmp_path: Path) -> None:
    transport = FixtureTransport()
    canary = LiveDcaCanary(
        tmp_path / "outputs",
        transport=transport,
        park_user_id="park",
        park_chat_id="chat",
        gate=_activated_gate(tmp_path),
    )
    started = canary.start(_plan(), timestamp="2026-08-22T00:00:00+00:00")
    assert started["status"] == "prepared"
    assert started["network_io"] is False
    assert started["live_writes_enabled"] is False
    canary.submit_entry(0, timestamp="2026-08-22T00:01:00+00:00")
    canary.replace_protection(quantity=1, timestamp="2026-08-22T00:02:00+00:00")
    canary.submit_entry(1, timestamp="2026-08-22T00:03:00+00:00")
    canary.cancel("entry-2", timestamp="2026-08-22T00:04:00+00:00")
    stopped = canary.stop(timestamp="2026-08-22T00:05:00+00:00")
    assert stopped["status"] == "stopped"
    assert any(name == "cancel_order" for name, _ in transport.calls)
    flatten = [request for name, request in transport.calls if name == "flatten_reduce_only"]
    assert flatten and flatten[-1]["reduce_only"] is True
    assert canary.snapshot()["live_writes_enabled"] is False


def test_live_dca_canary_blocks_grid_and_risk_budget_before_transport(tmp_path: Path) -> None:
    transport = FixtureTransport()
    canary = LiveDcaCanary(tmp_path / "outputs", transport=transport, park_user_id="park", park_chat_id="chat", gate=_activated_gate(tmp_path))
    grid = _plan()
    grid["strategy_type"] = "grid"
    with pytest.raises(LiveDcaCanaryError, match="DCA"):
        canary.start(grid, timestamp="2026-08-22T00:00:00+00:00")
    oversized = _plan()
    oversized["risk_limits"]["max_acceptable_loss"] = 1
    with pytest.raises(LiveDcaCanaryError, match="maximum acceptable loss"):
        canary.start(oversized, timestamp="2026-08-22T00:00:00+00:00")
    assert transport.calls == []


def test_live_dca_canary_freezes_on_unknown_entry_response(tmp_path: Path) -> None:
    class UnknownTransport(FixtureTransport):
        def submit_entry(self, request):
            self.calls.append(("submit_entry", dict(request)))
            return {"status": "unknown"}

    transport = UnknownTransport()
    canary = LiveDcaCanary(tmp_path / "outputs", transport=transport, park_user_id="park", park_chat_id="chat", gate=_activated_gate(tmp_path))
    canary.start(_plan(), timestamp="2026-08-22T00:00:00+00:00")
    with pytest.raises(LiveDcaCanaryError, match="unknown"):
        canary.submit_entry(0, timestamp="2026-08-22T00:01:00+00:00")
    assert canary.snapshot()["status"] == "prepared"
