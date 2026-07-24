from pathlib import Path

import pytest

from services.safe_repair_queue import SafeRepairQueue, build_repair_proposal


def test_safe_repair_queue_is_append_only_and_records_verification_evidence(tmp_path: Path) -> None:
    queue = SafeRepairQueue(tmp_path)
    proposal = build_repair_proposal(
        diagnosis="dashboard read model is stale",
        domain="read_model",
        requested_action="refresh cached projection",
        before_evidence={"snapshot_id": "old"},
        observed_at="2026-07-24T00:00:00+00:00",
    )

    first = queue.enqueue(proposal)
    duplicate = queue.enqueue(proposal)
    receipt = queue.record_verification(
        first["repair_id"],
        after_evidence={"snapshot_id": "new"},
        verified=True,
        verified_at="2026-07-24T00:01:00+00:00",
    )
    model = queue.read_model()

    assert duplicate == first
    assert receipt["status"] == "verified"
    assert len(model["records"]) == 1
    assert model["records"][0]["before_evidence"] == {"snapshot_id": "old"}
    assert model["records"][0]["verification"]["after_evidence"] == {"snapshot_id": "new"}
    assert model["counts"] == {
        "queued": 0,
        "verified": 1,
        "verification_failed": 0,
        "requires_human_confirmation": 0,
    }
    assert model["safety"]["automatic_actuator_present"] is False


@pytest.mark.parametrize("domain", ["orders", "positions", "risk", "strategy_plan", "execution_engine", "market_provider"])
def test_trading_domains_require_human_confirmation_and_never_get_execution_authority(domain: str) -> None:
    proposal = build_repair_proposal(
        diagnosis="needs operator review",
        domain=domain,
        requested_action="do not automate",
        before_evidence={"domain": domain},
        observed_at="2026-07-24T00:00:00+00:00",
    )

    assert proposal["authority"] == {
        "classification": "requires_human_confirmation",
        "requires_human_confirmation": True,
        "automatic_candidate": False,
        "execution_allowed": False,
    }
    assert proposal["safety"]["changes_orders"] is False
    assert proposal["safety"]["changes_strategy_plan"] is False


def test_failed_verification_requires_reason_and_cannot_change_a_proposal(tmp_path: Path) -> None:
    queue = SafeRepairQueue(tmp_path)
    proposal = queue.enqueue(build_repair_proposal(
        diagnosis="cache stale",
        domain="cache",
        requested_action="refresh cache candidate",
        before_evidence={"version": 1},
        observed_at="2026-07-24T00:00:00+00:00",
    ))

    with pytest.raises(ValueError, match="failure_reason"):
        queue.record_verification(proposal["repair_id"], after_evidence={"version": 1}, verified=False)
    failed = queue.record_verification(
        proposal["repair_id"],
        after_evidence={"version": 1},
        verified=False,
        failure_reason="refresh did not change cache version",
        verified_at="2026-07-24T00:01:00+00:00",
    )

    assert failed["status"] == "verification_failed"
    assert queue.read_model()["records"][0]["requested_action"] == "refresh cache candidate"


def test_safe_repair_queue_has_no_trading_or_process_actuator_dependency() -> None:
    source = Path(__file__).parents[1] / "services" / "safe_repair_queue.py"
    text = source.read_text(encoding="utf-8")

    for forbidden in (
        "strategy_control_plane",
        "broker_adapter",
        "risk_port",
        "PaperBrokerAdapter",
        "subprocess",
        "launchctl",
    ):
        assert forbidden not in text
