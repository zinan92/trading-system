from __future__ import annotations

import json
from pathlib import Path

import pytest

from pipelines.testnet_proof_driver import ProofDriverError, build_plan, map_confirmation
from services.park_confirmation_ledger import DurableParkConfirmationError, parse_durable_confirmation


def _preview() -> tuple[dict, dict]:
    digest = "sha256:" + "a" * 64
    confirmation = {
        "status": "confirmed",
        "acknowledged": True,
        "operator_id": "park",
        "activation_id": "sha256:" + "b" * 64,
        "preview_digest": digest,
        "plan_digest": digest,
        "strategy_family": "grid",
        "instrument_id": "BTC-USD-PERP",
        "strategy_session_id": "dashboard-session:a",
        "strategy_revision_id": "dashboard-revision:a",
        "confirmation_id": "dashboard-confirmation:a",
        "confirmation_digest": "sha256:" + "d" * 64,
        "confirmed_at": "2026-09-08T01:00:00+00:00",
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


def _dashboard_root(tmp_path: Path, dashboard: dict) -> None:
    control = tmp_path / "dashboard_control_plane"
    control.mkdir(parents=True)
    (control / "confirmations.json").write_text(json.dumps([dashboard]), encoding="utf-8")
    current = tmp_path / "testnet_automation"
    current.mkdir()
    (current / "current.json").write_text(
        json.dumps([{"activation_id": dashboard["activation_id"], "plan_digest": dashboard["plan_digest"]}]),
        encoding="utf-8",
    )


def test_dashboard_confirmation_maps_to_canonical_confirmation(tmp_path: Path) -> None:
    _, dashboard = _preview()
    _dashboard_root(tmp_path, dashboard)

    result = map_confirmation(tmp_path, dashboard, approval_id="approval", approved_by="park")

    assert result["proposal_id"] == dashboard["plan_digest"]
    assert result["receipt_digest"] == dashboard["confirmation_digest"]
    assert result["execution_authorized"] is True
    assert result["confirmation_source"] == "dashboard"


def test_historical_confirm_and_run_confirmation_without_acknowledged_maps(tmp_path: Path) -> None:
    _, dashboard = _preview()
    dashboard.pop("acknowledged")
    dashboard["schema_version"] = "dashboard-confirmation-v1"
    dashboard["event"] = "operator_confirmed"
    _dashboard_root(tmp_path, dashboard)

    result = map_confirmation(tmp_path, dashboard, approval_id="approval", approved_by="park")

    assert result["confirmation_source"] == "dashboard"
    assert result["operator_id"] == "park"


def test_historical_non_confirm_and_run_confirmation_without_acknowledged_is_blocked(tmp_path: Path) -> None:
    _, dashboard = _preview()
    dashboard.pop("acknowledged")
    dashboard["event"] = "operator_confirmed_legacy_import"
    _dashboard_root(tmp_path, dashboard)

    with pytest.raises(ProofDriverError, match="dashboard_confirmation_not_acknowledged"):
        map_confirmation(tmp_path, dashboard, approval_id="approval", approved_by="park")


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("operator_id", "jessie", "dashboard_operator_invalid"),
        ("acknowledged", False, "dashboard_confirmation_not_acknowledged"),
    ],
)
def test_dashboard_confirmation_rejects_identity_and_acknowledgement_failures(
    tmp_path: Path, field: str, value: object, reason: str
) -> None:
    _, dashboard = _preview()
    dashboard[field] = value
    _dashboard_root(tmp_path, dashboard)

    with pytest.raises(ProofDriverError, match=reason):
        map_confirmation(tmp_path, dashboard, approval_id="approval", approved_by="park")


def test_dashboard_parser_rejects_plan_mismatch(tmp_path: Path) -> None:
    _, dashboard = _preview()
    _dashboard_root(tmp_path, dashboard)
    confirmation = {
        "event": "confirmed", "execution_authorized": True, "execution_environment": "testnet",
        "plan_digest": dashboard["plan_digest"], "confirmation_id": dashboard["confirmation_id"],
        "proposal_id": dashboard["plan_digest"], "receipt_digest": dashboard["confirmation_digest"],
        "confirmed_at": "2026-09-08T01:00:00+00:00", "operator_id": "park",
        "activation_id": dashboard["activation_id"], "confirmation_source": "dashboard",
    }
    with pytest.raises(DurableParkConfirmationError, match="dashboard_plan_digest_mismatch"):
        parse_durable_confirmation(
            tmp_path, plan={"plan_digest": "sha256:" + "e" * 64}, confirmation=confirmation
        )


def test_dashboard_parser_rejects_activation_mismatch(tmp_path: Path) -> None:
    _, dashboard = _preview()
    _dashboard_root(tmp_path, dashboard)
    confirmation = {
        "event": "confirmed", "execution_authorized": True, "execution_environment": "testnet",
        "plan_digest": dashboard["plan_digest"], "confirmation_id": dashboard["confirmation_id"],
        "proposal_id": dashboard["plan_digest"], "receipt_digest": dashboard["confirmation_digest"],
        "confirmed_at": dashboard["confirmed_at"], "operator_id": "park",
        "activation_id": "sha256:" + "c" * 64, "confirmation_source": "dashboard",
    }
    with pytest.raises(DurableParkConfirmationError, match="activation_identity_mismatch"):
        parse_durable_confirmation(tmp_path, plan={"plan_digest": dashboard["plan_digest"]}, confirmation=confirmation)


def test_confirmation_mapping_requires_explicit_park_approval(tmp_path: Path) -> None:
    _, dashboard = _preview()
    with pytest.raises(ProofDriverError, match="park_approval_required"):
        map_confirmation(tmp_path, dashboard, approval_id="", approved_by="park")
