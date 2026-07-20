from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pipelines.dualtrack_nautilus_parity_gate as parity_gate
from pipelines.dualtrack_nautilus_parity_gate import build_fixture_gate, main
from services.dualtrack_nautilus_parity_contract import (
    FIXTURE_CLASSES,
    PLATFORM_CODE_PATHS,
    platform_parity_code_hash,
)
from services.journal_store import write_json


def test_fixture_gate_requires_every_named_parity_class(tmp_path: Path) -> None:
    result = build_fixture_gate(tmp_path / "outputs")

    assert result["status"] == "blocked"
    assert "scale_in_weighted_average" in result["blockers"]
    assert "market_entry_long_short" in result["blockers"]


def test_fixture_gate_persists_blocked_result_when_code_evidence_is_unreadable(
    tmp_path: Path,
    monkeypatch,
) -> None:
    def unreadable_code() -> str:
        raise OSError("missing semantic dependency")

    monkeypatch.setattr(parity_gate, "platform_parity_code_hash", unreadable_code)

    result = build_fixture_gate(tmp_path / "outputs")

    assert result["status"] == "blocked"
    assert "platform_code_evidence" in result["blockers"]
    assert result["platform_code_hash"] == ""


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


def test_platform_hash_changes_with_accounting_or_shadow_semantics(tmp_path: Path) -> None:
    repository = Path(__file__).resolve().parents[1]
    for relative in PLATFORM_CODE_PATHS:
        source = repository / relative
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source.read_bytes())
    baseline = platform_parity_code_hash(tmp_path)

    accounting = tmp_path / "services" / "accounting_projection.py"
    accounting.write_bytes(accounting.read_bytes() + b"\n# semantic drift\n")
    accounting_drift = platform_parity_code_hash(tmp_path)
    accounting.write_bytes((repository / "services" / "accounting_projection.py").read_bytes())
    shadow = tmp_path / "services" / "strategy_shadow_nautilus.py"
    shadow.write_bytes(shadow.read_bytes() + b"\n# semantic drift\n")

    assert accounting_drift != baseline
    assert platform_parity_code_hash(tmp_path) != baseline


def test_fixture_gate_pass_is_bound_to_runtime_contracts_and_current_code(tmp_path: Path) -> None:
    root = tmp_path / "outputs"
    scenarios = {scenario for rows in FIXTURE_CLASSES.values() for scenario in rows}
    generated_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    code_hash = platform_parity_code_hash()
    contract = {
        "contract_hash": "sha256:execution-contract",
        "fee_contract_hash": "sha256:fee-contract",
    }
    for scenario in scenarios:
        write_json(root / "dualtrack" / "nautilus" / "parity" / f"{scenario}.json", [{
            "scenario": scenario,
            "generated_at": generated_at,
            "platform_code_hash": code_hash,
            "nautilus_version": "1.230.0",
            "execution_contract": contract,
            "parity": {"status": "pass"},
        }])
    write_json(
        root / "dualtrack" / "nautilus" / "parity" / "historical_machine_residual_units.json",
        [{
            "status": "pass",
            "generated_at": generated_at,
            "platform_code_hash": code_hash,
        }],
    )

    result = build_fixture_gate(root)

    assert result["status"] == "pass"
    assert result["blockers"] == []
    assert result["contracts"] == {
        "execution_contract_hash": "sha256:execution-contract",
        "fee_contract_hash": "sha256:fee-contract",
    }
    assert result["nautilus_version"] == "1.230.0"
    assert result["platform_code_hash"] == code_hash
    assert result["generated_at"] == generated_at


def test_fixture_gate_cannot_restamp_stale_or_old_code_child_evidence(tmp_path: Path) -> None:
    root = tmp_path / "outputs"
    stale_at = (datetime.now(timezone.utc) - timedelta(days=8)).replace(microsecond=0).isoformat()
    contract = {
        "contract_hash": "sha256:execution-contract",
        "fee_contract_hash": "sha256:fee-contract",
    }
    scenarios = {scenario for rows in FIXTURE_CLASSES.values() for scenario in rows}
    for scenario in scenarios:
        write_json(root / "dualtrack" / "nautilus" / "parity" / f"{scenario}.json", [{
            "scenario": scenario,
            "generated_at": stale_at,
            "platform_code_hash": "sha256:old-code",
            "nautilus_version": "1.229.0",
            "execution_contract": contract,
            "parity": {"status": "pass"},
        }])
    write_json(
        root / "dualtrack" / "nautilus" / "parity" / "historical_machine_residual_units.json",
        [{
            "status": "pass",
            "generated_at": stale_at,
            "platform_code_hash": "sha256:old-code",
        }],
    )

    result = build_fixture_gate(root)

    assert result["status"] == "blocked"
    assert "platform_code_evidence" in result["blockers"]
    assert "fixture_timestamp_stale" in result["blockers"]
    assert "nautilus_runtime_evidence" in result["blockers"]
