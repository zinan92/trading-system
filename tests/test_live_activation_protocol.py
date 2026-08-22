import hashlib
import json
from pathlib import Path

from services.journal_store import write_json
from pipelines.live_activation_protocol import create_proposal
from services.paper_release_receipt import current_source_attestation
from services.park_telegram_runtime import ParkTelegramRouter
from tests.test_live_activation_gate import _preflight, _ready_gate


def test_production_proposal_seam_creates_only_local_confirmation_proposal(tmp_path: Path, monkeypatch) -> None:
    output_root = tmp_path / "outputs"
    gate, readiness, rows = _ready_gate(tmp_path)
    reviews = gate._readiness_reviews_resolver()
    soak_root = output_root / "dualtrack" / "testnet_soak"
    write_json(soak_root / "readiness_receipts.json", [readiness])
    write_json(soak_root / "windows.json", rows)
    write_json(soak_root / "reviews.json", reviews)
    monkeypatch.setenv("HL_MAINNET_CREDENTIAL", "fixture-only")
    facts = _preflight(gate)
    approval_proposal_id = "park-plan-approval-1"
    approval_time = 1787350000
    plan_digest = "sha256:" + "c" * 64
    approval_receipt_digest = "sha256:" + hashlib.sha256(f"{approval_proposal_id}|{plan_digest}|confirmed|{approval_time}".encode()).hexdigest()
    approved = {
        "status": "approved",
        "strategy_scope": "dca",
        "strategy_session_id": "session-continuous",
        "strategy_revision_id": "revision-dca",
        "plan_digest": plan_digest,
        "approval_receipt_digest": approval_receipt_digest,
        "canonical_plan": {"strategy_type": "dca", "plan_digest": plan_digest},
    }
    confirmation_path = output_root / "park_strategy" / "confirmations.jsonl"
    confirmation_path.parent.mkdir(parents=True, exist_ok=True)
    confirmation_path.write_text(
        "\n".join(
            json.dumps(row)
            for row in [
                {"event": "proposal", "proposal_id": approval_proposal_id, "plan_digest": plan_digest, "strategy_session_id": "session-continuous", "strategy_revision_id": "revision-dca", "expires_at": 4102444800},
                {"event": "confirmed", "proposal_id": approval_proposal_id, "plan_digest": plan_digest, "receipt_digest": approval_receipt_digest, "execution_authorized": True, "execution_environment": "testnet", "confirmed_at": approval_time, "park_user_id": "park", "strategy_session_id": "session-continuous", "strategy_revision_id": "revision-dca"},
            ]
        ) + "\n",
        encoding="utf-8",
    )
    result = create_proposal(
        output_root=output_root,
        park_user_id="park",
        park_chat_id="chat",
        account_id=facts["account_id"],
        environment_fingerprint=facts["environment_fingerprint"],
        release_sha=current_source_attestation(Path(__file__).resolve().parents[1])["source_sha"],
        credential_source="HL_MAINNET_CREDENTIAL",
        plan_digest=approved["plan_digest"],
        expires_at=4102444800,
        capabilities=facts["capabilities"],
        risk_limits=facts["risk_limits"],
        readiness=readiness,
        approved_plan=approved,
    )
    assert result["status"] == "awaiting_confirmation"
    assert result["proposal"]["live_writes_enabled"] is False
    assert result["network_io"] is False
    assert result["proposal"]["plan_digest"] == approved["plan_digest"]
    command = "confirm live " + " ".join([
        result["proposal"]["activation_digest"],
        facts["release_sha"],
        facts["account_id"],
        facts["environment_fingerprint"],
        "dca",
        approved["plan_digest"],
    ])
    router = ParkTelegramRouter(output_root, park_user_id="park", chat_id="chat")
    confirmed = router.handle_update({"update_id": 100, "message": {"message_id": 200, "from": {"id": "park"}, "chat": {"id": "chat"}, "text": command}})
    assert confirmed["status"] == "live_activation_confirmed"
    assert confirmed["decision"]["execution_authorized"] is False
