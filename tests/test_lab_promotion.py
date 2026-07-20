from __future__ import annotations

from pathlib import Path

from services.journal_store import load_json
from services.lab_registry import LabRegistry
from services.lab_promotion import lab_expectation_for_strategy, promotion_gate, record_paper_eligibility


def _entry(**overrides) -> dict:
    entry = {
        "exp_id": "e1",
        "status": "valid",
        "family": "macd",
        "strategy_ref": {"strategy_id": "gold_1m_macd"},
        "holdout_consumed": True,
        "objective": {
            "walkforward": {"passed": True, "metrics": {"sortino": 1.2}},
            "holdout": {"passed": True, "metrics": {"sortino": 0.8}},
        },
    }
    entry.update(overrides)
    return entry


def test_promotion_gate_requires_walkforward_and_holdout():
    missing_execution = promotion_gate(_entry())
    assert missing_execution["paper_eligible"] is False
    assert "nautilus_execution_conformance_missing" in missing_execution["blockers"]

    blocked = promotion_gate(_entry(holdout_consumed=False))
    assert blocked["paper_eligible"] is False
    assert "holdout_not_consumed" in blocked["blockers"]


def test_lab_expectation_reads_latest_strategy_entry(tmp_path: Path):
    root = tmp_path / "outputs"
    registry = LabRegistry(root)
    registry.start({"hypothesis": "h", "family": "macd", "strategy_ref": {"strategy_id": "gold_1m_macd"}}, exp_id="e1")
    registry.finalize(
        "e1",
        status="valid",
        results={"objective": {"walkforward": {"passed": True}, "holdout": {"passed": True}}},
    )
    registry.record_holdout_consumption("e1", {"start": "2026-01-01", "end": "2026-02-01"})

    result = lab_expectation_for_strategy(root, "gold_1m_macd")

    assert result["paper_eligible"] is False
    assert result["source_exp_id"] == "e1"
    assert "execution_candidate_identity_missing" in result["blockers"]


def test_record_paper_eligibility_writes_lab_scoped_flag(tmp_path: Path):
    root = tmp_path / "outputs"
    registry = LabRegistry(root)
    registry.start({"hypothesis": "h", "family": "macd", "strategy_ref": {"strategy_id": "gold_1m_macd"}}, exp_id="e1")

    payload = record_paper_eligibility(root, "gold_1m_macd")

    assert payload["paper_eligible"] is False
    assert load_json(root / "lab" / "promotion" / "gold_1m_macd.json")[0]["paper_eligible"] is False
