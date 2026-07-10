from __future__ import annotations

import json
from pathlib import Path

import pytest

import pipelines.dualtrack_nautilus_shadow_replay as replay_pipeline
from spikes.dualtrack_nautilus_shadow_replay import _latest, run_replay


def test_shadow_replay_loader_requires_nonempty_artifact_array(tmp_path: Path) -> None:
    path = tmp_path / "input.json"
    path.write_text("[]", encoding="utf-8")
    with pytest.raises(ValueError, match="non-empty shadow input"):
        _latest(path, "shadow input")


def test_shadow_replay_loader_uses_latest_artifact(tmp_path: Path) -> None:
    path = tmp_path / "input.json"
    path.write_text(json.dumps([{"input_id": "old"}, {"input_id": "new"}]), encoding="utf-8")
    assert _latest(path, "shadow input") == {"input_id": "new"}


def test_shadow_replay_pipeline_fails_closed_without_prepared_artifacts(tmp_path: Path) -> None:
    result = replay_pipeline.main([
        "--cycle-id", "2026-07-10_DAY",
        "--nautilus-python", "/missing/nautilus-python",
        "--output-root", str(tmp_path / "outputs"),
    ])

    assert result == 2


def test_shadow_replay_refuses_command_without_immutable_payload(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    preflight = tmp_path / "preflight.json"
    input_path = tmp_path / "input.json"
    preflight.write_text(json.dumps([{
        "status": "ready_for_paper_shadow",
        "instrument": {"symbol": "XAUUSDT"},
        "fee_model": {"maker_fee_rate": "0.00005", "taker_fee_rate": "0.00005", "real_money_eligible": False},
    }]), encoding="utf-8")
    input_path.write_text(json.dumps([{
        "schema_version": "dualtrack-shadow-input-v1",
        "cycle_id": "2026-07-10_DAY",
        "commands": [{"command_id": "cmd-1"}],
        "market_events": [{"instrument_id": "XAUUSDT"}],
    }]), encoding="utf-8")

    monkeypatch.setattr("spikes.dualtrack_nautilus_shadow_replay.build_nautilus_instrument", lambda *_args, **_kwargs: object())
    with pytest.raises(ValueError, match="missing command payload"):
        run_replay(preflight, input_path)
