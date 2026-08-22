from pathlib import Path

from services.journal_store import write_json
from pipelines.live_activation_protocol import create_proposal
from services.paper_release_receipt import current_source_attestation
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
    approved = {
        "status": "approved",
        "strategy_scope": "dca",
        "plan_digest": "sha256:" + "c" * 64,
        "approval_receipt_digest": "sha256:" + "f" * 64,
        "canonical_plan": {"strategy_type": "dca", "plan_digest": "sha256:" + "c" * 64},
    }
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

