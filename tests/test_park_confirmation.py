from __future__ import annotations

from pathlib import Path

import pytest

from services.park_confirmation import (
    ParkConfirmationError,
    ParkConfirmationLedger,
    parse_confirmation_command,
    parse_confirmation_shortcut,
)


def _ledger(tmp_path: Path) -> ParkConfirmationLedger:
    return ParkConfirmationLedger(tmp_path / "outputs", park_user_id="park-user")


def _proposal(ledger: ParkConfirmationLedger, *, expires_at: float = 200.0) -> dict:
    return ledger.create_proposal(
        proposal_id="proposal-1",
        strategy_session_id="session-1",
        strategy_revision_id="revision-1",
        plan_digest="sha256:" + "a" * 64,
        risk_digest="sha256:" + "b" * 64,
        expires_at=expires_at,
    )


def test_command_requires_exact_verb_and_digest() -> None:
    assert parse_confirmation_command("确认 sha256:" + "a" * 64) == ("confirm", "sha256:" + "a" * 64)
    assert parse_confirmation_command("reject sha256:" + "a" * 64)[0] == "reject"
    with pytest.raises(ParkConfirmationError, match="exact"):
        parse_confirmation_command("确认")


def test_shortcut_requires_bounded_human_confirmation_phrase() -> None:
    assert parse_confirmation_shortcut("确认当前计划") == "confirm"
    assert parse_confirmation_shortcut("confirm this plan") == "confirm"
    assert parse_confirmation_shortcut("拒绝这个计划。") == "reject"
    with pytest.raises(ParkConfirmationError, match="confirm.*当前计划"):
        parse_confirmation_shortcut("确认一下")


def test_exact_confirmation_creates_capability_not_start(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    proposal = _proposal(ledger)
    receipt = ledger.decide(
        proposal_id=proposal["proposal_id"], park_user_id="park-user",
        command_text="confirm " + proposal["plan_digest"],
        current_binding={"strategy_session_id": "session-1", "strategy_revision_id": "revision-1"}, now=100,
    )
    assert receipt["event"] == "confirmed"
    assert receipt["execution_authorized"] is True
    assert receipt["start_or_order_submitted"] is False
    assert receipt["risk_digest"] == "sha256:" + "b" * 64


def test_pending_proposals_are_bound_to_one_active_revision(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    proposal = _proposal(ledger)
    assert ledger.pending_proposals({"strategy_session_id": "session-1", "strategy_revision_id": "revision-1"}) == [proposal]
    assert ledger.pending_proposals({"strategy_session_id": "session-other", "strategy_revision_id": "revision-1"}) == []


def test_confirmation_is_idempotent_and_wrong_identity_or_digest_is_rejected(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    proposal = _proposal(ledger)
    with pytest.raises(ParkConfirmationError, match="only Park"):
        ledger.decide(proposal_id="proposal-1", park_user_id="other", command_text="confirm " + proposal["plan_digest"], current_binding={"strategy_session_id": "session-1", "strategy_revision_id": "revision-1"}, now=100)
    with pytest.raises(ParkConfirmationError, match="does not match"):
        ledger.decide(proposal_id="proposal-1", park_user_id="park-user", command_text="confirm sha256:" + "c" * 64, current_binding={"strategy_session_id": "session-1", "strategy_revision_id": "revision-1"}, now=100)
    first = ledger.decide(proposal_id="proposal-1", park_user_id="park-user", command_text="confirm " + proposal["plan_digest"], current_binding={"strategy_session_id": "session-1", "strategy_revision_id": "revision-1"}, now=100)
    second = ledger.decide(proposal_id="proposal-1", park_user_id="park-user", command_text="confirm " + proposal["plan_digest"], current_binding={"strategy_session_id": "session-1", "strategy_revision_id": "revision-1"}, now=101)
    assert first == second
    assert len([row for row in ledger.rows() if row["event"] == "confirmed"]) == 1


def test_expiry_and_stale_revision_fail_closed(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    proposal = _proposal(ledger, expires_at=100)
    with pytest.raises(ParkConfirmationError, match="expired"):
        ledger.decide(proposal_id="proposal-1", park_user_id="park-user", command_text="confirm " + proposal["plan_digest"], current_binding={"strategy_session_id": "session-1", "strategy_revision_id": "revision-1"}, now=101)
    ledger = _ledger(tmp_path / "second")
    proposal = _proposal(ledger)
    with pytest.raises(ParkConfirmationError, match="stale"):
        ledger.decide(proposal_id="proposal-1", park_user_id="park-user", command_text="confirm " + proposal["plan_digest"], current_binding={"strategy_session_id": "session-1", "strategy_revision_id": "revision-2"}, now=100)


def test_rejection_is_durable_and_record_window_cannot_bind_confirmation(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    proposal = _proposal(ledger)
    with pytest.raises(ParkConfirmationError, match="stale"):
        ledger.decide(proposal_id="proposal-1", park_user_id="park-user", command_text="reject " + proposal["plan_digest"], current_binding={"record_window_id": "2026-08-14_DAY"}, now=100)
    rejected = ledger.decide(proposal_id="proposal-1", park_user_id="park-user", command_text="reject " + proposal["plan_digest"], current_binding={"strategy_session_id": "session-1", "strategy_revision_id": "revision-1"}, now=100)
    assert rejected["event"] == "rejected"
    assert rejected["execution_authorized"] is False
    assert rejected["next_action"] == "await_new_proposal"
