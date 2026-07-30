from __future__ import annotations

from pathlib import Path

from services.paper_supervisor import PaperSupervisor, SupervisorControlError, SupervisorOperations
from services.paper_supervisor_store import PaperSupervisorStore
from services.trading_system_read_model import project_trading_system_read_model


class _Controls:
    def __init__(
        self,
        *,
        failures: list[SupervisorControlError] | None = None,
        preview_failures: list[SupervisorControlError] | None = None,
    ) -> None:
        self.failures = list(failures or [])
        self.preview_failures = list(preview_failures or [])
        self.preview_calls = 0
        self.prepare_calls = 0
        self.start_calls: list[str] = []

    def observe(self, cycle_id: str) -> dict:
        return {
            "runtime": {"actual_state": "stopped"},
            "plan": {"cycle_id": cycle_id, "strategy_plan_id": "plan-1", "version": 1},
            "orders": [],
            "reconciliation": {"status": "pass"},
        }

    def preview(self, cycle_id: str, observed: dict) -> dict:
        self.preview_calls += 1
        if self.preview_failures:
            raise self.preview_failures.pop(0)
        return {"preview_id": f"preview-{self.preview_calls}"}

    def prepare(self, cycle_id: str, preview: dict, observed: dict) -> dict:
        self.prepare_calls += 1
        return {
            "prepared_start_id": f"prepared-{self.prepare_calls}",
            "expected_order_count": 10,
        }

    def start(self, cycle_id: str, prepared_start_id: str, prepared: dict) -> dict:
        self.start_calls.append(prepared_start_id)
        if self.failures:
            raise self.failures.pop(0)
        return {"runtime": {"actual_state": "running"}}

    def operations(self) -> SupervisorOperations:
        return SupervisorOperations(
            observe=self.observe,
            fresh_preview=self.preview,
            prepare_start=self.prepare,
            start=self.start,
        )


def _supervisor(tmp_path: Path, controls: _Controls, **kwargs: object) -> PaperSupervisor:
    return PaperSupervisor(PaperSupervisorStore(tmp_path), controls.operations(), **kwargs)


def test_market_moved_retries_with_new_preview_and_never_reuses_prepared_id(tmp_path: Path) -> None:
    controls = _Controls(failures=[SupervisorControlError("prepared_start_market_moved")])
    supervisor = _supervisor(tmp_path, controls)
    first = supervisor.tick("2026-07-30_DAY", now="2026-07-30T01:00:00Z")
    second = supervisor.tick("2026-07-30_DAY", now="2026-07-30T01:01:00Z")

    assert first["status"] == "retry_scheduled"
    assert second["status"] == "start_accepted"
    assert controls.start_calls == ["prepared-1", "prepared-2"]
    attempts = PaperSupervisorStore(tmp_path).read_model("2026-07-30_DAY")["attempts"]
    intents = [row for row in attempts if row["kind"] == "start_intent"]
    assert [row["preview_id"] for row in intents] == ["preview-1", "preview-2"]
    assert [row["prepared_start_id"] for row in intents] == ["prepared-1", "prepared-2"]


def test_five_transient_failures_raise_alert_then_long_interval_probe_not_absorbing_stop(tmp_path: Path) -> None:
    controls = _Controls(preview_failures=[SupervisorControlError("prepared_start_market_moved") for _ in range(6)])
    supervisor = _supervisor(tmp_path, controls)
    now = "2026-07-30T01:00:00Z"
    for minute in (0, 1, 3, 8, 18):
        result = supervisor.tick("2026-07-30_DAY", now=f"2026-07-30T{1 + minute // 60:02d}:{minute % 60:02d}:00Z")
    assert result["status"] == "long_interval_probe"
    assert result["alert_required"] is True
    assert result["next_attempt_at"] == "2026-07-30T01:48:00+00:00"
    probe = supervisor.tick("2026-07-30_DAY", now="2026-07-30T01:48:00Z")
    assert probe["status"] == "long_interval_probe"
    assert controls.preview_calls == 6


def test_structural_blocker_never_creates_preview_prepare_or_orders(tmp_path: Path) -> None:
    controls = _Controls()
    normal_observe = controls.observe

    def drift(cycle_id: str) -> dict:
        row = normal_observe(cycle_id)
        row["reconciliation"] = {"status": "drift"}
        return row

    controls.observe = drift  # type: ignore[method-assign]
    supervisor = _supervisor(tmp_path, controls)
    result = supervisor.tick("2026-07-30_DAY", now="2026-07-30T01:00:00Z")

    assert result["status"] == "blocked_structural"
    assert result["machine_code"] == "ledger_reconciliation_drift"
    assert controls.preview_calls == controls.prepare_calls == 0
    assert controls.start_calls == []


def test_unfinished_start_intent_is_unknown_outcome_and_never_retried(tmp_path: Path) -> None:
    store = PaperSupervisorStore(tmp_path)
    with store.lease(cycle_id="2026-07-30_DAY"):
        store.append_event(
            "2026-07-30_DAY",
            "start_intent",
            attempt_id="crashed-attempt",
            fields={"prepared_start_id": "prepared-lost", "preview_id": "preview-lost"},
        )
    controls = _Controls()
    result = _supervisor(tmp_path, controls).tick("2026-07-30_DAY", now="2026-07-30T01:00:00Z")

    assert result["machine_code"] == "control_outcome_unknown"
    assert controls.start_calls == []


def test_start_attempt_cap_blocks_thirteenth_control_call(tmp_path: Path) -> None:
    controls = _Controls()
    store = PaperSupervisorStore(tmp_path)
    store.write_state("2026-07-30_DAY", {"start_call_count": 12})
    result = _supervisor(tmp_path, controls).tick("2026-07-30_DAY", now="2026-07-30T01:00:00Z")

    assert result["machine_code"] == "cycle_start_attempt_cap_reached"
    assert controls.start_calls == []


def test_read_model_exposes_current_cycle_supervisor_attempt_history() -> None:
    model = project_trading_system_read_model({
        "cycle": {"cycle_id": "2026-07-30_DAY"},
        "market": {"provider": "test", "trusted": True},
        "production_plan": {"strategy_plan_id": "plan-1"},
        "production_execution": {"accounting_snapshot": {"schema_version": "accounting-snapshot-v1"}},
        "runtime": {},
        "supervisor": {"status": "retry_scheduled", "attempt_count": 2, "attempts": [{"attempt_id": "a-1"}]},
    }).to_dict()

    assert model["runtime"]["supervisor"]["attempt_count"] == 2
    assert model["runtime"]["supervisor"]["attempts"][0]["attempt_id"] == "a-1"
