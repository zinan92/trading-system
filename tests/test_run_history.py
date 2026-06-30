from pathlib import Path

from services.run_history import RunHistory


def test_append_and_recent_newest_first(tmp_path: Path):
    rh = RunHistory(tmp_path / "outputs")
    rh.append({"run_date": "2026-05-29", "state": "ok", "health_status": "ok"})
    rh.append({"run_date": "2026-05-30", "state": "ok", "health_status": "warn"})
    rh.append({"run_date": "2026-05-31", "state": "error", "health_status": "error"})

    recent = rh.recent(limit=2)
    assert [r["run_date"] for r in recent] == ["2026-05-31", "2026-05-30"]  # newest first
    assert recent[0]["recorded_at"]  # stamped on append


def test_query_by_state_and_date_range(tmp_path: Path):
    rh = RunHistory(tmp_path / "outputs")
    rh.append({"run_date": "2026-05-28", "state": "ok"})
    rh.append({"run_date": "2026-05-29", "state": "error"})
    rh.append({"run_date": "2026-05-30", "state": "ok"})

    assert [r["run_date"] for r in rh.query(state="error")] == ["2026-05-29"]
    in_range = rh.query(since="2026-05-29", until="2026-05-30")
    assert {r["run_date"] for r in in_range} == {"2026-05-29", "2026-05-30"}


def test_recent_on_empty_history(tmp_path: Path):
    rh = RunHistory(tmp_path / "outputs")
    assert rh.recent() == []


def test_corrupt_line_is_skipped(tmp_path: Path):
    rh = RunHistory(tmp_path / "outputs")
    rh.append({"run_date": "2026-05-30", "state": "ok"})
    # inject a corrupt line
    rh.path.parent.mkdir(parents=True, exist_ok=True)
    with rh.path.open("a", encoding="utf-8") as handle:
        handle.write("{not json}\n")
    rh.append({"run_date": "2026-05-31", "state": "ok"})

    recent = rh.recent()
    assert [r["run_date"] for r in recent] == ["2026-05-31", "2026-05-30"]  # corrupt line skipped
