from __future__ import annotations

from pathlib import Path

from services.park_recording_track import ParkRecordingTrack, REQUIRED_CATEGORIES


def _recording(tmp_path: Path) -> ParkRecordingTrack:
    track = ParkRecordingTrack(tmp_path / "outputs")
    track.start_window(
        record_window_id="2026-08-14_DAY",
        strategy_session_id="session-1",
        strategy_revision_id="revision-1",
        starts_at="2026-08-14T01:00:00Z",
        ends_at="2026-08-14T13:00:00Z",
    )
    return track


def _all_facts(track: ParkRecordingTrack, window: str = "2026-08-14_DAY") -> None:
    for category in REQUIRED_CATEGORIES:
        track.record_event(
            record_window_id=window,
            strategy_session_id="session-1",
            strategy_revision_id="revision-1",
            category=category,
            event_type=f"{category}_observed",
            source="test",
            occurred_at="2026-08-14T10:00:00Z",
            payload={"category": category},
        )


def test_same_session_can_start_two_record_windows(tmp_path: Path) -> None:
    track = _recording(tmp_path)
    second = track.start_window(
        record_window_id="2026-08-14_NIGHT",
        strategy_session_id="session-1",
        strategy_revision_id="revision-1",
        starts_at="2026-08-14T13:00:00Z",
        ends_at="2026-08-15T01:00:00Z",
    )
    assert second["strategy_session_id"] == "session-1"
    assert second["strategy_revision_id"] == "revision-1"


def test_complete_package_allows_open_strategy_and_positions(tmp_path: Path) -> None:
    track = _recording(tmp_path)
    _all_facts(track)
    package = track.close_package(
        record_window_id="2026-08-14_DAY",
        strategy_session_id="session-1",
        strategy_revision_id="revision-1",
        strategy_open=True,
        positions_open=2,
    )
    assert package["status"] == "complete"
    assert package["strategy_open"] is True
    assert package["positions_open"] == 2
    assert package["execution_mutations"] == []


def test_missing_evidence_is_blocked_not_fabricated(tmp_path: Path) -> None:
    track = _recording(tmp_path)
    track.record_event(
        record_window_id="2026-08-14_DAY", strategy_session_id="session-1", strategy_revision_id="revision-1",
        category="control", event_type="proposal", source="test", occurred_at="2026-08-14T10:00:00Z",
    )
    package = track.close_package(record_window_id="2026-08-14_DAY", strategy_session_id="session-1", strategy_revision_id="revision-1", strategy_open=False, positions_open=0)
    assert package["status"] == "blocked_incomplete"
    assert "fills" in package["missing_categories"]


def test_late_event_appends_amendment_and_review_is_evidence_based(tmp_path: Path) -> None:
    track = _recording(tmp_path)
    _all_facts(track)
    package = track.close_package(record_window_id="2026-08-14_DAY", strategy_session_id="session-1", strategy_revision_id="revision-1", strategy_open=True, positions_open=1)
    amendment = track.amend_late_event(
        record_window_id="2026-08-14_DAY", strategy_session_id="session-1", strategy_revision_id="revision-1",
        category="fills", event_type="late_fill", source="test", occurred_at="2026-08-14T10:30:00Z", payload={"fill_id": "late-1"},
    )
    assert amendment["revision"] == package["revision"] + 1
    assert len(track.events()) == len(REQUIRED_CATEGORIES) + 2
    review = track.review(record_window_id="2026-08-14_DAY")
    assert review["strategy_session_id"] == "session-1"
    assert review["pnl_claim"] is None
    assert review["did_well"]
    assert review["next_window"]
