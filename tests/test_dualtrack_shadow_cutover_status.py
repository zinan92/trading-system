from __future__ import annotations

from pathlib import Path

import pipelines.dualtrack_shadow_cutover_status as status_pipeline
from services.journal_store import load_json, write_json


def _report(
    cycle_id: str,
    status: str,
    *,
    blocker: str = "",
    qualifies_for_cutover: bool = True,
    cycle_complete: bool = True,
) -> dict:
    result = {"cycle_id": cycle_id, "status": status}
    if blocker:
        result["blocker"] = blocker
    result["shadow_evidence"] = {
        "qualification_contract_version": "dualtrack-shadow-qualification-v2",
        "qualification_checks": {
            "cycle_complete": cycle_complete,
            "cycle_close_artifact": cycle_complete,
        },
        "qualification_blocker": "" if cycle_complete else "cycle_incomplete",
        "qualifies_for_cutover": qualifies_for_cutover,
    }
    return result


def test_cutover_status_requires_exactly_seven_trailing_passes(tmp_path: Path) -> None:
    root = tmp_path / "outputs"
    write_json(root / "dualtrack" / "nautilus" / "parity" / "current.json", [{"status": "pass", "blockers": []}])
    for index in range(7):
        write_json(
            root / "dualtrack" / "reconciliation" / f"2026-07-{index + 1:02d}_DAY.json",
            [_report(f"2026-07-{index + 1:02d}_DAY", "pass")],
        )

    result = status_pipeline.build_cutover_status(root)

    assert result["status"] == "ready_for_attended_paper_switch"
    assert result["schema_version"] == "dualtrack-shadow-cutover-gate-v2"
    assert result["configured_engine_changed"] is False
    assert result["real_money_eligible"] is False
    assert result["observed_consecutive_passes"] == 7


def test_cutover_status_blocks_on_latest_drift_or_missing_history(tmp_path: Path) -> None:
    root = tmp_path / "outputs"
    empty = status_pipeline.build_cutover_status(root)
    assert empty["status"] == "blocked"
    assert empty["blocker"] == "fixed_parity_fixtures_not_passed"

    write_json(root / "dualtrack" / "nautilus" / "parity" / "current.json", [{"status": "pass", "blockers": []}])

    write_json(
        root / "dualtrack" / "reconciliation" / "2026-07-10_DAY.json",
        [_report("2026-07-10_DAY", "blocked", blocker="candidate_snapshot_missing")],
    )
    result = status_pipeline.build_cutover_status(root)

    assert result["status"] == "blocked"
    assert result["blocker"] == "candidate_snapshot_missing"


def test_cutover_status_does_not_count_empty_shadow_replay_as_qualification(tmp_path: Path) -> None:
    root = tmp_path / "outputs"
    write_json(root / "dualtrack" / "nautilus" / "parity" / "current.json", [{"status": "pass", "blockers": []}])
    write_json(
        root / "dualtrack" / "reconciliation" / "2026-07-10_DAY.json",
        [_report("2026-07-10_DAY", "pass", qualifies_for_cutover=False)],
    )

    result = status_pipeline.build_cutover_status(root)

    assert result["status"] == "blocked"
    assert result["observed_consecutive_passes"] == 0
    assert result["blocker"] == "candidate_activity_insufficient"


def test_legacy_qualification_without_v2_close_evidence_does_not_count(tmp_path: Path) -> None:
    root = tmp_path / "outputs"
    write_json(root / "dualtrack" / "nautilus" / "parity" / "current.json", [{"status": "pass", "blockers": []}])
    write_json(
        root / "dualtrack" / "reconciliation" / "2026-07-10_DAY.json",
        [{
            "cycle_id": "2026-07-10_DAY",
            "status": "pass",
            "shadow_evidence": {"qualifies_for_cutover": True},
        }],
    )

    result = status_pipeline.build_cutover_status(root)

    assert result["status"] == "blocked"
    assert result["observed_consecutive_passes"] == 0
    assert result["blocker"] == "completed_cycle_evidence_missing"


def test_open_cycle_observation_does_not_reset_seven_completed_passes(tmp_path: Path) -> None:
    root = tmp_path / "outputs"
    write_json(root / "dualtrack" / "nautilus" / "parity" / "current.json", [{"status": "pass", "blockers": []}])
    for index in range(7):
        cycle_id = f"2026-07-{index + 1:02d}_DAY"
        write_json(
            root / "dualtrack" / "reconciliation" / f"{cycle_id}.json",
            [_report(cycle_id, "pass")],
        )
    write_json(
        root / "dualtrack" / "reconciliation" / "2026-07-08_DAY.json",
        [_report("2026-07-08_DAY", "pass", qualifies_for_cutover=False, cycle_complete=False)],
    )

    result = status_pipeline.build_cutover_status(root)

    assert result["status"] == "ready_for_attended_paper_switch"
    assert result["observed_consecutive_passes"] == 7
    assert result["latest_completed_cycle_id"] == "2026-07-07_DAY"
    assert result["latest_observation_cycle_id"] == "2026-07-08_DAY"


def test_open_cycle_drift_blocks_cutover_without_erasing_completed_streak(tmp_path: Path) -> None:
    root = tmp_path / "outputs"
    write_json(root / "dualtrack" / "nautilus" / "parity" / "current.json", [{"status": "pass", "blockers": []}])
    for index in range(7):
        cycle_id = f"2026-07-{index + 1:02d}_DAY"
        write_json(
            root / "dualtrack" / "reconciliation" / f"{cycle_id}.json",
            [_report(cycle_id, "pass")],
        )
    write_json(
        root / "dualtrack" / "reconciliation" / "2026-07-08_DAY.json",
        [_report("2026-07-08_DAY", "drift", qualifies_for_cutover=False, cycle_complete=False)],
    )

    result = status_pipeline.build_cutover_status(root)

    assert result["status"] == "blocked"
    assert result["observed_consecutive_passes"] == 7
    assert result["blocker"] == "current_observation_drift"


def test_completed_drift_resets_completed_cycle_streak(tmp_path: Path) -> None:
    root = tmp_path / "outputs"
    write_json(root / "dualtrack" / "nautilus" / "parity" / "current.json", [{"status": "pass", "blockers": []}])
    write_json(
        root / "dualtrack" / "reconciliation" / "2026-07-07_DAY.json",
        [_report("2026-07-07_DAY", "pass")],
    )
    write_json(
        root / "dualtrack" / "reconciliation" / "2026-07-08_DAY.json",
        [_report("2026-07-08_DAY", "drift", qualifies_for_cutover=False)],
    )

    result = status_pipeline.build_cutover_status(root)

    assert result["status"] == "blocked"
    assert result["observed_consecutive_passes"] == 0
    assert result["blocker"] == "latest_reconciliation_drift"


def test_cutover_cli_writes_operator_gate_without_changing_engine(tmp_path: Path) -> None:
    result = status_pipeline.main(["--output-root", str(tmp_path / "outputs")])

    assert result == 2
    saved = load_json(tmp_path / "outputs" / "dualtrack" / "cutover" / "shadow_gate_current.json")[-1]
    assert saved["configured_engine_changed"] is False
