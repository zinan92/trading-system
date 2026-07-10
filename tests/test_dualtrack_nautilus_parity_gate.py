from __future__ import annotations

from pathlib import Path

from pipelines.dualtrack_nautilus_parity_gate import build_fixture_gate, main
from services.journal_store import write_json


def test_fixture_gate_requires_every_named_parity_class(tmp_path: Path) -> None:
    result = build_fixture_gate(tmp_path / "outputs")

    assert result["status"] == "blocked"
    assert "scale_in_weighted_average" in result["blockers"]
    assert "market_entry_long_short" in result["blockers"]


def test_fixture_gate_accepts_only_recorded_exact_passes_for_implemented_class(tmp_path: Path) -> None:
    root = tmp_path / "outputs"
    for name in ("long_stop", "short_stop"):
        write_json(root / "dualtrack" / "nautilus" / "parity" / f"{name}.json", [{"parity": {"status": "pass"}}])

    result = build_fixture_gate(root)
    market = next(row for row in result["classes"] if row["class"] == "market_entry_long_short")

    assert market["status"] == "pass"
    assert result["status"] == "blocked"


def test_fixture_gate_cli_fails_closed_without_preflight(tmp_path: Path) -> None:
    assert main([
        "--nautilus-python", "/missing/nautilus-python",
        "--output-root", str(tmp_path / "outputs"),
    ]) == 2
