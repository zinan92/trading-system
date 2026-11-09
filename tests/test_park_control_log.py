"""The control loop leaves one structured record per run (#1475)."""
from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

import pipelines.park_control as module
from services.structured_log import ROOT


@pytest.fixture(autouse=True)
def _detach_log_handlers():
    yield
    root = logging.getLogger(ROOT)
    for handler in [h for h in root.handlers if not isinstance(h, logging.NullHandler)]:
        root.removeHandler(handler)
        handler.close()


def _records(root: Path) -> list[dict]:
    return [json.loads(line) for line in (root / "logs" / "park-control.jsonl").read_text().splitlines()]


class _Owned:
    def __init__(self, _root):
        pass

    def verify(self):
        return {"ok": True}


def _config(tmp_path: Path) -> Path:
    path = tmp_path / "park.json"
    path.write_text("{}")
    return path


def test_a_blocked_run_is_one_tick_record(monkeypatch, tmp_path, capsys) -> None:
    class Guard(_Owned):
        def verify(self):
            return {"ok": False, "blocker": "scheduler_owner_id_mismatch"}

    monkeypatch.setattr(module, "SchedulerOwnershipGuard", Guard)

    assert module.main(["--output-root", str(tmp_path)]) == 79
    assert json.loads(capsys.readouterr().out)["code"] == "scheduler_owner_id_mismatch"  # stdout unchanged
    [tick] = _records(tmp_path)
    assert tick["service"] == "park-control" and tick["event"] == "tick" and tick["level"] == "WARNING"
    assert tick["status"] == "blocked" and tick["code"] == "scheduler_owner_id_mismatch"
    assert set(tick["steps"]) == {"ownership"} and isinstance(tick["ms"], int)


def test_a_typed_failure_names_its_step_and_keeps_the_traceback(monkeypatch, tmp_path, capsys) -> None:
    monkeypatch.setattr(module, "SchedulerOwnershipGuard", _Owned)
    monkeypatch.setattr(module, "load_effective_park_config", lambda _root, config: config)
    monkeypatch.setattr(module, "prepare_park_paper_config", lambda config, **_kw: config)
    monkeypatch.setattr(module, "active_instrument_key", lambda _root: "gold")
    monkeypatch.setattr(module, "TelegramBotTransport", lambda **_kw: object())
    monkeypatch.setattr(module, "ParkAiProviderGateway", lambda: object())
    monkeypatch.setattr(module.HyperliquidTestnetRuntimeConfig, "from_environment", classmethod(lambda cls: None))

    class Worker:
        def __init__(self, *_a, **_kw):
            pass

        def run_once(self, _transport):
            raise ValueError("telegram token=abc123 rejected")

    monkeypatch.setattr(module, "ParkTelegramWorker", Worker)

    argv = ["--output-root", str(tmp_path), "--park-config", str(_config(tmp_path)), "--park-user-id", "1", "--chat-id", "2"]
    assert module.main(argv) == 2
    assert json.loads(capsys.readouterr().out) == {"code": "park_control_blocked", "paper_only": True, "schema_version": "park-control-v1", "status": "blocked"}
    failed, tick = _records(tmp_path)
    assert failed["event"] == "tick_failed" and failed["level"] == "ERROR" and failed["step"] == "telegram"
    assert "ValueError" in failed["exception"] and "abc123" not in json.dumps(failed)
    assert tick["event"] == "tick" and tick["status"] == "blocked" and "telegram" in tick["steps"]


def test_an_unexpected_crash_is_logged_and_still_raised(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(module, "SchedulerOwnershipGuard", _Owned)
    monkeypatch.setattr(module, "load_effective_park_config", lambda _root, _config: (_ for _ in ()).throw(RuntimeError("boom")))

    with pytest.raises(RuntimeError, match="boom"):
        module.main(["--output-root", str(tmp_path), "--park-config", str(_config(tmp_path))])
    [crashed] = _records(tmp_path)
    assert crashed["event"] == "tick_crashed" and crashed["level"] == "ERROR" and crashed["step"] == "config"
    assert "RuntimeError: boom" in crashed["exception"]
