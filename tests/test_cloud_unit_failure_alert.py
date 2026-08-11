from __future__ import annotations

import json
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

import pytest

from services.cloud_unit_failure_alert import CloudUnitFailureAlert


class Response:
    def __init__(self, status: int = 200) -> None:
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def fixed_now() -> datetime:
    return datetime(2026, 8, 11, 14, 30, tzinfo=timezone.utc)


def read_events(root: Path) -> list[dict]:
    path = root / "cloud" / "unit_failure_events" / "2026-08-11.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_unit_failure_is_persisted_before_external_fail_signal(tmp_path: Path) -> None:
    observed: dict = {}

    def opener(request, *, timeout):
        observed["events_at_send"] = read_events(tmp_path)
        observed["url"] = request.full_url
        observed["timeout"] = timeout
        return Response(200)

    result = CloudUnitFailureAlert(
        tmp_path,
        url="https://hc.example/check-id?existing=1",
        opener=opener,
        timeout_seconds=4.0,
        now=fixed_now,
    ).run("gridmind-backup.service")

    assert len(observed["events_at_send"]) == 1
    assert observed["events_at_send"][0]["phase"] == "detected"
    assert observed["events_at_send"][0]["control_actions_executed"] == 0
    parsed = urllib.parse.urlparse(observed["url"])
    assert parsed.path.endswith("/fail")
    query = dict(urllib.parse.parse_qsl(parsed.query))
    assert query["machine_code"] == "cloud_unit_failed"
    assert query["failed_unit"] == "gridmind-backup.service"
    assert query["existing"] == "1"
    assert observed["timeout"] == 4.0
    assert result["delivery"] == {
        "status": "fail_sent",
        "delivered": True,
        "status_code": 200,
        "target_kind": "fail",
    }
    events = read_events(tmp_path)
    assert [event["phase"] for event in events] == ["detected", "delivery_result"]
    assert events[0]["event_id"] == events[1]["event_id"]
    assert all(event["paper_only"] is True for event in events)
    assert all(event["orders_created"] == 0 for event in events)
    assert all(event["positions_changed"] == 0 for event in events)
    assert "check-id" not in json.dumps(events)


def test_unit_failure_records_unconfigured_delivery_without_network(tmp_path: Path) -> None:
    result = CloudUnitFailureAlert(
        tmp_path,
        url="",
        opener=lambda *_args, **_kwargs: pytest.fail("network must not be called"),
        now=fixed_now,
    ).run("gridmind-live-tick.service")

    assert result["delivery"] == {
        "status": "not_configured",
        "delivered": False,
        "target_kind": "fail",
    }
    assert len(read_events(tmp_path)) == 2


@pytest.mark.parametrize(
    "failed_unit",
    ["", "ssh.service", "gridmind-backup.timer", "../../gridmind-backup.service"],
)
def test_unit_failure_rejects_non_gridmind_service_names(
    tmp_path: Path, failed_unit: str
) -> None:
    with pytest.raises(ValueError, match="invalid_gridmind_failed_unit"):
        CloudUnitFailureAlert(tmp_path, url="", now=fixed_now).run(failed_unit)
    assert not (tmp_path / "cloud").exists()
