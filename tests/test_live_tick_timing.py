import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from services.live_tick_timing import (
    REQUIRED_PHASES,
    LiveTickTimingSession,
    read_timing_receipts,
    validate_homogeneous_sample,
    validate_success_receipt,
)


class Clock:
    def __init__(self) -> None:
        self.value = 0

    def __call__(self) -> int:
        self.value += 1_000_000
        return self.value


def _session(tmp_path: Path, **overrides) -> LiveTickTimingSession:
    defaults = {
        "output_root": tmp_path,
        "cycle_id": "2026-07-30_DAY",
        "observed_at": datetime(2026, 7, 30, 4, tzinfo=timezone.utc),
        "monotonic_ns": Clock(),
        "wall_now": lambda: datetime(2026, 7, 30, 4, tzinfo=timezone.utc),
        "source_attestation": lambda: {
            "source_sha": "a" * 40,
            "source_tree_sha": "b" * 40,
            "tracked_tree_clean": True,
        },
        "hostname": lambda: "cloud-paper-1",
        "ownership": lambda: {
            "status": "active",
            "active_owner_id": "cloud-paper-primary",
            "epoch": 3,
        },
        "runtime_mode": "cloud",
    }
    defaults.update(overrides)
    return LiveTickTimingSession(**defaults)


def _complete(session: LiveTickTimingSession) -> dict:
    for phase in REQUIRED_PHASES:
        assert session.measure(phase, lambda: "ok") == "ok"
    return session.finish(status="success")


def test_timing_receipt_is_append_only_source_bound_and_closes(tmp_path: Path) -> None:
    first = _complete(_session(tmp_path))
    second = _complete(_session(tmp_path))

    rows = read_timing_receipts(tmp_path)
    assert rows == [first, second]
    assert len(
        (
            tmp_path
            / "dualtrack"
            / "observability"
            / "live_tick_timing"
            / "2026-07-30.jsonl"
        ).read_text(encoding="utf-8").splitlines()
    ) == 2
    assert validate_homogeneous_sample(rows) == rows
    assert first["control_actions_executed"] == 0
    assert first["control_action_scope"] == "timing_instrumentation_only"
    assert first["total_duration_ms"] == (
        first["phase_sum_ms"] + first["unattributed_duration_ms"]
    )


def test_phase_exception_is_preserved_and_partial_row_is_not_accepted(
    tmp_path: Path,
) -> None:
    session = _session(tmp_path)

    with pytest.raises(RuntimeError, match="phase failed"):
        session.measure(
            "lifecycle",
            lambda: (_ for _ in ()).throw(RuntimeError("phase failed")),
        )
    failed = session.finish(status="failed", error_type="RuntimeError")

    assert failed["phases"] == [
        {
            "name": "lifecycle",
            "status": "failed",
            "duration_ms": 1.0,
            "error_type": "RuntimeError",
        }
    ]
    with pytest.raises(ValueError, match="not_successful"):
        validate_success_receipt(failed)


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (lambda row: row.update(runtime_mode="local"), "not_cloud"),
        (
            lambda row: row["source"].update(source_sha="bad"),
            "source_sha_invalid",
        ),
        (
            lambda row: row.update(control_actions_executed=1),
            "control_action_detected",
        ),
        (lambda row: row["phases"].pop(), "phases_incomplete"),
        (
            lambda row: row.update(unattributed_duration_ms=999),
            "total_does_not_close",
        ),
    ],
)
def test_validator_fails_closed_on_unusable_rows(
    tmp_path: Path,
    mutate,
    expected: str,
) -> None:
    row = _complete(_session(tmp_path))
    mutate(row)

    with pytest.raises(ValueError, match=expected):
        validate_success_receipt(row)


def test_homogeneous_sample_rejects_mixed_sha_host_or_owner(tmp_path: Path) -> None:
    first = _complete(_session(tmp_path))
    second = json.loads(json.dumps(first))
    second["source"]["source_sha"] = "c" * 40

    with pytest.raises(ValueError, match="mixed_identity"):
        validate_homogeneous_sample([first, second])


def test_metadata_failure_never_changes_measured_operation(tmp_path: Path) -> None:
    session = _session(
        tmp_path,
        source_attestation=lambda: (_ for _ in ()).throw(
            RuntimeError("git unavailable")
        ),
    )

    row = _complete(session)

    assert row["status"] == "success"
    assert row["metadata_status"] == "invalid"
    assert row["metadata_errors"] == ["RuntimeError"]
    with pytest.raises(ValueError, match="metadata_invalid"):
        validate_success_receipt(row)


def test_receipt_write_failure_never_changes_measured_operation(
    tmp_path: Path,
) -> None:
    blocked_root = tmp_path / "not-a-directory"
    blocked_root.write_text("blocked", encoding="utf-8")
    session = _session(blocked_root)

    row = _complete(session)

    assert row["status"] == "success"
    assert not (
        blocked_root
        / "dualtrack"
        / "observability"
        / "live_tick_timing"
    ).exists()
