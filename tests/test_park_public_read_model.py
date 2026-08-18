from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from services.journal_store import write_json
from services.park_public_read_model import build_park_public_read_model


NOW = datetime(2026, 8, 17, 0, 1, tzinfo=timezone.utc)


def _append_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )


def _fixture(root: Path) -> None:
    identity = {
        "event": "session_started",
        "strategy_session_id": "session-1",
        "strategy_revision_id": "revision-1",
        "plan_digest": "sha256:" + "a" * 64,
        "observed_at": "2026-08-16T23:00:00+00:00",
    }
    lifecycle = {
        **identity,
        "event": "plan_activated",
        "state": "ACTIVE_LOCKED",
        "strategy_type": "grid",
        "direction": "neutral",
        "lower_price_boundary": 4100.0,
        "upper_price_boundary": 4450.0,
        "maximum_leverage": 20.0,
        "maximum_acceptable_loss": 1200.0,
    }
    plan = {
        **identity,
        "event": "plan_proposed",
        "normalized_input": {
            "strategy_type": "grid",
            "direction": "neutral",
            "lower_price_boundary": 4100.0,
            "upper_price_boundary": 4450.0,
            "maximum_leverage": 20.0,
        },
        "risk": {
            "maximum_notional": 20000.0,
            "theoretical_max_loss": 1200.0,
            "order_count": 30,
            "selected_constraint": "maximum_leverage",
        },
    }
    _append_jsonl(root / "park_strategy" / "identity.jsonl", [identity])
    _append_jsonl(root / "park_strategy" / "lifecycle.jsonl", [lifecycle])
    _append_jsonl(root / "park_strategy" / "plans.jsonl", [plan])
    write_json(
        root
        / "dualtrack"
        / "nautilus_authoritative"
        / "snapshots"
        / "park-session-1.json",
        [
            {
                "engine": "nautilus_paper",
                "cycle_id": "park-session-1",
                "mark": {"price": 4400.0, "fresh": True, "source": "trusted-test"},
                "capabilities": {"paper_only": True, "immutable_fill_guard": True},
                "reconciliation": {"status": "ok", "issues": []},
                "orders": [
                    {"order_id": "order-1", "state": "accepted", "side": "buy", "price": 4300.0},
                    {"order_id": "order-2", "state": "filled", "side": "sell", "price": 4400.0},
                ],
                "fills": [{"fill_id": "fill-1", "side": "sell", "price": 4400.0}],
                "positions": [
                    {"position_id": "position-1", "status": "open", "side": "short", "remaining_units": 1.0},
                    {"position_id": "position-2", "status": "closed", "side": "long", "remaining_units": 0.0},
                ],
                "account": {"equity": 10025.0},
                "pnl": {"realized": 30.0, "unrealized": -5.0},
            }
        ],
    )
    write_json(
        root / "park_strategy" / "safety_evidence.json",
        [
            {
                "status": "pass",
                "checked_at": "2026-08-17T00:00:00+00:00",
                "release_sha": "b" * 40,
                "tracked_tree_clean": True,
                "boot_verified": True,
                "paper_only": True,
                "immutable_fill": True,
                "supervisor_fail_closed": True,
            }
        ],
    )


def _digests(root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def test_public_read_model_projects_persisted_facts_without_writes(tmp_path: Path) -> None:
    output = tmp_path / "outputs"
    _fixture(output)
    before = _digests(output)

    result = build_park_public_read_model(output, now=lambda: NOW)

    assert _digests(output) == before
    assert result["status"] == "ok"
    assert result["blockers"] == []
    assert result["viewer"] == {
        "mode": "public_read_only",
        "paper_only": True,
        "control_plane": "telegram_only",
        "mutations_allowed": False,
    }
    assert result["strategy"]["strategy_session_id"] == "session-1"
    assert result["strategy"]["direction"] == "neutral"
    assert result["strategy"]["maximum_leverage"] == 20.0
    assert result["strategy"]["grid_entry_range"] == {
        "lower": 4111.29032258,
        "upper": 4438.70967742,
    }
    assert result["strategy"]["grid_spacing"] == 11.29032258
    assert result["strategy"]["grid_rung_count"] == 30
    assert result["strategy"]["grid_rung_prices"][:2] == [4111.29032258, 4122.58064516]
    assert result["execution"]["counts"] == {
        "accepted_orders": 1,
        "filled_orders": 1,
        "fills": 1,
        "open_positions": 1,
        "closed_positions": 1,
    }
    assert result["execution"]["reconciliation"]["status"] == "ok"
    assert result["market"] == {
        "price": 4400.0,
        "fresh": True,
        "source": "trusted-test",
    }


def test_public_read_model_fails_closed_when_facts_are_missing(tmp_path: Path) -> None:
    result = build_park_public_read_model(tmp_path / "missing", now=lambda: NOW)

    assert result["status"] == "blocked"
    assert "active_strategy_missing" in result["blockers"]
    assert "safety_evidence_not_passing" in result["blockers"]
    assert result["execution"]["orders"] == []
    assert result["viewer"]["mutations_allowed"] is False


def test_public_read_model_fails_closed_on_corrupt_snapshot(tmp_path: Path) -> None:
    output = tmp_path / "outputs"
    _fixture(output)
    snapshot = (
        output
        / "dualtrack"
        / "nautilus_authoritative"
        / "snapshots"
        / "park-session-1.json"
    )
    snapshot.write_text("not-json", encoding="utf-8")

    result = build_park_public_read_model(output, now=lambda: NOW)

    assert result["status"] == "blocked"
    assert "park_snapshot_invalid" in result["blockers"]
    assert "authoritative_snapshot_missing" in result["blockers"]
    assert result["execution"]["engine"] == "unavailable"


def test_public_read_model_fails_closed_on_malformed_grid_geometry(tmp_path: Path) -> None:
    output = tmp_path / "outputs"
    _fixture(output)
    plan_path = output / "park_strategy" / "plans.jsonl"
    latest = json.loads(plan_path.read_text(encoding="utf-8").splitlines()[-1])
    latest["risk"]["order_count"] = "not-a-count"
    _append_jsonl(plan_path, [latest])

    result = build_park_public_read_model(output, now=lambda: NOW)

    assert result["status"] == "blocked"
    assert "grid_geometry_invalid" in result["blockers"]
    assert result["strategy"]["grid_entry_range"] is None
    assert result["strategy"]["grid_rung_prices"] == []


def test_public_read_model_rejects_non_integral_grid_count(tmp_path: Path) -> None:
    output = tmp_path / "outputs"
    _fixture(output)
    plan_path = output / "park_strategy" / "plans.jsonl"
    latest = json.loads(plan_path.read_text(encoding="utf-8").splitlines()[-1])
    latest["risk"]["order_count"] = 19.5
    _append_jsonl(plan_path, [latest])

    result = build_park_public_read_model(output, now=lambda: NOW)

    assert result["status"] == "blocked"
    assert "grid_geometry_invalid" in result["blockers"]
    assert result["strategy"]["grid_rung_count"] == 0


def test_public_read_model_rejects_lifecycle_and_normalized_strategy_mismatch(tmp_path: Path) -> None:
    output = tmp_path / "outputs"
    _fixture(output)
    lifecycle_path = output / "park_strategy" / "lifecycle.jsonl"
    lifecycle = json.loads(lifecycle_path.read_text(encoding="utf-8").splitlines()[-1])
    lifecycle["strategy_type"] = "dca"
    _append_jsonl(lifecycle_path, [lifecycle])

    result = build_park_public_read_model(output, now=lambda: NOW)

    assert result["status"] == "blocked"
    assert "strategy_type_mismatch" in result["blockers"]


def test_public_read_model_uses_lifecycle_boundaries_and_blocks_mismatch(tmp_path: Path) -> None:
    output = tmp_path / "outputs"
    _fixture(output)
    lifecycle_path = output / "park_strategy" / "lifecycle.jsonl"
    lifecycle = json.loads(lifecycle_path.read_text(encoding="utf-8").splitlines()[-1])
    lifecycle["upper_price_boundary"] = 4500.0
    _append_jsonl(lifecycle_path, [lifecycle])

    result = build_park_public_read_model(output, now=lambda: NOW)

    assert result["status"] == "blocked"
    assert "strategy_boundary_mismatch" in result["blockers"]
    assert result["strategy"]["upper_price_boundary"] == 4500.0
    assert result["strategy"]["grid_entry_range"]["upper"] < 4500.0


def test_public_read_model_projects_plan_authoritative_grid_geometry(tmp_path: Path) -> None:
    output = tmp_path / "outputs"
    _fixture(output)
    plan_path = output / "park_strategy" / "plans.jsonl"
    plan = json.loads(plan_path.read_text(encoding="utf-8").splitlines()[-1])
    plan["risk"].update(
        {
            "order_count": 34,
            "grid_spacing": 10.0,
            "grid_entry_range": {"lower": 4110.0, "upper": 4440.0},
            "grid_rung_prices": [4110.0 + index * 10.0 for index in range(34)],
            "hard_stop": {"long": 4100.0, "short": 4450.0},
            "hard_stop_source": "authorized_price_boundary",
            "grid_rungs": [
                {
                    "rung": index + 1,
                    "price": 4110.0 + index * 10.0,
                    "side": "buy" if index < 15 else "sell",
                    "take_profit": 4120.0 + index * 10.0 if index < 15 else 4100.0 + index * 10.0,
                    "hard_stop": 4100.0 if index < 15 else 4450.0,
                }
                for index in range(34)
            ],
        }
    )
    _append_jsonl(plan_path, [plan])

    result = build_park_public_read_model(output, now=lambda: NOW)

    assert result["strategy"]["grid_entry_range"] == {"lower": 4110.0, "upper": 4440.0}
    assert result["strategy"]["grid_spacing"] == 10.0
    assert result["strategy"]["grid_rung_count"] == 34
    assert result["strategy"]["grid_rung_prices"] == [4110.0 + index * 10.0 for index in range(34)]
    assert result["strategy"]["stop_price"] == {"long": 4100.0, "short": 4450.0}
    assert result["strategy"]["hard_stop_source"] == "authorized_price_boundary"
    assert len(result["strategy"]["grid_rungs"]) == 34
    assert result["strategy"]["local_stop_authorized"] is False


def test_public_read_model_projects_recording_package_and_runtime_blocker(tmp_path: Path) -> None:
    output = tmp_path / "outputs"
    _fixture(output)
    recording_root = output / "park_strategy" / "recording"
    recording_root.mkdir(parents=True, exist_ok=True)
    (recording_root / "packages.jsonl").write_text(
        json.dumps(
            {
                "record_window_id": "2026-08-14_DAY",
                "strategy_session_id": "session-1",
                "strategy_revision_id": "revision-1",
                "status": "blocked_incomplete",
                "missing_categories": ["fills"],
                "strategy_open": True,
                "positions_open": 2,
                "execution_mutations": [],
                "next_action": "collect_missing_evidence",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (output / "park_strategy" / "runtime_blockers.jsonl").write_text(
        json.dumps(
            {
                "code": "recording_package_blocked",
                "strategy_session_id": "session-1",
                "strategy_revision_id": "revision-1",
                "recording_windows": [{"record_window_id": "2026-08-14_DAY"}],
                "next_action": "notify_park_and_wait",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    result = build_park_public_read_model(output, now=lambda: NOW)

    assert result["recording"] == {
        "status": "blocked",
        "record_window_id": "2026-08-14_DAY",
        "strategy_session_id": "session-1",
        "strategy_revision_id": "revision-1",
        "package_status": "blocked_incomplete",
        "missing_categories": ["fills"],
        "strategy_open": True,
        "positions_open": 2,
        "execution_mutations": [],
        "next_action": "notify_park_and_wait",
        "blocker_code": "recording_package_blocked",
    }


def test_public_read_model_projects_active_recording_window_before_package_close(tmp_path: Path) -> None:
    output = tmp_path / "outputs"
    _fixture(output)
    recording_root = output / "park_strategy" / "recording"
    recording_root.mkdir(parents=True, exist_ok=True)
    _append_jsonl(
        recording_root / "packages.jsonl",
        [
            {
                "record_window_id": "2026-08-16_NIGHT",
                "strategy_session_id": "session-old",
                "strategy_revision_id": "revision-old",
                "status": "complete",
                "execution_mutations": [],
            }
        ],
    )
    _append_jsonl(
        recording_root / "events.jsonl",
        [
            {
                "event": "manifest_started",
                "record_window_id": "2026-08-17_DAY",
                "strategy_session_id": "session-1",
                "strategy_revision_id": "revision-1",
                "starts_at": "2026-08-17T01:00:00Z",
                "ends_at": "2026-08-17T13:00:00Z",
            }
        ],
    )

    result = build_park_public_read_model(output, now=lambda: NOW)

    assert result["recording"] == {
        "status": "in_progress",
        "record_window_id": "2026-08-17_DAY",
        "strategy_session_id": "session-1",
        "strategy_revision_id": "revision-1",
        "package_status": None,
        "missing_categories": [],
        "strategy_open": True,
        "positions_open": None,
        "execution_mutations": [],
        "next_action": "continue_recording_window",
        "blocker_code": None,
    }


def test_public_read_model_clears_recovered_prior_window_blocker_for_new_window(tmp_path: Path) -> None:
    output = tmp_path / "outputs"
    _fixture(output)
    recording_root = output / "park_strategy" / "recording"
    recording_root.mkdir(parents=True, exist_ok=True)
    _append_jsonl(
        recording_root / "packages.jsonl",
        [
            {
                "record_window_id": "2026-08-16_NIGHT",
                "strategy_session_id": "session-1",
                "strategy_revision_id": "revision-1",
                "status": "complete",
                "execution_mutations": [],
            }
        ],
    )
    _append_jsonl(
        recording_root / "events.jsonl",
        [
            {
                "event": "manifest_started",
                "record_window_id": "2026-08-17_DAY",
                "strategy_session_id": "session-1",
                "strategy_revision_id": "revision-1",
                "starts_at": "2026-08-17T01:00:00Z",
                "ends_at": "2026-08-17T13:00:00Z",
            }
        ],
    )
    _append_jsonl(
        output / "park_strategy" / "runtime_blockers.jsonl",
        [
            {
                "code": "recording_facts_blocked",
                "strategy_session_id": "session-1",
                "strategy_revision_id": "revision-1",
                "recording_windows": [{"record_window_id": "2026-08-16_NIGHT"}],
            },
            {
                "code": "recording_facts_recovered",
                "record_window_id": "2026-08-16_NIGHT",
                "strategy_session_id": "session-1",
                "strategy_revision_id": "revision-1",
            },
        ],
    )

    result = build_park_public_read_model(output, now=lambda: NOW)

    assert result["recording"]["status"] == "in_progress"
    assert result["recording"]["record_window_id"] == "2026-08-17_DAY"
    assert result["recording"]["blocker_code"] is None


def test_public_read_model_clears_completed_package_blocker_for_new_window(tmp_path: Path) -> None:
    output = tmp_path / "outputs"
    _fixture(output)
    recording_root = output / "park_strategy" / "recording"
    recording_root.mkdir(parents=True, exist_ok=True)
    _append_jsonl(
        recording_root / "packages.jsonl",
        [
            {
                "record_window_id": "2026-08-16_NIGHT",
                "strategy_session_id": "session-1",
                "strategy_revision_id": "revision-1",
                "status": "complete",
                "review_status": "complete",
                "execution_mutations": [],
            }
        ],
    )
    _append_jsonl(
        recording_root / "events.jsonl",
        [
            {
                "event": "manifest_started",
                "record_window_id": "2026-08-17_DAY",
                "strategy_session_id": "session-1",
                "strategy_revision_id": "revision-1",
                "starts_at": "2026-08-17T01:00:00Z",
                "ends_at": "2026-08-17T13:00:00Z",
            }
        ],
    )
    _append_jsonl(
        output / "park_strategy" / "runtime_blockers.jsonl",
        [
            {
                "code": "recording_package_blocked",
                "strategy_session_id": "session-1",
                "strategy_revision_id": "revision-1",
                "recording_windows": [{"record_window_id": "2026-08-16_NIGHT"}],
            }
        ],
    )

    result = build_park_public_read_model(output, now=lambda: NOW)

    assert result["recording"]["status"] == "in_progress"
    assert result["recording"]["record_window_id"] == "2026-08-17_DAY"
    assert result["recording"]["blocker_code"] is None


def test_public_read_model_does_not_use_stale_complete_package_to_clear_blocker(tmp_path: Path) -> None:
    output = tmp_path / "outputs"
    _fixture(output)
    recording_root = output / "park_strategy" / "recording"
    recording_root.mkdir(parents=True, exist_ok=True)
    _append_jsonl(
        recording_root / "packages.jsonl",
        [
            {
                "record_window_id": "2026-08-16_NIGHT",
                "strategy_session_id": "session-1",
                "strategy_revision_id": "revision-1",
                "status": "complete",
                "review_status": "complete",
                "execution_mutations": [],
            },
            {
                "record_window_id": "2026-08-16_NIGHT",
                "strategy_session_id": "session-1",
                "strategy_revision_id": "revision-1",
                "status": "blocked_incomplete",
                "review_status": "blocked",
                "missing_categories": ["review"],
                "execution_mutations": [],
            },
        ],
    )
    _append_jsonl(
        recording_root / "events.jsonl",
        [
            {
                "event": "manifest_started",
                "record_window_id": "2026-08-17_DAY",
                "strategy_session_id": "session-1",
                "strategy_revision_id": "revision-1",
                "starts_at": "2026-08-17T01:00:00Z",
                "ends_at": "2026-08-17T13:00:00Z",
            }
        ],
    )
    _append_jsonl(
        output / "park_strategy" / "runtime_blockers.jsonl",
        [
            {
                "code": "recording_package_blocked",
                "strategy_session_id": "session-1",
                "strategy_revision_id": "revision-1",
                "recording_windows": [{"record_window_id": "2026-08-16_NIGHT"}],
            }
        ],
    )

    result = build_park_public_read_model(output, now=lambda: NOW)

    assert result["recording"]["status"] == "blocked"
    assert result["recording"]["blocker_code"] == "recording_package_blocked"


def test_public_read_model_blocks_package_with_execution_mutations(tmp_path: Path) -> None:
    output = tmp_path / "outputs"
    _fixture(output)
    recording_root = output / "park_strategy" / "recording"
    recording_root.mkdir(parents=True, exist_ok=True)
    _append_jsonl(
        recording_root / "packages.jsonl",
        [
            {
                "record_window_id": "2026-08-17_DAY",
                "strategy_session_id": "session-1",
                "strategy_revision_id": "revision-1",
                "status": "complete",
                "review_status": "complete",
                "execution_mutations": ["cancel"],
            }
        ],
    )

    result = build_park_public_read_model(output, now=lambda: NOW)

    assert result["recording"]["status"] == "blocked"
    assert result["recording"]["blocker_code"] == "recording_execution_mutation_detected"
    assert "recording_execution_mutation_detected" in result["blockers"]


def test_public_read_model_blocks_complete_package_until_review_is_durable(tmp_path: Path) -> None:
    output = tmp_path / "outputs"
    _fixture(output)
    recording_root = output / "park_strategy" / "recording"
    recording_root.mkdir(parents=True, exist_ok=True)
    _append_jsonl(
        recording_root / "packages.jsonl",
        [
            {
                "record_window_id": "2026-08-17_DAY",
                "strategy_session_id": "session-1",
                "strategy_revision_id": "revision-1",
                "status": "complete",
                "review_status": "pending",
                "execution_mutations": [],
            }
        ],
    )

    result = build_park_public_read_model(output, now=lambda: NOW)

    assert result["recording"]["status"] == "blocked"
    assert result["recording"]["blocker_code"] == "recording_review_pending"
    assert "recording_review_pending" in result["blockers"]


def test_public_read_model_does_not_project_an_older_session_package(tmp_path: Path) -> None:
    output = tmp_path / "outputs"
    _fixture(output)
    recording_root = output / "park_strategy" / "recording"
    recording_root.mkdir(parents=True, exist_ok=True)
    (recording_root / "packages.jsonl").write_text(
        json.dumps(
            {
                "record_window_id": "2026-08-14_DAY",
                "strategy_session_id": "session-old",
                "strategy_revision_id": "revision-old",
                "strategy_session_ids": ["session-old"],
                "strategy_revision_ids": ["revision-old"],
                "status": "complete",
                "execution_mutations": [],
            }
        )
        + "\n",
        encoding="utf-8",
    )

    result = build_park_public_read_model(output, now=lambda: NOW)

    assert result["recording"]["status"] == "blocked"
    assert result["recording"]["record_window_id"] is None
    assert result["recording"]["blocker_code"] == "recording_identity_missing"


def test_public_read_model_clears_recovered_same_window_blocker(tmp_path: Path) -> None:
    output = tmp_path / "outputs"
    _fixture(output)
    recording_root = output / "park_strategy" / "recording"
    recording_root.mkdir(parents=True, exist_ok=True)
    (recording_root / "packages.jsonl").write_text(
        json.dumps(
            {
                "record_window_id": "2026-08-14_DAY",
                "strategy_session_id": "session-1",
                "strategy_revision_id": "revision-1",
                "status": "complete",
                "strategy_open": True,
                "positions_open": 1,
                "execution_mutations": [],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (output / "park_strategy" / "runtime_blockers.jsonl").write_text(
        json.dumps(
            {
                "code": "recording_package_blocked",
                "strategy_session_id": "session-1",
                "strategy_revision_id": "revision-1",
                "recording_windows": [{"record_window_id": "2026-08-14_DAY"}],
            }
        )
        + "\n",
        encoding="utf-8",
    )

    result = build_park_public_read_model(output, now=lambda: NOW)

    assert result["recording"]["status"] == "complete"
    assert result["recording"]["blocker_code"] is None
