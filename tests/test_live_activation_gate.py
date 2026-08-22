from pathlib import Path

from services.paper_release_receipt import current_source_attestation
from services.live_activation_gate import LiveActivationGate


def _readiness(source: dict) -> dict:
    return {
        "status": "ready",
        "environment": "testnet",
        "window_count": 14,
        "required_window_count": 14,
        "day_count": 7,
        "required_day_count": 7,
        "receipt_digest": "sha256:" + "a" * 64,
        "live_enabled": False,
        "live_writes_enabled": False,
        "blockers": [],
        "source_attestation": source,
        "critical_gate_results": {"orders": "pass", "protection": "pass", "reconciliation": "pass"},
    }


def _preflight(gate: LiveActivationGate) -> dict:
    source = current_source_attestation(Path(__file__).resolve().parents[1])
    operations = [
        "account.read",
        "order_execution.submit",
        "order_execution.cancel",
        "order_execution.replace",
        "order_execution.query",
        "order_execution.open_orders",
        "order_execution.fills",
        "protection_order.submit",
        "protection_order.cancel",
        "protection_order.replace",
        "protection_order.query",
        "protection_order.reduce_only",
        "protection_order.position_following",
        "protection_order.position_level_tpsl",
        "protection_order.take_profit_market",
        "protection_order.stop_loss_market",
        "reconciliation",
    ]
    return gate.preflight(
        broker_id="hyperliquid",
        account_id="0x" + "1" * 40,
        environment_fingerprint="hyperliquid:mainnet:0x" + "1" * 40,
        release_sha=source["source_sha"],
        credential_source="HL_MAINNET_CREDENTIAL",
        instrument_scope="default_perpetuals",
        strategy_scope="dca",
        readiness=_readiness(source),
        capabilities={"operations": operations},
        risk_limits={"max_acceptable_loss": 25, "max_notional": 100, "max_leverage": 2},
        source_attestation=source,
    )


def test_live_preflight_is_read_only_and_binds_exact_identity(tmp_path: Path) -> None:
    gate = LiveActivationGate(tmp_path / "outputs", park_user_id="park")
    result = _preflight(gate)
    assert result["status"] == "ready_for_activation"
    assert result["environment"] == "mainnet"
    assert result["network_io"] is False
    assert result["live_writes_enabled"] is False


def test_live_activation_requires_exact_confirmation_and_is_idempotent(tmp_path: Path) -> None:
    gate = LiveActivationGate(tmp_path / "outputs", park_user_id="park")
    preflight = _preflight(gate)
    proposal = gate.prepare_activation(preflight, plan_digest="sha256:" + "c" * 64, expires_at=4102444800)
    rejected = gate.confirm(activation_digest=proposal["activation_digest"], command_text="confirm live wrong", park_user_id="park", telegram_update_id=1, telegram_message_id=1, current_preflight=preflight, now=1787350000)
    assert rejected["event"] == "activation_rejected"

    # A fresh gate is used to model a new exact proposal after rejection.
    gate = LiveActivationGate(tmp_path / "fresh", park_user_id="park")
    preflight = _preflight(gate)
    proposal = gate.prepare_activation(preflight, plan_digest="sha256:" + "c" * 64, expires_at=4102444800)
    command = "confirm live " + " ".join([
        proposal["activation_digest"],
        preflight["release_sha"],
        preflight["account_id"],
        preflight["environment_fingerprint"],
        preflight["strategy_scope"],
        proposal["plan_digest"],
    ])
    confirmed = gate.confirm(activation_digest=proposal["activation_digest"], command_text=command, park_user_id="park", telegram_update_id=2, telegram_message_id=3, telegram_chat_id=4, current_preflight=preflight, now=1787350000)
    replay = gate.confirm(activation_digest=proposal["activation_digest"], command_text=command, park_user_id="park", telegram_update_id=2, telegram_message_id=3, telegram_chat_id=4, current_preflight=preflight, now=1787350001)
    assert confirmed == replay
    assert confirmed["live_writes_enabled"] is False
    assert confirmed["execution_authorized"] is False
    assert gate.public_status()["status"] == "activated_pending_canary"


def test_live_preflight_blocks_grid_and_missing_soak_or_capabilities(tmp_path: Path) -> None:
    gate = LiveActivationGate(tmp_path / "outputs", park_user_id="park")
    result = gate.preflight(
        broker_id="hyperliquid",
        account_id="",
        environment_fingerprint="paper",
        release_sha="not-a-sha",
        credential_source="raw-secret",
        instrument_scope="hip3",
        strategy_scope="grid",
        readiness={"status": "blocked", "environment": "paper"},
        capabilities={"operations": []},
    )
    assert result["status"] == "blocked"
    assert "live_scope_must_be_dca" in result["blockers"]
    assert result["live_writes_enabled"] is False
