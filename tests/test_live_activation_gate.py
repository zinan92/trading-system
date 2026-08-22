import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from services.paper_release_receipt import current_source_attestation
from services.live_activation_gate import LiveActivationGate, _digest
from services.broker_adapter import LiveBrokerAdapter
from services.broker_port import BrokerOrderRequest


def _readiness(source: dict, artifact_root: Path | None = None) -> tuple[dict, list[dict], list[dict]]:
    rows = []
    reviews = []
    now = datetime.now(timezone.utc).replace(microsecond=0)
    final_end = now - timedelta(minutes=1)
    first_start = final_end - timedelta(hours=12 * 14)
    required_categories = {
        category: {"status": "pass", "observed_at": (first_start + timedelta(hours=12 * index + 6)).isoformat(), "environment": "testnet", "broker_id": "hyperliquid", "release_sha": source["source_sha"], "account_fingerprint": "testnet-account"}
        for index, category in enumerate((
            "orders_fills_positions_reconciliation",
            "protection_coverage",
            "capability_status",
            "market_freshness_trust",
            "runtime_health",
            "retry_outcomes",
            "release_account_environment_identity",
            "recording_package",
        ))
    }
    for index in range(14):
        start = first_start + timedelta(hours=12 * index)
        end = start + timedelta(hours=12)
        evidence = {category: dict(payload) for category, payload in required_categories.items()}
        for payload in evidence.values():
            payload["observed_at"] = (start + timedelta(hours=6)).isoformat()
        if artifact_root is not None:
            for category, payload in evidence.items():
                path = artifact_root / f"{index:02d}-{category}.json"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps({"artifact_kind": category, "category": category, "window_index": index, "record_window_id": f"soak-window-{index:02d}", "strategy_session_id": "session-continuous", "strategy_revision_id": "revision-dca", "plan_digest": "sha256:" + "a" * 64, "environment": "testnet", "broker_id": "hyperliquid", "release_sha": source["source_sha"], "account_fingerprint": "testnet-account", "starts_at": start.isoformat(), "ends_at": end.isoformat(), "observed_at": (start + timedelta(hours=6)).isoformat(), "status": "pass"}), encoding="utf-8")
                payload["artifact_ref"] = str(path)
                payload["artifact_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
                payload["artifact_kind"] = category
        row = {
            "window_index": index,
            "record_window_id": f"soak-window-{index:02d}",
            "starts_at": start.isoformat(),
            "ends_at": end.isoformat(),
            "environment": "testnet",
            "broker_id": "hyperliquid",
            "release_sha": source["source_sha"],
            "account_fingerprint": "testnet-account",
            "strategy_session_id": "session-continuous",
            "strategy_revision_id": "revision-dca",
            "plan_digest": "sha256:" + "a" * 64,
            "status": "pass",
            "blockers": [],
            "package_status": "complete",
            "gate_evidence": evidence,
        }
        review = {"status": "pass", "window_index": index}
        review_digest = _digest(review)
        row["review_digest"] = review_digest
        reviews.append({"event": "window_review", "record_window_id": row["record_window_id"], "review": review, "review_digest": review_digest})
        row["row_digest"] = _digest({key: value for key, value in row.items() if key != "row_digest"})
        rows.append(row)
    receipt = {
        "status": "ready",
        "environment": "testnet",
        "strategy_session_id": "session-continuous",
        "strategy_revision_id": "revision-dca",
        "plan_digest": "sha256:" + "a" * 64,
        "window_count": 14,
        "required_window_count": 14,
        "day_count": 7,
        "required_day_count": 7,
        "receipt_digest": "sha256:" + "a" * 64,
        "live_enabled": False,
        "live_writes_enabled": False,
        "blockers": [],
        "source_attestation": source,
        "critical_gate_results": {category: "pass" for category in (
            "orders_fills_positions_reconciliation",
            "protection_coverage",
            "capability_status",
            "market_freshness_trust",
            "runtime_health",
            "retry_outcomes",
            "release_account_environment_identity",
            "recording_package",
        )},
        "created_at": now.isoformat(),
    }
    receipt["window_digests"] = [row["row_digest"] for row in rows]
    receipt["required_day_count"] = 7
    receipt["receipt_digest"] = _digest({key: value for key, value in receipt.items() if key != "receipt_digest"})
    return receipt, rows, reviews


def _ready_gate(tmp_path: Path) -> tuple[LiveActivationGate, dict, list[dict]]:
    source = current_source_attestation(Path(__file__).resolve().parents[1])
    readiness, rows, reviews = _readiness(source, tmp_path / "artifacts")
    gate = LiveActivationGate(
        tmp_path / "outputs",
        park_user_id="park",
        park_chat_id="chat",
        readiness_receipt_resolver=lambda: readiness,
        readiness_windows_resolver=lambda: rows,
        readiness_reviews_resolver=lambda: reviews,
        credential_presence_resolver=lambda _: True,
        approved_plan_resolver=lambda: {
            "status": "approved",
            "strategy_scope": "dca",
            "strategy_session_id": "session-continuous",
            "strategy_revision_id": "revision-dca",
            "plan_digest": "sha256:" + "c" * 64,
            "approval_receipt_digest": "sha256:" + "f" * 64,
            "canonical_plan": {"strategy_type": "dca", "direction": "long", "target_price": 4500, "stop_price": 3700, "account_equity": 2000, "plan_digest": "sha256:" + "c" * 64, "risk_limits": {"max_acceptable_loss": 1000, "max_notional": 10000, "max_leverage": 5, "max_open_orders": 2, "max_positions": 2, "max_slippage": 10}, "entries": [{"price": 4000, "quantity": 1, "notional": 4000}, {"price": 3900, "quantity": 1, "notional": 3900}]},
        },
        park_approval_resolver=lambda _plan_digest, _receipt_digest: True,
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
        readiness=gate._readiness_receipt_resolver(),
        capabilities={"operations": operations},
        risk_limits={"max_acceptable_loss": 1000, "max_notional": 10000, "max_leverage": 5, "max_open_orders": 2, "max_positions": 2, "max_slippage": 10},
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
    rejected_text = "confirm live wrong"
    rejected = gate.confirm(activation_digest=proposal["activation_digest"], command_text=rejected_text, park_user_id="park", telegram_update_id=1, telegram_message_id=1, telegram_chat_id="chat", telegram_receipt={"event": "inbound_received", "update_id": 1, "message_id": 1, "sender_id": "park", "chat_id": "chat", "text": rejected_text, "text_digest": "sha256:" + hashlib.sha256(rejected_text.encode()).hexdigest()}, current_preflight=preflight, now=1787350000)
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
    receipt = {"event": "inbound_received", "update_id": 2, "message_id": 3, "sender_id": "park", "chat_id": "chat", "text": command, "text_digest": "sha256:" + hashlib.sha256(command.encode()).hexdigest()}
    confirmed = gate.confirm(activation_digest=proposal["activation_digest"], command_text=command, park_user_id="park", telegram_update_id=2, telegram_message_id=3, telegram_chat_id="chat", telegram_receipt=receipt, current_preflight=preflight, now=1787350000)
    bad_text = "confirm live wrong"
    bad_receipt = {"event": "inbound_received", "update_id": 4, "message_id": 5, "sender_id": "park", "chat_id": "chat", "text": bad_text, "text_digest": "sha256:" + hashlib.sha256(bad_text.encode()).hexdigest()}
    bad = gate.confirm(activation_digest=proposal["activation_digest"], command_text=bad_text, park_user_id="park", telegram_update_id=4, telegram_message_id=5, telegram_chat_id="chat", telegram_receipt=bad_receipt, current_preflight=preflight, now=4102444801)
    replay = gate.confirm(activation_digest=proposal["activation_digest"], command_text=command, park_user_id="park", telegram_update_id=2, telegram_message_id=3, telegram_chat_id="chat", telegram_receipt=receipt, current_preflight=preflight, now=4102444801)
    assert bad["event"] == "activation_rejected"
    assert bad["code"] == "confirmation_digest_mismatch"
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
    assert "raw-secret" not in str(result)


def test_prepare_rejects_self_consistent_forged_preflight(tmp_path: Path) -> None:
    gate, _, _ = _ready_gate(tmp_path)
    preflight = _preflight(gate)
    forged = dict(preflight)
    forged["risk_limits"] = {
        "max_acceptable_loss": 999,
        "max_notional": 999,
        "max_leverage": 20,
        "max_open_orders": 999,
        "max_positions": 999,
        "max_slippage": 999,
    }
    forged["preflight_digest"] = _digest({key: value for key, value in forged.items() if key != "preflight_digest"})
    try:
        gate.prepare_activation(forged, plan_digest="sha256:" + "c" * 64, expires_at=4102444800)
    except Exception as exc:  # noqa: BLE001 - exact typed blocker is asserted below.
        assert getattr(exc, "code", "") == "live_preflight_blocked"
    else:
        raise AssertionError("a forged preflight must not be activatable")


def test_confirmation_rechecks_current_readiness_and_canary_stays_blocked(tmp_path: Path) -> None:
    gate, readiness, _ = _ready_gate(tmp_path)
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
    readiness["blockers"] = [{"code": "new_blocker"}]
    receipt = {"event": "inbound_received", "update_id": 7, "message_id": 8, "sender_id": "park", "chat_id": "chat", "text": command, "text_digest": "sha256:" + hashlib.sha256(command.encode()).hexdigest()}
    rejected = gate.confirm(activation_digest=proposal["activation_digest"], command_text=command, park_user_id="park", telegram_update_id=7, telegram_message_id=8, telegram_chat_id="chat", telegram_receipt=receipt, current_preflight=preflight, now=1787350000)
    assert rejected["code"] == "preflight_recheck_failed"
    assert gate.canary_status()["ready"] is False


def test_hyperliquid_live_adapter_does_not_trust_legacy_json(tmp_path: Path) -> None:
    root = tmp_path / "outputs"
    (root / "live_activation").mkdir(parents=True)
    (root / "live_activation" / "2026-08-22.json").write_text(
        '[{"status":"real_money_ready","real_money_ready":true}]\n', encoding="utf-8"
    )
    adapter = LiveBrokerAdapter(root, True, {"provider": "hyperliquid", "broker_id": "hyperliquid", "dry_run": False})
    adapter.preflight = lambda: {"ready": True, "block_reason": ""}
    request = BrokerOrderRequest("2026-08-22", {"ticket_id": "ticket-1", "asset": "BTC", "side": "buy", "entry_zone": "1-2"})
    try:
        adapter.submit_order(request)
    except RuntimeError as exc:
        assert "source-bound Live activation/canary" in str(exc)
    else:
        raise AssertionError("legacy real_money_ready JSON must not authorize Hyperliquid")
