from pathlib import Path

from services.paper_release_receipt import current_source_attestation
from services.live_activation_gate import LiveActivationGate, _digest


def _readiness(source: dict) -> tuple[dict, list[dict]]:
    rows = []
    required_categories = {
        "orders_fills_positions_reconciliation": {"observed_at": "2026-08-20T01:00:00+00:00"},
        "protection_coverage": {"observed_at": "2026-08-20T01:00:00+00:00"},
        "capability_status": {"observed_at": "2026-08-20T01:00:00+00:00"},
        "market_freshness_trust": {"observed_at": "2026-08-20T01:00:00+00:00"},
        "runtime_health": {"observed_at": "2026-08-20T01:00:00+00:00"},
        "retry_outcomes": {"observed_at": "2026-08-20T01:00:00+00:00"},
        "release_account_environment_identity": {"observed_at": "2026-08-20T01:00:00+00:00"},
        "recording_package": {"observed_at": "2026-08-20T01:00:00+00:00"},
    }
    for index in range(14):
        row = {
            "window_index": index,
            "status": "pass",
            "blockers": [],
            "package_status": "complete",
            "review_digest": "sha256:" + f"{index + 1:064x}"[-64:],
            "gate_evidence": required_categories,
        }
        row["row_digest"] = _digest({key: value for key, value in row.items() if key != "row_digest"})
        rows.append(row)
    receipt = {
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
    receipt["window_digests"] = [row["row_digest"] for row in rows]
    receipt["required_day_count"] = 7
    receipt["receipt_digest"] = _digest({key: value for key, value in receipt.items() if key != "receipt_digest"})
    return receipt, rows


def _ready_gate(tmp_path: Path) -> tuple[LiveActivationGate, dict, list[dict]]:
    source = current_source_attestation(Path(__file__).resolve().parents[1])
    readiness, rows = _readiness(source)
    gate = LiveActivationGate(
        tmp_path / "outputs",
        park_user_id="park",
        park_chat_id="chat",
        readiness_receipt_resolver=lambda: readiness,
        readiness_windows_resolver=lambda: rows,
    )
    return gate, readiness, rows


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
        readiness=_readiness(source)[0],
        capabilities={"operations": operations},
        risk_limits={"max_acceptable_loss": 25, "max_notional": 100, "max_leverage": 2},
        source_attestation=source,
    )


def test_live_preflight_is_read_only_and_binds_exact_identity(tmp_path: Path) -> None:
    gate, _, _ = _ready_gate(tmp_path)
    result = _preflight(gate)
    assert result["status"] == "ready_for_activation"
    assert result["environment"] == "mainnet"
    assert result["network_io"] is False
    assert result["live_writes_enabled"] is False


def test_live_activation_requires_exact_confirmation_and_is_idempotent(tmp_path: Path) -> None:
    gate, _, _ = _ready_gate(tmp_path)
    preflight = _preflight(gate)
    proposal = gate.prepare_activation(preflight, plan_digest="sha256:" + "c" * 64, expires_at=4102444800)
    rejected = gate.confirm(activation_digest=proposal["activation_digest"], command_text="confirm live wrong", park_user_id="park", telegram_update_id=1, telegram_message_id=1, telegram_chat_id="chat", telegram_receipt={"event": "inbound_received", "update_id": 1, "message_id": 1, "sender_id": "park", "chat_id": "chat", "text": "confirm live wrong", "text_digest": "sha256:" + "d" * 64}, current_preflight=preflight, now=1787350000)
    assert rejected["event"] == "activation_rejected"

    # A fresh gate is used to model a new exact proposal after rejection.
    gate, _, _ = _ready_gate(tmp_path / "fresh")
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
    receipt = {"event": "inbound_received", "update_id": 2, "message_id": 3, "sender_id": "park", "chat_id": "chat", "text": command, "text_digest": "sha256:" + "e" * 64}
    confirmed = gate.confirm(activation_digest=proposal["activation_digest"], command_text=command, park_user_id="park", telegram_update_id=2, telegram_message_id=3, telegram_chat_id="chat", telegram_receipt=receipt, current_preflight=preflight, now=1787350000)
    replay = gate.confirm(activation_digest=proposal["activation_digest"], command_text=command, park_user_id="park", telegram_update_id=2, telegram_message_id=3, telegram_chat_id="chat", telegram_receipt=receipt, current_preflight=preflight, now=1787350001)
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
