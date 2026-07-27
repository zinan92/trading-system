import sys

import pytest

from pipelines import dashboard_server, dualtrack_cycle_runner
from services.paper_release_receipt import PAPER_SERVICE_BOOT_BLOCKED_EXIT_CODE


class _BlockedBootGate:
    def __init__(self, *args, **kwargs):
        pass

    def verify(self, service):
        return {"ok": False, "blocker": "current_tracked_source_tree_dirty"}


def test_dashboard_refuses_boot_before_binding(monkeypatch):
    monkeypatch.setattr(dashboard_server, "PaperServiceBootGate", _BlockedBootGate)
    monkeypatch.setattr(
        dashboard_server,
        "ThreadingHTTPServer",
        lambda *args, **kwargs: pytest.fail("dashboard bound before boot verification"),
    )
    monkeypatch.setattr(sys, "argv", ["dashboard_server"])

    with pytest.raises(SystemExit) as exc_info:
        dashboard_server.main()

    assert exc_info.value.code == PAPER_SERVICE_BOOT_BLOCKED_EXIT_CODE


def test_live_tick_refuses_boot_before_runner_construction(monkeypatch, tmp_path):
    monkeypatch.setattr(dualtrack_cycle_runner, "PaperServiceBootGate", _BlockedBootGate)
    monkeypatch.setattr(
        dualtrack_cycle_runner,
        "DualTrackCycleRunner",
        lambda *args, **kwargs: pytest.fail("runner constructed before boot verification"),
    )

    result = dualtrack_cycle_runner.main(
        ["--event", "live-tick", "--output-root", str(tmp_path)]
    )

    assert result == PAPER_SERVICE_BOOT_BLOCKED_EXIT_CODE
    assert result == 79


def test_cloud_dashboard_uses_cloud_gate_before_binding(monkeypatch):
    monkeypatch.setenv("GRIDMIND_RUNTIME_MODE", "cloud")
    monkeypatch.setattr(dashboard_server, "CloudPaperServiceBootGate", _BlockedBootGate)
    monkeypatch.setattr(
        dashboard_server,
        "PaperServiceBootGate",
        lambda *args, **kwargs: pytest.fail("local launchd gate used in cloud mode"),
    )
    monkeypatch.setattr(
        dashboard_server,
        "ThreadingHTTPServer",
        lambda *args, **kwargs: pytest.fail("dashboard bound before cloud verification"),
    )
    monkeypatch.setattr(sys, "argv", ["dashboard_server"])

    with pytest.raises(SystemExit) as exc_info:
        dashboard_server.main()

    assert exc_info.value.code == PAPER_SERVICE_BOOT_BLOCKED_EXIT_CODE


def test_cloud_live_tick_uses_cloud_gate_before_runner(monkeypatch, tmp_path):
    monkeypatch.setenv("GRIDMIND_RUNTIME_MODE", "cloud")
    monkeypatch.setattr(
        dualtrack_cycle_runner,
        "CloudPaperServiceBootGate",
        _BlockedBootGate,
    )
    monkeypatch.setattr(
        dualtrack_cycle_runner,
        "PaperServiceBootGate",
        lambda *args, **kwargs: pytest.fail("local launchd gate used in cloud mode"),
    )
    monkeypatch.setattr(
        dualtrack_cycle_runner,
        "DualTrackCycleRunner",
        lambda *args, **kwargs: pytest.fail("runner constructed before cloud verification"),
    )

    result = dualtrack_cycle_runner.main(
        ["--event", "live-tick", "--output-root", str(tmp_path)]
    )

    assert result == PAPER_SERVICE_BOOT_BLOCKED_EXIT_CODE


def test_local_live_tick_refuses_cloud_owned_namespace_before_boot_gate(
    monkeypatch,
    tmp_path,
):
    from services.scheduler_ownership import SchedulerOwnershipStore

    store = SchedulerOwnershipStore(tmp_path)
    local = store.initialize_local()
    paused = store.pause(
        expected_owner_id="local-mac",
        expected_epoch=local["epoch"],
    )
    store.activate(
        new_owner_id="cloud-primary",
        expected_epoch=paused["epoch"],
    )
    monkeypatch.delenv("GRIDMIND_RUNTIME_MODE", raising=False)
    monkeypatch.delenv("GRIDMIND_SCHEDULER_OWNER_ID", raising=False)
    monkeypatch.setattr(
        dualtrack_cycle_runner,
        "PaperServiceBootGate",
        lambda *args, **kwargs: pytest.fail("boot gate ran despite owner mismatch"),
    )
    monkeypatch.setattr(
        dualtrack_cycle_runner,
        "DualTrackCycleRunner",
        lambda *args, **kwargs: pytest.fail("runner constructed despite owner mismatch"),
    )

    result = dualtrack_cycle_runner.main(
        ["--event", "live-tick", "--output-root", str(tmp_path)]
    )

    assert result == PAPER_SERVICE_BOOT_BLOCKED_EXIT_CODE
