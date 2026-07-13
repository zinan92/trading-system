from pathlib import Path

import pytest

from services.dualtrack_evidence_gap import DualTrackEvidenceGapStore
from services.journal_store import load_json, write_json


def test_records_closed_cycle_as_non_pnl_non_closed_loop_evidence_gap(tmp_path: Path) -> None:
    output = tmp_path / "outputs"
    write_json(output / "dualtrack" / "runner" / "2026-07-11_NIGHT.json", [])

    result = DualTrackEvidenceGapStore(output).record(
        "2026-07-11_NIGHT",
        reason="runner_not_observed_during_cycle",
        detected_at="2026-07-12T02:00:00+00:00",
    )

    assert result["status"] == "evidence_gap"
    assert result["counts_as_closed_loop"] is False
    assert result["eligible_for_paper_pnl"] is False
    assert result["eligible_for_self_evolution"] is False
    assert result["reconstruction_policy"] == "do_not_synthesize_missing_execution_evidence"
    saved = load_json(output / "dualtrack" / "evidence_gaps" / "2026-07-11_NIGHT.json")[-1]
    assert saved == result


def test_refuses_gap_before_close_or_when_attribution_exists(tmp_path: Path) -> None:
    output = tmp_path / "outputs"
    store = DualTrackEvidenceGapStore(output)

    with pytest.raises(ValueError, match="before cycle end"):
        store.record(
            "2026-07-11_NIGHT",
            reason="runner_not_observed_during_cycle",
            detected_at="2026-07-11T14:00:00+00:00",
        )

    write_json(output / "dualtrack" / "attribution" / "2026-07-11_NIGHT.json", [{"status": "closed"}])
    with pytest.raises(ValueError, match="attribution exists"):
        store.record(
            "2026-07-11_NIGHT",
            reason="runner_not_observed_during_cycle",
            detected_at="2026-07-12T02:00:00+00:00",
        )
