from __future__ import annotations

import json
from pathlib import Path

import pytest

from services.journal_store import load_json
from services.lab_registry import LabRegistry


def _spec() -> dict:
    return {
        "hypothesis": "MACD baseline has positive expectancy after costs",
        "family": "macd_baseline",
        "strategy_ref": {"strategy_id": "gold_1m_macd"},
        "data_range": {"start": "2026-01-01", "end": "2026-02-01"},
    }


def test_registry_writes_pending_before_results(tmp_path: Path):
    registry = LabRegistry(tmp_path / "outputs")

    entry = registry.start(_spec(), exp_id="e1")

    assert entry["status"] == "pending"
    assert load_json(tmp_path / "outputs" / "lab" / "experiments" / "e1.json")[0]["status"] == "pending"
    assert registry.trial_count("macd_baseline") == 1


def test_registry_finalizes_and_records_holdout_consumption(tmp_path: Path):
    registry = LabRegistry(tmp_path / "outputs")
    registry.start(_spec(), exp_id="e1")
    registry.finalize("e1", status="valid", results={"objective": {"passed": False}}, notes=["thin sample"])
    registry.record_holdout_consumption("e1", {"start": "2026-01-15", "end": "2026-02-01", "bars": 100})

    entry = registry.load("e1")
    index = load_json(tmp_path / "outputs" / "lab" / "registry.json")[0]
    assert entry["status"] == "valid"
    assert entry["objective"] == {"passed": False}
    assert entry["holdout_consumed"] is True
    assert index["experiments"][0]["holdout_consumed"] is True


def test_registry_preserves_report_key_outputs(tmp_path: Path):
    registry = LabRegistry(tmp_path / "outputs")
    registry.start(_spec(), exp_id="e1")

    registry.finalize("e1", status="valid", results={"break_even_bp": 0.25, "data_coverage": {"acceptance_blocker": True}})

    entry = registry.load("e1")
    assert entry["break_even_bp"] == 0.25
    assert entry["data_coverage"]["acceptance_blocker"] is True


def test_registry_atomic_write_failure_does_not_corrupt_existing_index(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    registry = LabRegistry(tmp_path / "outputs")
    registry.start(_spec(), exp_id="e1")
    before = (tmp_path / "outputs" / "lab" / "registry.json").read_text(encoding="utf-8")

    import services.lab_registry as lab_registry

    original_write = lab_registry.write_json

    def broken_write(path, rows):
        if path.name == "registry.json":
            raise RuntimeError("interrupted")
        original_write(path, rows)

    monkeypatch.setattr(lab_registry, "write_json", broken_write)
    with pytest.raises(RuntimeError):
        registry.finalize("e1", status="valid", results={"objective": {"passed": True}})

    after = (tmp_path / "outputs" / "lab" / "registry.json").read_text(encoding="utf-8")
    assert after == before
    assert json.loads(after)[0]["experiments"][0]["status"] == "pending"
