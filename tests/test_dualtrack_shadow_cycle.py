from __future__ import annotations

from pathlib import Path

import pipelines.dualtrack_shadow_cycle as shadow_cycle
from services.journal_store import load_json


def test_shadow_cycle_fails_closed_when_prepare_fails(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(shadow_cycle.dualtrack_shadow_input_prepare, "main", lambda _args: 2)
    monkeypatch.setattr(shadow_cycle.dualtrack_nautilus_shadow_replay, "main", lambda _args: (_ for _ in ()).throw(AssertionError("must not replay")))
    monkeypatch.setattr(shadow_cycle.dualtrack_shadow_cutover_status, "main", lambda _args: 2)

    result = shadow_cycle.main([
        "--cycle-id", "2026-07-10_DAY",
        "--nautilus-python", "/missing/python",
        "--output-root", str(tmp_path / "outputs"),
    ])

    assert result == 2
    artifact = load_json(tmp_path / "outputs" / "dualtrack" / "shadow_runs" / "2026-07-10_DAY.json")[-1]
    assert artifact["status"] == "blocked"
    assert artifact["replay_exit_code"] == 2


def test_shadow_cycle_records_pass_without_marking_real_money_eligible(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(shadow_cycle.dualtrack_shadow_input_prepare, "main", lambda _args: 0)
    monkeypatch.setattr(shadow_cycle.dualtrack_nautilus_shadow_replay, "main", lambda _args: 0)
    monkeypatch.setattr(shadow_cycle.dualtrack_shadow_cutover_status, "main", lambda _args: 2)

    result = shadow_cycle.main([
        "--cycle-id", "2026-07-10_DAY",
        "--nautilus-python", "/safe/python",
        "--output-root", str(tmp_path / "outputs"),
    ])

    assert result == 0
    artifact = load_json(tmp_path / "outputs" / "dualtrack" / "shadow_runs" / "2026-07-10_DAY.json")[-1]
    assert artifact["status"] == "replayed"
    assert artifact["real_money_eligible"] is False
