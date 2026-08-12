from __future__ import annotations

import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

from services.cloud_deadman_watchdog import CloudDeadmanWatchdog
from services.journal_store import load_json, write_json


NOW = datetime(2026, 8, 12, 1, 30, tzinfo=timezone.utc)


class _Response:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def _timer(command, **_kwargs):
    assert command[2] == "gridmind-deadman-ping.timer"
    return subprocess.CompletedProcess(
        command,
        0,
        "LoadState=loaded\nUnitFileState=enabled\nActiveState=active\n",
        "",
    )


def _receipt(root: Path, at: datetime) -> None:
    write_json(
        root / "deadman_ping" / "current.json",
        [{
            "checked_at": at.isoformat(),
            "status": "sent",
            "ping": {"delivered": True},
        }],
    )


def test_healthy_watchdog_never_masks_primary_deadman_with_success_ping(
    tmp_path: Path,
) -> None:
    _receipt(tmp_path, NOW - timedelta(minutes=1))

    def no_network(*_args, **_kwargs):
        raise AssertionError("healthy watchdog must not send any ping")

    result = CloudDeadmanWatchdog(
        tmp_path,
        url="https://hc-ping.com/redacted",
        opener=no_network,
        command_runner=_timer,
        now=lambda: NOW,
    ).run()

    assert result["status"] == "healthy"
    assert result["success_ping_sent"] is False
    assert load_json(
        tmp_path / "cloud" / "deadman_watchdog" / "current.json"
    )[0]["machine_code"] == "deadman_liveness_fresh"


def test_stale_receipt_sends_one_fail_then_deduplicates_same_outage(
    tmp_path: Path,
) -> None:
    _receipt(tmp_path, NOW - timedelta(minutes=20))
    requests = []

    def opener(request, **_kwargs):
        requests.append(request.full_url)
        return _Response()

    watcher = CloudDeadmanWatchdog(
        tmp_path,
        url="https://hc-ping.com/redacted?existing=1",
        opener=opener,
        command_runner=_timer,
        now=lambda: NOW,
    )
    first = watcher.run()
    second = watcher.run()

    assert first["status"] == "blocked"
    assert first["machine_code"] == "deadman_success_receipt_stale"
    assert first["delivery"]["status"] == "fail_sent"
    assert second["delivery"]["status"] == "duplicate_suppressed"
    assert len(requests) == 1
    assert "/fail?" in requests[0]
    assert "redacted" in requests[0]
    assert first["success_ping_sent"] is False
    events = [
        json.loads(line)
        for line in (
            tmp_path
            / "cloud"
            / "deadman_watchdog"
            / "events"
            / "2026-08-12.jsonl"
        ).read_text(encoding="utf-8").splitlines()
    ]
    assert [row["event"] for row in events] == [
        "detected",
        "delivery_result",
    ]
    assert all(row["secrets_included"] is False for row in events)
    assert "redacted" not in json.dumps(events)


def test_recovery_is_recorded_without_sending_success(tmp_path: Path) -> None:
    _receipt(tmp_path, NOW - timedelta(minutes=20))
    clock = [NOW]
    watcher = CloudDeadmanWatchdog(
        tmp_path,
        url="https://hc-ping.com/redacted",
        opener=lambda *_args, **_kwargs: _Response(),
        command_runner=_timer,
        now=lambda: clock[0],
    )
    blocked = watcher.run()
    clock[0] = NOW + timedelta(minutes=1)
    _receipt(tmp_path, clock[0])
    recovered = watcher.run()

    assert blocked["status"] == "blocked"
    assert recovered["status"] == "healthy"
    events_path = (
        tmp_path
        / "cloud"
        / "deadman_watchdog"
        / "events"
        / "2026-08-12.jsonl"
    )
    events = [json.loads(line) for line in events_path.read_text().splitlines()]
    assert events[-1]["event"] == "recovered"
    assert events[-1]["outage_id"] == blocked["outage_id"]


def test_inactive_timer_fails_closed(tmp_path: Path) -> None:
    _receipt(tmp_path, NOW)

    def inactive(command, **_kwargs):
        return subprocess.CompletedProcess(
            command,
            0,
            "LoadState=loaded\nUnitFileState=enabled\nActiveState=inactive\n",
            "",
        )

    result = CloudDeadmanWatchdog(
        tmp_path,
        url="https://hc-ping.com/redacted",
        opener=lambda *_args, **_kwargs: _Response(),
        command_runner=inactive,
        now=lambda: NOW,
    ).run()

    assert result["status"] == "blocked"
    assert result["machine_code"] == "deadman_timer_unavailable"
