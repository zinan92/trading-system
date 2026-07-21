from pathlib import Path

import pytest

from services.journal_store import load_json, write_json
from services.strategy_control_plane import StrategyControlPlane
from services.strategy_cycle_package import StrategyCyclePackager
from tests.test_strategy_control_plane import proposal


def test_cycle_package_is_idempotent_and_links_the_terminal_production_ledger(tmp_path: Path) -> None:
    output = tmp_path / "outputs"
    cycle_id = "2026-07-05_DAY"
    plane = StrategyControlPlane(output)
    saved = plane.upsert_proposal(proposal(cycle_id, "ai"))
    plan = plane.lock_production_plan(cycle_id, selected_proposal_id=saved["proposal_id"])

    class Adapter:
        name = "nautilus_paper"

        def snapshot(self, _cycle_id):
            return {
                "engine": self.name,
                "orders": [{"order_id": "o1", "state": "cancelled", "strategy_plan_id": plan["strategy_plan_id"], "strategy_plan_version": plan["version"]}],
                "fills": [{"fill_id": "f1", "strategy_plan_id": plan["strategy_plan_id"], "strategy_plan_version": plan["version"]}],
                "positions": [],
                "account": {"equity": 10003.5},
                "pnl": {"realized": 3.5, "unrealized": 0.0},
            }

        def reconcile(self, _cycle_id):
            return {"status": "ok", "issues": []}

    write_json(output / "dualtrack" / "nautilus_authoritative" / "events" / f"{cycle_id}.json", [
        {"event_id": "e1", "event_started_at": "2026-07-05T01:00:00+00:00", "open": 100, "high": 105, "low": 99, "close": 104},
        {"event_id": "e2", "event_started_at": "2026-07-05T12:59:00+00:00", "open": 104, "high": 112, "low": 103, "close": 110},
    ])
    packager = StrategyCyclePackager(output, adapter=Adapter())
    first = packager.package(cycle_id, now="2026-07-05T13:00:00+00:00")
    second = packager.package(cycle_id, now="2026-07-05T13:05:00+00:00")

    assert first == second
    assert first["status"] == "closed"
    assert first["review"]["realized_pnl"] == 3.5
    assert first["review"]["realized_pnl_source"] == "execution.pnl.realized"
    assert first["market_evidence"]["event_count"] == 2
    assert first["shadow_generation"]["future_function"] is False
    assert first["strategy_shadows"]
    assert first["traceability"]["orders_linked"] is True
    assert first["traceability"]["fills_linked"] is True
    assert first["safety"]["writes_production_ledger"] is False


def test_cycle_package_is_blocked_when_execution_reconciliation_has_drift(tmp_path: Path) -> None:
    output = tmp_path / "outputs"
    cycle_id = "2026-07-05_NIGHT"
    plane = StrategyControlPlane(output)
    saved = plane.upsert_proposal(proposal(cycle_id, "ai"))
    plane.lock_production_plan(cycle_id, selected_proposal_id=saved["proposal_id"])

    class DriftedAdapter:
        name = "nautilus_paper"

        def snapshot(self, _cycle_id):
            return {"engine": self.name, "orders": [], "fills": [], "positions": []}

        def reconcile(self, _cycle_id):
            return {"status": "drift", "issues": [{"field": "fills"}]}

    package = StrategyCyclePackager(output, adapter=DriftedAdapter()).package(
        cycle_id,
        now="2026-07-06T01:00:00+00:00",
    )

    assert package["status"] == "blocked"
    assert package["review"]["status"] == "blocked"
    assert package["review"]["reconciliation_status"] == "drift"


def test_cycle_package_appends_review_repair_without_overwriting_original(tmp_path: Path) -> None:
    output = tmp_path / "outputs"
    cycle_id = "2026-07-04_DAY"
    path = output / "dualtrack" / "strategy_cycle_packages" / f"{cycle_id}.json"
    original = {
        "schema_version": "strategy-cycle-package-v1",
        "cycle_id": cycle_id,
        "execution": {"pnl": {"realized": 8.12812}, "fills": [{"event": "entry"}, {"event": "target"}]},
        "review": {"schema_version": "production-cycle-review-v1", "realized_pnl": 0.0, "fill_count": 2},
        "package_hash": "original-hash",
    }
    write_json(path, [original])

    repaired = StrategyCyclePackager(output).package(cycle_id, now="2026-07-05T01:00:00+00:00")
    rows = load_json(path)

    assert len(rows) == 2
    assert rows[0] == original
    assert repaired["review"]["realized_pnl"] == 8.12812
    assert repaired["review"]["realized_pnl_source"] == "execution.pnl.realized"
    assert "repair_review_realized_pnl_source" in repaired["revision"]["reasons"]
    assert repaired["revision"]["supersedes_package_hash"] == "original-hash"
    assert repaired["revision"]["original_record_preserved"] is True


def test_cycle_package_shadow_backfill_accepts_price_as_event_close(tmp_path: Path) -> None:
    output = tmp_path / "outputs"
    cycle_id = "2026-07-03_NIGHT"
    plane = StrategyControlPlane(output)
    saved = plane.upsert_proposal(proposal(cycle_id, "ai"))
    plan = plane.lock_production_plan(cycle_id, selected_proposal_id=saved["proposal_id"])
    path = output / "dualtrack" / "strategy_cycle_packages" / f"{cycle_id}.json"
    write_json(path, [{
        "schema_version": "strategy-cycle-package-v1",
        "cycle_id": cycle_id,
        "status": "closed",
        "strategy_plan": plan,
        "proposals": [saved],
        "execution": {"pnl": {"realized": 0.0}},
        "review": {"realized_pnl": 0.0},
        "market_evidence": {"event_count": 2},
        "package_hash": "before-shadow",
    }])
    write_json(output / "dualtrack" / "nautilus_authoritative" / "events" / f"{cycle_id}.json", [
        {"event_started_at": "2026-07-03T13:00:00+00:00", "open": 100, "high": 101, "low": 99, "price": 100.5},
        {"event_started_at": "2026-07-03T13:01:00+00:00", "open": 100.5, "high": 102, "low": 100, "price": 101.5},
    ])

    repaired = StrategyCyclePackager(output).package(cycle_id)

    assert repaired["shadow_generation"]["market_event_count"] == 2
    assert repaired["shadow_generation"]["future_function"] is False
    assert repaired["strategy_shadows"]
    assert "backfill_strategy_shadow_evidence" in repaired["revision"]["reasons"]


def test_cycle_package_maps_legacy_review_to_explicit_non_auto_next_change(tmp_path: Path) -> None:
    output = tmp_path / "outputs"
    cycle_id = "2026-07-02_DAY"
    plane = StrategyControlPlane(output)
    saved = plane.upsert_proposal(proposal(cycle_id, "ai", direction="short"))
    plan = plane.lock_production_plan(cycle_id, selected_proposal_id=saved["proposal_id"])

    class Adapter:
        name = "nautilus_paper"

        def snapshot(self, _cycle_id):
            return {"engine": self.name, "orders": [], "fills": [], "positions": [], "pnl": {"realized": 0}}

        def reconcile(self, _cycle_id):
            return {"status": "ok", "issues": []}

    write_json(output / "dualtrack" / "reviews" / f"{cycle_id}_machine.json", [{
        "direction_review": {"verdict": "adjust", "decision": "short", "realized_regime": "long"},
        "next_iteration": {"validation_rule": "10 cycles / 30 trades"},
    }])
    write_json(output / "dualtrack" / "nautilus_authoritative" / "events" / f"{cycle_id}.json", [])

    packaged = StrategyCyclePackager(output, adapter=Adapter()).package(cycle_id)

    change = packaged["review"]["next_version_change"]
    assert change["dimension"] == "direction"
    assert change["auto_apply"] is False
    assert "计划 short" in change["summary"]
    assert packaged["strategy_plan"]["strategy_plan_id"] == plan["strategy_plan_id"]


def test_cycle_package_failed_shadow_backfill_is_idempotent(tmp_path: Path) -> None:
    output = tmp_path / "outputs"
    cycle_id = "2026-07-01_DAY"
    saved_plan = proposal(cycle_id, "ai") | {
        "strategy_plan_id": "plan-idempotent",
        "version": 1,
        "status": "superseded",
    }
    path = output / "dualtrack" / "strategy_cycle_packages" / f"{cycle_id}.json"
    original = {
        "schema_version": "strategy-cycle-package-v1",
        "cycle_id": cycle_id,
        "status": "closed",
        "strategy_plan": saved_plan,
        "proposals": [],
        "execution": {"pnl": {"realized": 0.0}},
        "review": {"realized_pnl": 0.0},
        "market_evidence": {"event_count": 0},
        "package_hash": "before-backfill",
    }
    write_json(path, [original])
    packager = StrategyCyclePackager(output)

    first = packager.package(cycle_id, now="2026-07-02T01:00:00+00:00")
    second = packager.package(cycle_id, now="2026-07-02T01:01:00+00:00")

    assert first == second
    assert first["shadow_generation"]["status"] == "skipped"
    assert first["shadow_generation"]["backfill_attempted"] is True
    assert len(load_json(path)) == 2


def test_shadow_runtime_error_is_isolated_from_terminal_package(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    output = tmp_path / "outputs"
    cycle_id = "2026-06-30_NIGHT"
    plane = StrategyControlPlane(output)
    saved = plane.upsert_proposal(proposal(cycle_id, "ai"))
    plane.lock_production_plan(cycle_id, selected_proposal_id=saved["proposal_id"])

    class Adapter:
        name = "nautilus_paper"

        def snapshot(self, _cycle_id):
            return {"engine": self.name, "orders": [], "fills": [], "positions": [], "pnl": {"realized": 0}}

        def reconcile(self, _cycle_id):
            return {"status": "ok", "issues": []}

    write_json(output / "dualtrack" / "nautilus_authoritative" / "events" / f"{cycle_id}.json", [
        {"event_started_at": "2026-06-30T13:00:00+00:00", "open": 100, "high": 101, "low": 99, "price": 100},
    ])
    monkeypatch.setattr("services.strategy_cycle_package.StrategyShadowRunner.run", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("shadow bug")))

    package = StrategyCyclePackager(output, adapter=Adapter()).package(cycle_id)

    assert package["status"] == "closed"
    assert package["shadow_generation"]["status"] == "blocked"
    assert package["shadow_generation"]["errors"][0]["error"] == "RuntimeError: shadow bug"
    assert load_json(output / "dualtrack" / "strategy_cycle_packages" / f"{cycle_id}.json")[-1] == package
