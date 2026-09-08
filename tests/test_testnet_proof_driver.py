from __future__ import annotations

import json
from pathlib import Path

import pytest

from pipelines.testnet_proof_driver import ProofDriverError, build_plan, map_confirmation


def _preview() -> tuple[dict, dict]:
    digest = "sha256:" + "a" * 64
    confirmation = {
        "status": "confirmed",
        "activation_id": "sha256:" + "b" * 64,
        "preview_digest": digest,
        "plan_digest": digest,
        "strategy_family": "grid",
        "instrument_id": "BTC-USD-PERP",
        "strategy_session_id": "dashboard-session:a",
        "strategy_revision_id": "dashboard-revision:a",
        "confirmation_id": "dashboard-confirmation:a",
    }
    preview = {
        "preview_digest": digest,
        "strategy_family": "grid",
        "instrument_id": "BTC-USD-PERP",
        "preview": {
            "direction": "long",
            "market": {"timestamp": "2026-09-08T01:00:00+00:00"},
            "range": {"low": 90, "high": 110},
            "grid": {"hard_stop": 80},
            "orders": [
                {"level": 0, "side": "buy", "price": 90, "quantity": "1", "tp": 95},
                {"level": 1, "side": "buy", "price": 95, "quantity": "1", "tp": 100},
            ],
        },
    }
    # The helper is tested with a digest token representing a persisted row;
    # digest recomputation is covered by the Dashboard contract suite.
    return preview, confirmation


def test_plan_uses_preview_orders_and_keeps_plan_digest() -> None:
    preview, confirmation = _preview()
    plan = build_plan(preview, confirmation)

    assert plan["plan_digest"] == preview["preview_digest"]
    assert plan["grid"]["rungs"] == [
        {"rung": 0, "side": "buy", "price": 90, "quantity": "1", "tp": 95, "hard_stop": 80},
        {"rung": 1, "side": "buy", "price": 95, "quantity": "1", "tp": 100, "hard_stop": 80},
    ]


def test_confirmation_mapping_rejects_dashboard_authorization_forgery(tmp_path: Path) -> None:
    _, dashboard = _preview()
    dashboard["execution_authorized"] = True
    path = tmp_path / "park_strategy" / "confirmations.jsonl"
    path.parent.mkdir()
    path.write_text(
        json.dumps({"event": "proposal", "proposal_id": "p", "plan_digest": dashboard["plan_digest"]})
        + "\n"
        + json.dumps({"event": "rejected", "proposal_id": "p", "plan_digest": dashboard["plan_digest"]}),
        encoding="utf-8",
    )

    with pytest.raises(ProofDriverError, match="durable_park_confirmation_missing"):
        map_confirmation(tmp_path, dashboard, approval_id="approval", approved_by="park")


def test_confirmation_mapping_requires_explicit_park_approval(tmp_path: Path) -> None:
    _, dashboard = _preview()
    with pytest.raises(ProofDriverError, match="park_approval_required"):
        map_confirmation(tmp_path, dashboard, approval_id="", approved_by="park")
