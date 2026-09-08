from datetime import datetime, timedelta, timezone

from pipelines.soak_report import EXPECTED_TICKS, build_soak_report


START = datetime(2026, 9, 1, 0, tzinfo=timezone.utc)


def _tick(index: int, *, execution_status: str = "active", receipt: bool = True) -> dict:
    row = {
        "tick_id": f"tick-{index}",
        "occurred_at": (START + timedelta(minutes=index)).isoformat(),
        "execution": {"status": execution_status},
        "heartbeat": {"status": "fresh"},
    }
    if receipt:
        row["receipt"] = {"status": "persisted"}
    return row


def _complete(*, missing_tick: int | None = None, unknown: int | None = None, missing_boundary: bool = False) -> list[dict]:
    rows = [_tick(index, execution_status="unknown" if index == unknown else "active") for index in range(EXPECTED_TICKS) if index != missing_tick]
    for index, hour in ((780, 21), (1440, 8), (2220, 21)):
        if not missing_boundary or hour != 21:
            rows.append({"occurred_at": (START + timedelta(minutes=index)).isoformat(), "event": "rollover_receipt", "receipt": {"status": "pass"}})
    return rows


def test_historical_48h_replay_is_deterministic_and_passes() -> None:
    rows = _complete()
    first = build_soak_report(rows)
    second = build_soak_report(reversed(rows))
    assert first == second
    assert first["expected_ticks"] == 2880
    assert first["healthy_ticks"] == 2880
    assert first["status"] == "pass"


def test_missing_tick_and_boundary_are_reported() -> None:
    report = build_soak_report(_complete(missing_tick=12, missing_boundary=True))
    assert report["missing_ticks"] == 1
    assert report["healthy_ticks"] == 2879
    assert report["rollover_boundaries"][0]["status"] == "missing"
    assert report["status"] == "blocked"


def test_unknown_control_result_blocks_report() -> None:
    report = build_soak_report(_complete(unknown=7))
    assert report["unknown_control_results"] == 1
    assert report["acceptance"]["zero_unknown_control_results"] is False
    assert report["status"] == "blocked"
