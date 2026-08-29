from __future__ import annotations

from pathlib import Path

from services.dashboard_control_evidence import (
    DashboardControlEvidenceStore,
    build_dashboard_control_evidence,
)


def _snapshot():
    digest = "sha256:" + "d" * 64
    return {
        "catalog": {
            "venue_profiles": [
                {
                    "id": "hyperliquid.testnet",
                    "environment": "testnet",
                    "instruments": [{"instrument_id": "BTC-USD-PERP", "eligibility": "eligible"}],
                }
            ]
        },
        "selection": {
            "venue_profile_id": "hyperliquid.testnet",
            "instrument_id": "BTC-USD-PERP",
            "strategy_family": "dca",
        },
        "preview": {
            "venue_profile_id": "hyperliquid.testnet",
            "instrument_id": "BTC-USD-PERP",
            "strategy_family": "dca",
            "preview_digest": digest,
            "execution_ready": True,
            "blockers": [],
            "authorizing": False,
        },
        "confirmation": {
            "status": "confirmed",
            "preview_digest": digest,
            "plan_digest": digest,
            "environment": "testnet",
            "instrument_id": "BTC-USD-PERP",
            "strategy_family": "dca",
            "execution_mutation": False,
        },
        "runtime": {
            "status": "activated",
            "instrument_id": "BTC-USD-PERP",
            "strategy_family": "dca",
            "execution_mutation": False,
        },
    }


def test_evidence_projects_complete_dashboard_flow_without_claiming_external_proof() -> None:
    result = build_dashboard_control_evidence(**_snapshot())

    assert result["status"] == "ready"
    assert all(check["status"] == "pass" for check in result["checks"])
    assert result["external_testnet_proof"]["status"] == "human_required"
    assert result["safety"]["orders_submitted"] is False
    assert result["safety"]["credentials_exposed"] is False


def test_evidence_blocks_mismatched_identity_and_preserves_next_action() -> None:
    snapshot = _snapshot()
    snapshot["confirmation"] = {**snapshot["confirmation"], "instrument_id": "ETH-USD-PERP"}

    result = build_dashboard_control_evidence(**snapshot)

    assert result["status"] == "blocked"
    assert "confirmation_instrument_mismatch" in result["blockers"]
    assert result["next_action"] == "fix_identity_or_rebuild_preview"


def test_evidence_store_writes_append_only_redacted_receipts(tmp_path: Path) -> None:
    store = DashboardControlEvidenceStore(tmp_path)
    result = store.record(_snapshot())

    assert result["receipt_id"].startswith("dashboard-acceptance:")
    rows = (tmp_path / "dashboard_control_plane" / "acceptance.json").read_text()
    assert "private_key" not in rows
    assert "secret" not in rows


def test_dashboard_acceptance_builder_returns_human_proof_boundary(tmp_path: Path) -> None:
    from pipelines import dashboard_server

    result = dashboard_server.build_dashboard_control_acceptance_response(
        {"snapshot": _snapshot()},
        output_root=tmp_path,
    )

    assert result["evidence"]["status"] == "ready"
    assert result["evidence"]["external_testnet_proof"]["status"] == "human_required"
    assert result["safety"]["orders_submitted"] is False
