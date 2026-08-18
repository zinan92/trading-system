from __future__ import annotations

import json


def test_park_control_checks_scheduler_ownership_before_telegram(monkeypatch, tmp_path, capsys) -> None:
    import pipelines.park_control as module

    class Guard:
        def __init__(self, _root):
            pass

        def verify(self):
            return {"ok": False, "blocker": "scheduler_owner_id_mismatch"}

    monkeypatch.setattr(module, "SchedulerOwnershipGuard", Guard)

    assert module.main(["--output-root", str(tmp_path)]) == 79
    payload = json.loads(capsys.readouterr().out)
    assert payload["code"] == "scheduler_owner_id_mismatch"
    assert payload["paper_only"] is True
