from pathlib import Path

import pytest

from services.journal_store import load_json, write_json
from services.paper_degradation_events import PaperDegradationEventStore
from services.paper_supervisor_exception_provenance import (
    PaperSupervisorExceptionStore,
)
from services.strategy_cycle_package import (
    StrategyCyclePackager,
    _hash_payload,
    load_latest_verified_cycle_package,
)


def _plan(cycle_id: str) -> dict:
    return {
        "cycle_id": cycle_id,
        "strategy_plan_id": "plan-1",
        "version": 3,
        "status": "active",
        "direction": "neutral",
    }


class TerminalAdapter:
    name = "nautilus_paper"

    def snapshot(self, _cycle_id: str) -> dict:
        return {
            "engine": self.name,
            "orders": [{"order_id": "o1", "state": "cancelled", "strategy_plan_id": "plan-1"}],
            "fills": [{"fill_id": "f1", "realized_pnl": 3.5, "strategy_plan_id": "plan-1"}],
            "positions": [],
            "account": {"equity": 10_003.5},
            "pnl": {"realized": 3.5, "unrealized": 0.0},
        }

    def reconcile(self, _cycle_id: str) -> dict:
        return {"status": "ok", "issues": []}


def _seed_plan(output: Path, cycle_id: str) -> None:
    write_json(
        output / "dualtrack" / "strategy_control" / "plans" / f"{cycle_id}.json",
        [_plan(cycle_id)],
    )


def test_terminal_package_is_hashed_linked_and_idempotent(tmp_path: Path) -> None:
    output = tmp_path / "outputs"
    cycle_id = "2026-07-05_DAY"
    _seed_plan(output, cycle_id)
    packager = StrategyCyclePackager(output, adapter=TerminalAdapter())

    first = packager.package(cycle_id, now="2026-07-05T13:00:00+00:00")
    second = packager.package(cycle_id, now="2026-07-05T13:01:00+00:00")

    assert first == second
    assert first["status"] == "closed"
    assert first["strategy_plan"]["version"] == 3
    assert first["execution"]["pnl"]["realized"] == 3.5
    assert first["review"]["realized_pnl"] == 3.5
    assert first["traceability"]["orders_linked"] is True
    assert first["traceability"]["fills_linked"] is True
    assert first["schema_version"] == "strategy-cycle-package-v2"
    assert first["degradation_events"] == []
    assert first["degradation_event_count"] == 0
    assert len(first["degradation_events_digest"]) == 64
    assert first["continuity_transitions"] == []
    assert first["continuity_transition_count"] == 0
    assert first["traceability"]["degradation_events_complete"] is True
    assert first["pre_intent_exception_receipts"] == []
    assert first["pre_intent_exception_receipt_count"] == 0
    assert first["traceability"][
        "pre_intent_exception_evidence_complete"
    ] is True
    assert len(first["package_hash"]) == 64
    assert len(load_json(output / "dualtrack" / "strategy_cycle_packages" / f"{cycle_id}.json")) == 1


def test_package_blocks_on_non_terminal_execution(tmp_path: Path) -> None:
    class DriftedAdapter(TerminalAdapter):
        def snapshot(self, _cycle_id: str) -> dict:
            return {
                "orders": [{"order_id": "o1", "state": "accepted"}],
                "positions": [{"position_id": "p1", "status": "open"}],
                "fills": [],
                "pnl": {"realized": 0.0},
            }

        def reconcile(self, _cycle_id: str) -> dict:
            return {"status": "drift", "issues": [{"code": "position_mismatch"}]}

    output = tmp_path / "outputs"
    cycle_id = "2026-07-05_NIGHT"
    _seed_plan(output, cycle_id)

    result = StrategyCyclePackager(output, adapter=DriftedAdapter()).package(cycle_id)

    assert result["status"] == "blocked"
    assert result["review"]["status"] == "blocked"
    assert result["execution"]["reconciliation"]["status"] == "drift"


def test_terminal_package_embeds_complete_pre_intent_exception_chain(
    tmp_path: Path,
) -> None:
    output = tmp_path / "outputs"
    cycle_id = "2026-07-06_DAY"
    _seed_plan(output, cycle_id)
    evidence_store = PaperSupervisorExceptionStore(
        output,
        source_attestation=lambda: {
            "source_sha": "a" * 40,
            "source_tree_sha": "b" * 40,
            "tracked_tree_clean": True,
        },
    )
    receipt = evidence_store.record_exception(
        cycle_id=cycle_id,
        attempt_id="supervisor-attempt-package-proof",
        phase="provider_fallback",
        occurred_at="2026-07-06T01:00:00+00:00",
        exc=RuntimeError("fallback failed token=secret-value"),
    )

    package = StrategyCyclePackager(
        output,
        adapter=TerminalAdapter(),
    ).package(cycle_id)

    assert package["pre_intent_exception_receipts"] == [receipt]
    assert package["pre_intent_exception_receipt_count"] == 1
    assert package["pre_intent_exception_receipt_tail_digest"] == (
        receipt["receipt_digest"]
    )
    package_path = (
        output
        / "dualtrack"
        / "strategy_cycle_packages"
        / f"{cycle_id}.json"
    )
    assert load_latest_verified_cycle_package(package_path)[
        "pre_intent_exception_receipts"
    ] == [receipt]

    rows = load_json(package_path)
    rows[0]["pre_intent_exception_receipts"][0][
        "redacted_message"
    ] = "rewritten"
    rows[0]["package_hash"] = _hash_payload(
        {
            key: value
            for key, value in rows[0].items()
            if key != "package_hash"
        }
    )
    write_json(package_path, rows)
    with pytest.raises(
        ValueError,
        match="exception evidence is invalid",
    ):
        load_latest_verified_cycle_package(package_path)


def test_verified_handoff_closes_books_without_closing_positions(tmp_path: Path) -> None:
    class HandedOffAdapter(TerminalAdapter):
        def snapshot(self, _cycle_id: str) -> dict:
            return {
                "engine": self.name,
                "orders": [{
                    "order_id": "o-open",
                    "state": "accepted",
                    "strategy_plan_id": "plan-1",
                }],
                "fills": [{
                    "fill_id": "f-entry",
                    "realized_pnl": 2.0,
                    "strategy_plan_id": "plan-1",
                }],
                "positions": [{
                    "position_id": "p-open",
                    "status": "open",
                    "strategy_plan_id": "plan-1",
                }],
                "account": {"equity": 10_006.0},
                "pnl": {"realized": 2.0, "unrealized": 4.0},
            }

    output = tmp_path / "outputs"
    cycle_id = "2026-07-05_NIGHT"
    _seed_plan(output, cycle_id)
    handoff = {
        "status": "verified",
        "identity_preserved": True,
        "previous_cycle_id": cycle_id,
        "current_cycle_id": "2026-07-06_DAY",
        "accepted_order_ids": ["o-open"],
        "open_position_ids": ["p-open"],
    }

    result = StrategyCyclePackager(
        output,
        adapter=HandedOffAdapter(),
    ).package(cycle_id, handoff=handoff)

    assert result["status"] == "closed"
    assert result["execution"]["terminal_mode"] == "handed_off"
    assert result["execution"]["handoff"]["current_cycle_id"] == "2026-07-06_DAY"
    assert result["review"]["status"] == "complete"
    assert result["review"]["terminal_mode"] == "handed_off"
    assert result["traceability"]["cycle_handoff_verified"] is True


def test_package_blocks_when_strategy_plan_identity_is_missing(tmp_path: Path) -> None:
    result = StrategyCyclePackager(
        tmp_path / "outputs",
        adapter=TerminalAdapter(),
    ).package("2026-07-02_DAY")

    assert result["status"] == "blocked"
    assert result["blockers"] == ["strategy_plan_link_missing"]
    assert result["shadow_generation"]["attempted"] is False


def test_repair_appends_and_supersedes_without_overwrite(tmp_path: Path) -> None:
    output = tmp_path / "outputs"
    cycle_id = "2026-07-04_DAY"
    path = output / "dualtrack" / "strategy_cycle_packages" / f"{cycle_id}.json"
    original = {
        "cycle_id": cycle_id,
        "status": "closed",
        "execution": {"pnl": {"realized": 8.125}},
        "review": {"realized_pnl": 0.0},
    }
    original["package_hash"] = _hash_payload(original)
    write_json(path, [original])

    repaired = StrategyCyclePackager(output).package(cycle_id, now="2026-07-05T01:00:00+00:00")
    rows = load_json(path)

    assert rows[0] == original
    assert len(rows) == 2
    assert repaired["review"]["realized_pnl"] == 8.125
    assert repaired["revision"]["supersedes_package_hash"] == original["package_hash"]
    assert repaired["revision"]["original_record_preserved"] is True


def test_shadow_failure_is_recorded_once_without_blocking_terminal_close(tmp_path: Path) -> None:
    output = tmp_path / "outputs"
    cycle_id = "2026-07-03_NIGHT"
    _seed_plan(output, cycle_id)
    attempts = 0

    def broken_shadow(*_args):
        nonlocal attempts
        attempts += 1
        raise RuntimeError("shadow unavailable")

    packager = StrategyCyclePackager(
        output,
        adapter=TerminalAdapter(),
        shadow_evidence_builder=broken_shadow,
    )
    first = packager.package(cycle_id)
    second = packager.package(cycle_id)

    assert first == second
    assert first["status"] == "closed"
    assert first["shadow_generation"]["status"] == "blocked"
    assert first["shadow_generation"]["retry"] == "explicit_new_package_revision_only"
    assert attempts == 1


def test_blocked_package_refreshes_execution_evidence_and_can_close(tmp_path: Path) -> None:
    output = tmp_path / "outputs"
    cycle_id = "2026-07-02_NIGHT"
    _seed_plan(output, cycle_id)

    class RecoveringAdapter(TerminalAdapter):
        def __init__(self):
            self.reconciliations = 0

        def reconcile(self, _cycle_id: str) -> dict:
            self.reconciliations += 1
            if self.reconciliations == 1:
                return {"status": "drift", "issues": [{"code": "transient"}]}
            return {"status": "ok", "issues": []}

    adapter = RecoveringAdapter()
    packager = StrategyCyclePackager(output, adapter=adapter)
    blocked = packager.package(cycle_id, now="2026-07-03T01:00:00+00:00")
    closed = packager.package(cycle_id, now="2026-07-03T01:01:00+00:00")

    assert blocked["status"] == "blocked"
    assert closed["status"] == "closed"
    assert closed["revision"]["supersedes_package_hash"] == blocked["package_hash"]
    assert len(load_json(output / "dualtrack" / "strategy_cycle_packages" / f"{cycle_id}.json")) == 2


def test_tampered_closed_package_fails_integrity_before_it_can_be_reused(tmp_path: Path) -> None:
    output = tmp_path / "outputs"
    cycle_id = "2026-07-01_DAY"
    _seed_plan(output, cycle_id)
    path = output / "dualtrack" / "strategy_cycle_packages" / f"{cycle_id}.json"
    original = StrategyCyclePackager(output, adapter=TerminalAdapter()).package(cycle_id)
    tampered = {**original, "status": "closed", "package_hash": "forged"}
    write_json(path, [tampered])

    class CountingAdapter(TerminalAdapter):
        def __init__(self):
            self.snapshot_calls = 0

        def snapshot(self, cycle_id: str) -> dict:
            self.snapshot_calls += 1
            return super().snapshot(cycle_id)

    adapter = CountingAdapter()
    packager = StrategyCyclePackager(output, adapter=adapter)

    incident = packager.package(cycle_id)
    repeated = packager.package(cycle_id)

    assert incident["status"] == "blocked"
    assert repeated == incident
    assert incident["blockers"] == ["package_hash_mismatch"]
    assert incident["integrity"]["source_record_trusted"] is False
    assert incident["revision"]["supersedes_package_hash"] is None
    assert adapter.snapshot_calls == 0

    before_rejected_ack = load_json(path)
    with pytest.raises(ValueError, match="requires an actor"):
        packager.acknowledge_integrity_incident(
            cycle_id,
            incident_package_hash=incident["package_hash"],
            actor={"id": "", "transport": ""},
        )
    assert load_json(path) == before_rejected_ack

    acknowledged = packager.acknowledge_integrity_incident(
        cycle_id,
        incident_package_hash=incident["package_hash"],
        actor={"id": "park-ai-bot", "transport": "github_issue"},
        now="2026-07-02T01:00:00+00:00",
    )
    repaired = packager.package(cycle_id, now="2026-07-02T01:01:00+00:00")

    assert acknowledged["integrity"]["status"] == "acknowledged"
    assert acknowledged["integrity"]["acknowledged_by"] == {
        "id": "park-ai-bot",
        "transport": "github_issue",
    }
    assert repaired["status"] == "closed"
    assert repaired["revision"]["supersedes_package_hash"] == acknowledged["package_hash"]
    assert adapter.snapshot_calls == 1


def test_verified_package_listing_excludes_tampered_closed_journals(tmp_path: Path) -> None:
    output = tmp_path / "outputs"
    valid_cycle = "2026-07-02_DAY"
    tampered_cycle = "2026-07-01_DAY"
    for cycle_id in (valid_cycle, tampered_cycle):
        _seed_plan(output, cycle_id)
        StrategyCyclePackager(output, adapter=TerminalAdapter()).package(cycle_id)
    tampered_path = output / "dualtrack" / "strategy_cycle_packages" / f"{tampered_cycle}.json"
    rows = load_json(tampered_path)
    rows[-1] = {**rows[-1], "review": {"realized_pnl": 999_999.0}}
    write_json(tampered_path, rows)

    packages = StrategyCyclePackager(output).list_verified_packages(limit=12)

    assert [row["cycle_id"] for row in packages] == [valid_cycle]
    assert packages[0]["review"]["realized_pnl"] == 3.5


def test_terminal_package_embeds_complete_degradation_event_chain(
    tmp_path: Path,
) -> None:
    output = tmp_path / "outputs"
    cycle_id = "2026-07-02_DAY"
    _seed_plan(output, cycle_id)
    event = PaperDegradationEventStore(output).record(
        event_id="degradation-attempt-1",
        cycle_id=cycle_id,
        execution_profile="paper_continuous",
        bypassed_gate="prepared_start_market_gate",
        original_machine_code="prepared_start_market_moved",
        original_reason="price moved",
        alternative_action="recompute_fresh_preview",
        occurred_at="2026-07-02T01:04:00+00:00",
    )

    package = StrategyCyclePackager(
        output,
        adapter=TerminalAdapter(),
    ).package(cycle_id)

    assert package["degradation_events"] == [event]
    assert package["degradation_event_count"] == 1
    assert package["degradation_event_tail_digest"] == event["event_digest"]
    assert load_latest_verified_cycle_package(
        output
        / "dualtrack"
        / "strategy_cycle_packages"
        / f"{cycle_id}.json"
    ) == package


def test_v2_package_with_missing_referenced_event_fails_closed(
    tmp_path: Path,
) -> None:
    output = tmp_path / "outputs"
    cycle_id = "2026-07-03_DAY"
    _seed_plan(output, cycle_id)
    PaperDegradationEventStore(output).record(
        event_id="degradation-attempt-1",
        cycle_id=cycle_id,
        execution_profile="paper_continuous",
        bypassed_gate="risk_envelope_gate",
        original_machine_code="risk_envelope_preview_out_of_bounds",
        original_reason="proposal exceeded authorized boundary",
        alternative_action="reprice_from_authoritative_equity",
        occurred_at="2026-07-03T01:04:00+00:00",
    )
    package = StrategyCyclePackager(
        output,
        adapter=TerminalAdapter(),
    ).package(cycle_id)
    path = (
        output
        / "dualtrack"
        / "strategy_cycle_packages"
        / f"{cycle_id}.json"
    )
    malformed = {**package, "degradation_events": []}
    malformed["package_hash"] = _hash_payload(
        {
            key: value
            for key, value in malformed.items()
            if key != "package_hash"
        }
    )
    write_json(path, [malformed])

    with pytest.raises(
        ValueError,
        match="degradation evidence is invalid",
    ):
        load_latest_verified_cycle_package(path)


def test_tampered_historical_revision_latches_integrity_incident(tmp_path: Path) -> None:
    output = tmp_path / "outputs"
    cycle_id = "2026-07-01_NIGHT"
    _seed_plan(output, cycle_id)
    path = output / "dualtrack" / "strategy_cycle_packages" / f"{cycle_id}.json"

    class RecoveringAdapter(TerminalAdapter):
        def __init__(self):
            self.reconciliations = 0

        def reconcile(self, _cycle_id: str) -> dict:
            self.reconciliations += 1
            if self.reconciliations == 1:
                return {"status": "drift", "issues": [{"code": "transient"}]}
            return {"status": "ok", "issues": []}

    packager = StrategyCyclePackager(output, adapter=RecoveringAdapter())
    packager.package(cycle_id)
    closed = packager.package(cycle_id)
    rows = load_json(path)
    rows[0] = {**rows[0], "status": "closed"}
    write_json(path, rows)

    incident = packager.package(cycle_id)
    repeated = packager.package(cycle_id)

    assert closed["status"] == "closed"
    assert incident["blockers"] == ["package_hash_mismatch"]
    assert incident["integrity"]["source_record_index"] == 0
    assert incident["integrity"]["failure_reason"] == "package_hash_mismatch"
    assert repeated == incident
