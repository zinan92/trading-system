import sys

import pytest

from pipelines import dashboard_server, dualtrack_cycle_runner


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

    assert exc_info.value.code == 78


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

    assert result == 78
