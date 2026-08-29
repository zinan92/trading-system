from __future__ import annotations

from pathlib import Path
from copy import deepcopy

import pytest

import pipelines.dashboard_server as dashboard_server
import pipelines.dualtrack_execution_reconcile as reconcile_pipeline
from services.accounting_projection import AccountingContractError
from services.journal_store import load_json, write_json
from tests.test_dualtrack_dt2_machine_runner import TEST_CONFIG


@pytest.fixture(autouse=True)
def _isolate_legacy_execution_engine(monkeypatch: pytest.MonkeyPatch):
    config = deepcopy(TEST_CONFIG)
    config["execution_engine"] = {
        "authoritative": "legacy_paper",
        "shadow": "none",
        "real_money_eligible": False,
    }
    factory = lambda *args, **kwargs: deepcopy(config)
    monkeypatch.setattr("services.dualtrack_config.dualtrack_config", factory)
    monkeypatch.setattr(dashboard_server, "dualtrack_config", factory)


def test_execution_endpoint_separates_authoritative_and_shadow_reconciliation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    output = tmp_path / "outputs"
    cycle_id = "2026-07-10_DAY"
    write_json(
        output / "dualtrack" / "reconciliation" / f"{cycle_id}.json",
        [{"status": "blocked", "blocker": "instrument_definition_missing"}],
    )
    write_json(
        output / "dualtrack" / "cutover" / "shadow_gate_current.json",
        [{"status": "blocked", "blocker": "candidate_snapshot_missing"}],
    )
    monkeypatch.setattr(
        dashboard_server,
        "_dualtrack_mark_price",
        lambda *args, **kwargs: {"price": 100.0, "fresh": True, "source": "test"},
    )

    payload = dashboard_server.build_dualtrack_execution_response(cycle_id, output_root=output, as_of="2026-07-10T02:00:00+00:00")

    assert payload["schema_version"] == "dualtrack-execution-v1"
    assert payload["engine"] == "legacy_paper"
    assert payload["reconciliation"]["engine"] == "legacy_paper"
    assert payload["reconciliation"]["status"] == "ok"
    assert payload["execution_shadow_reconciliation"] == {
        "status": "blocked",
        "blocker": "instrument_definition_missing",
    }
    assert payload["shadow_cutover"] == {"status": "blocked", "blocker": "candidate_snapshot_missing"}
    assert payload["accounting_snapshot"]["schema_version"] == "accounting-snapshot-v1"
    assert payload["accounting_snapshot"]["source_name"] == "legacy_paper"
    assert payload["accounting_snapshot"]["counts"] == {
        "order_count": 0,
        "open_order_count": 0,
        "fill_count": 0,
        "entry_fill_count": 0,
        "exit_fill_count": 0,
        "trade_count": 0,
        "open_trade_count": 0,
        "completed_trade_count": 0,
        "position_count": 0,
        "open_position_count": 0,
    }
    assert payload["safety"] == {
        "read_only": True,
        "execution_control": False,
        "machine_track_disclosed": False,
    }
    assert "/api/dualtrack/execution/" not in dashboard_server._DUALTRACK_POST_ENDPOINTS


def test_execution_accounting_projection_failure_is_not_silently_recomputed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class BrokenAdapter:
        name = "broken"

        def snapshot(self, *_args, **_kwargs):
            return {
                "schema_version": "dualtrack-execution-v1",
                "engine": "broken",
                "cycle_id": "2026-07-10_DAY",
                "orders": [],
                "fills": [{"fill_id": "missing-economic-fields"}],
                "positions": [],
                "account": {},
                "pnl": {"realized": 0.0, "unrealized": 0.0},
            }

        def reconcile(self, _cycle_id):
            return {"status": "ok"}

    monkeypatch.setattr(dashboard_server, "build_configured_execution_engine_adapter", lambda *_args, **_kwargs: BrokenAdapter())
    monkeypatch.setattr(
        dashboard_server,
        "_dualtrack_mark_price",
        lambda *_args, **_kwargs: {"price": 100.0, "fresh": True, "source": "test"},
    )

    with pytest.raises(AccountingContractError, match="fill price"):
        dashboard_server.build_dualtrack_execution_response(
            "2026-07-10_DAY",
            output_root=tmp_path / "outputs",
            as_of="2026-07-10T02:00:00+00:00",
        )


def test_cloud_passive_read_model_surfaces_missing_execution_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unavailable(*_args, **_kwargs):
        raise RuntimeError("Nautilus paper switch requires attended approval")

    monkeypatch.setenv("GRIDMIND_RUNTIME_MODE", "cloud")
    monkeypatch.setattr(
        dashboard_server,
        "build_configured_execution_engine_adapter",
        unavailable,
    )
    monkeypatch.setattr(
        dashboard_server,
        "_dualtrack_mark_price",
        lambda *_args, **_kwargs: {"price": 100.0, "fresh": True, "source": "test"},
    )

    payload = dashboard_server.build_dualtrack_execution_response(
        "2026-07-10_DAY",
        output_root=tmp_path / "outputs",
        as_of="2026-07-10T02:00:00+00:00",
    )

    assert payload["engine"] == "unavailable"
    assert payload["availability"]["reason"] == "cloud_execution_authority_unavailable"
    assert payload["orders"] == []
    assert payload["positions"] == []
    assert payload["safety"]["execution_control"] is False


def test_local_passive_read_model_surfaces_missing_execution_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unavailable(*_args, **_kwargs):
        raise RuntimeError("Nautilus paper switch evidence gate is not ready")

    monkeypatch.delenv("GRIDMIND_RUNTIME_MODE", raising=False)
    monkeypatch.setattr(
        dashboard_server,
        "build_configured_execution_engine_adapter",
        unavailable,
    )
    monkeypatch.setattr(
        dashboard_server,
        "_dualtrack_mark_price",
        lambda *_args, **_kwargs: {"price": 100.0, "fresh": True, "source": "test"},
    )

    payload = dashboard_server.build_dualtrack_execution_response(
        "2026-07-10_DAY",
        output_root=tmp_path / "outputs",
        as_of="2026-07-10T02:00:00+00:00",
    )

    assert payload["engine"] == "unavailable"
    assert payload["availability"]["reason"] == "local_execution_authority_unavailable"
    assert payload["orders"] == []
    assert payload["positions"] == []
    assert payload["safety"]["execution_control"] is False


def test_reconciliation_pipeline_records_blocked_without_candidate(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    result = reconcile_pipeline.main([
        "--cycle-id", "2026-07-10_DAY",
        "--output-root", str(tmp_path / "outputs"),
        "--json",
    ])

    assert result == 2
    assert '"status": "blocked"' in capsys.readouterr().out
    report = load_json(tmp_path / "outputs" / "dualtrack" / "reconciliation" / "current.json")[-1]
    assert report["status"] == "blocked"
    assert report["blocker"] == "candidate_snapshot_missing"


def test_reconciliation_pipeline_passes_exact_matching_candidate(tmp_path: Path) -> None:
    cycle_id = "2026-07-10_DAY"
    candidate = {
        "engine": "nautilus_shadow",
        "pnl": {"realized": 0.0, "unrealized": 0.0},
        "fills": [],
        "positions": [],
        "account": {
            "starting_cash": 10_000.0,
            "realized_pnl": 0.0,
            "ending_cash": 10_000.0,
            "equity": 10_000.0,
            "margin": 0.0,
                "exposure": 0.0,
                "slippage": 0.0,
                "fees": 0.0,
                "funding": 0.0,
            },
        "reconciliation": {"status": "ok"},
    }
    candidate_path = tmp_path / "candidate.json"
    write_json(candidate_path, [candidate])

    result = reconcile_pipeline.main([
        "--cycle-id", cycle_id,
        "--candidate-path", str(candidate_path),
        "--output-root", str(tmp_path / "outputs"),
    ])

    assert result == 0
    report = load_json(tmp_path / "outputs" / "dualtrack" / "reconciliation" / f"{cycle_id}.json")[-1]
    assert report["status"] == "pass"
    assert report["candidate_engine"] == "nautilus_shadow"
