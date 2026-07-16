from __future__ import annotations

from pathlib import Path

from services.dualtrack_nautilus_execution_adapter import REPLAY_VERSION
from services.dualtrack_shadow_execution_adapter import ShadowingExecutionEngineAdapter
from services.journal_store import load_json, write_json


class FakeAdapter:
    def __init__(self, name: str, *, failure: str = "") -> None:
        self.name = name
        self.failure = failure
        self.commands: list[dict] = []
        self.events: list[dict] = []

    def submit_order(self, command: dict) -> dict:
        self.commands.append(dict(command))
        if self.failure == "submit":
            raise RuntimeError("shadow submit failed")
        return {"state": "accepted", "order_id": f"{self.name}-order"}

    def process_market_event(self, event: dict) -> dict:
        self.events.append(dict(event))
        if self.failure == "event":
            raise RuntimeError("shadow event failed")
        return {"status": "ok", "event_id": event.get("event_id")}

    def snapshot(self, cycle_id: str, **_kwargs) -> dict:
        return {
            "engine": self.name,
            "cycle_id": cycle_id,
            "orders": [],
            "fills": [],
            "positions": list(getattr(self, "positions", [])),
            "capabilities": {"replay_version": REPLAY_VERSION},
        }

    def reconcile(self, cycle_id: str) -> dict:
        return {"engine": self.name, "cycle_id": cycle_id, "status": "ok", "issues": []}

    def cancel_orders(self, cycle_id: str, **kwargs) -> dict:
        command = {"cycle_id": cycle_id, **kwargs}
        self.commands.append(command)
        if self.failure == "cancel":
            raise RuntimeError("shadow cancel failed")
        return {"status": "cancelled", "cancelled_order_ids": list(kwargs.get("order_ids") or [])}

    def flush(self, cycle_id: str) -> dict:
        if self.failure == "flush":
            raise RuntimeError("shadow flush failed")
        return {"status": "replayed", "cycle_id": cycle_id}


def _command() -> dict:
    return {
        "cycle_id": "2026-07-16_DAY",
        "ts": "2026-07-16T01:00:00+00:00",
        "side": "buy",
        "event": "entry",
        "order_type": "limit",
        "price": 4000.0,
        "quantity": 0.1,
        "strategy_plan_id": "plan-1",
        "strategy_plan_version": 1,
    }


def _event() -> dict:
    return {
        "event_id": "market-1",
        "cycle_id": "2026-07-16_DAY",
        "ts_event": "2026-07-16T01:01:00+00:00",
        "price": 4001.0,
        "fresh": True,
        "is_synthetic": False,
        "source": "market_db:binance_usdm_futures",
        "provider": "binance_usdm_futures",
        "instrument_id": "XAUUSDT",
    }


def test_mirrors_exact_input_without_changing_authoritative_result(tmp_path: Path) -> None:
    authoritative = FakeAdapter("legacy_paper")
    shadow = FakeAdapter("nautilus_paper")
    adapter = ShadowingExecutionEngineAdapter(tmp_path / "outputs", authoritative=authoritative, shadow=shadow)

    receipt = adapter.submit_order(_command())
    result = adapter.process_market_event(_event())

    assert receipt == {"state": "accepted", "order_id": "legacy_paper-order"}
    assert result == {"status": "ok", "event_id": "market-1"}
    assert authoritative.commands == [_command()]
    assert shadow.commands == [{**_command(), "authoritative_order_id": "legacy_paper-order"}]
    assert authoritative.events == shadow.events == [_event()]
    assert adapter.name == "legacy_paper"
    assert adapter.snapshot("2026-07-16_DAY")["engine"] == "legacy_paper"
    assert adapter.reconcile("2026-07-16_DAY")["engine"] == "legacy_paper"
    status = load_json(
        tmp_path / "outputs" / "dualtrack" / "nautilus_shadow_runtime" / "2026-07-16_DAY.json"
    )
    assert [row["status"] for row in status] == ["ok", "ok"]


def test_shadow_failure_is_visible_without_failing_authoritative_result(tmp_path: Path) -> None:
    authoritative = FakeAdapter("legacy_paper")
    shadow = FakeAdapter("nautilus_paper", failure="event")
    adapter = ShadowingExecutionEngineAdapter(tmp_path / "outputs", authoritative=authoritative, shadow=shadow)

    result = adapter.process_market_event(_event())

    assert result == {"status": "ok", "event_id": "market-1"}
    assert authoritative.events == [_event()]
    rows = load_json(
        tmp_path / "outputs" / "dualtrack" / "nautilus_shadow_runtime" / "2026-07-16_DAY.json"
    )
    assert rows[-1]["status"] == "error"
    assert rows[-1]["operation"] == "process_market_event"
    assert rows[-1]["error"] == "shadow event failed"
    assert rows[-1]["authoritative_engine"] == "legacy_paper"
    assert rows[-1]["shadow_engine"] == "nautilus_paper"


def test_missing_shadow_runtime_is_explicit_but_authoritative_stays_available(tmp_path: Path) -> None:
    authoritative = FakeAdapter("legacy_paper")
    adapter = ShadowingExecutionEngineAdapter(
        tmp_path / "outputs",
        authoritative=authoritative,
        shadow=None,
        blocker="nautilus_shadow_runtime_missing",
    )

    assert adapter.submit_order(_command())["state"] == "accepted"
    row = load_json(
        tmp_path / "outputs" / "dualtrack" / "nautilus_shadow_runtime" / "2026-07-16_DAY.json"
    )[-1]
    assert row["status"] == "blocked"
    assert row["blocker"] == "nautilus_shadow_runtime_missing"


def test_shadow_batch_flush_failure_is_visible_without_raising_to_production(tmp_path: Path) -> None:
    adapter = ShadowingExecutionEngineAdapter(
        tmp_path / "outputs",
        authoritative=FakeAdapter("legacy_paper"),
        shadow=FakeAdapter("nautilus_paper", failure="flush"),
    )

    result = adapter.flush_shadow("2026-07-16_DAY")

    assert result["status"] == "error"
    row = load_json(
        tmp_path / "outputs" / "dualtrack" / "nautilus_shadow_runtime" / "2026-07-16_DAY.json"
    )[-1]
    assert row["operation"] == "flush"
    assert row["error"] == "shadow flush failed"


def test_cancel_is_mirrored_but_authoritative_receipt_remains_the_result(tmp_path: Path) -> None:
    authoritative = FakeAdapter("legacy_paper")
    shadow = FakeAdapter("nautilus_paper")
    adapter = ShadowingExecutionEngineAdapter(tmp_path / "outputs", authoritative=authoritative, shadow=shadow)

    result = adapter.cancel_orders(
        "2026-07-16_DAY",
        order_ids=["legacy-order-1"],
        ts="2026-07-16T01:03:00+00:00",
        reason="regrid",
    )

    assert result == {"status": "cancelled", "cancelled_order_ids": ["legacy-order-1"]}
    assert authoritative.commands == shadow.commands
    assert authoritative.commands[-1]["order_ids"] == ["legacy-order-1"]


def test_close_mirror_carries_pre_close_position_identity_for_hedged_replay(tmp_path: Path) -> None:
    authoritative = FakeAdapter("legacy_paper")
    authoritative.positions = [{
        "trade_id": "legacy-trade-1",
        "position_id": "manual",
        "status": "open",
        "side": "long",
        "remaining_units": 0.25,
        "entry_price": 4000.0,
    }]
    shadow = FakeAdapter("nautilus_paper")
    adapter = ShadowingExecutionEngineAdapter(tmp_path / "outputs", authoritative=authoritative, shadow=shadow)
    command = {
        **_command(),
        "side": "sell",
        "event": "flatten",
        "order_type": "market",
        "trade_id": "legacy-trade-1",
        "position_id": "manual",
        "quantity": 0.25,
    }

    adapter.submit_order(command)

    mirrored = shadow.commands[-1]
    assert mirrored["target_authoritative_trade_id"] == "legacy-trade-1"
    assert mirrored["target_position_side"] == "long"
    assert mirrored["target_entry_price"] == 4000.0
    assert mirrored["target_remaining_units"] == 0.25
    assert mirrored["authoritative_order_id"] == "legacy_paper-order"


def test_successful_shadow_reconciliation_refreshes_cutover_gate(tmp_path: Path) -> None:
    class SnapshotShadow(FakeAdapter):
        def flush(self, cycle_id: str) -> dict:
            return {"status": "replayed", "cycle_id": cycle_id, "snapshot": self.snapshot(cycle_id)}

    output = tmp_path / "outputs"
    cycle_id = "2026-07-16_DAY"
    write_json(
        output / "dualtrack" / "nautilus" / "parity" / "current.json",
        [{"status": "pass", "blockers": []}],
    )
    write_json(
        output / "dualtrack" / "nautilus_paper" / "commands" / f"{cycle_id}.json",
        [{"command_id": "command-1"}],
    )
    write_json(
        output / "dualtrack" / "nautilus_paper" / "events" / f"{cycle_id}.json",
        [_event()],
    )
    preflight = output / "dualtrack" / "nautilus" / "instrument_preflight.json"
    fee_model = {
        "mode": "account_observed",
        "maker_fee_rate": "0",
        "taker_fee_rate": "0.0004",
        "source": "account",
        "observed_at": "2026-07-16T00:00:00+00:00",
        "real_money_eligible": False,
    }
    write_json(preflight, [{"status": "ready_for_paper_shadow", "fee_model": fee_model}])
    shadow = SnapshotShadow("nautilus_paper")
    shadow.preflight_path = preflight
    shadow.config = {
        "paper_fee_model": fee_model,
        "market_data": {"provider": "binance_usdm_futures"},
    }
    adapter = ShadowingExecutionEngineAdapter(
        output,
        authoritative=FakeAdapter("legacy_paper"),
        shadow=shadow,
    )

    open_result = adapter.flush_shadow(cycle_id)
    open_gate = load_json(output / "dualtrack" / "cutover" / "shadow_gate_current.json")[-1]
    assert open_result["reconciliation_status"] == "pass"
    assert open_gate["observed_consecutive_passes"] == 0
    assert open_gate["blocker"] == "cycle_incomplete"

    write_json(output / "dualtrack" / "attribution" / f"{cycle_id}.json", [{"status": "closed"}])
    result = adapter.flush_shadow(cycle_id, cycle_complete=True)

    gate = load_json(output / "dualtrack" / "cutover" / "shadow_gate_current.json")[-1]
    assert result["reconciliation_status"] == "pass"
    assert result["cutover_status"] == "blocked"
    assert gate["observed_consecutive_passes"] == 1
    assert gate["blocker"] == "requires_7_consecutive_passes_observed_1"
    evidence = load_json(output / "dualtrack" / "reconciliation" / f"{cycle_id}.json")[-1]["shadow_evidence"]
    assert evidence["qualifies_for_cutover"] is True
    assert evidence["qualification_checks"] == {
        "cycle_complete": True,
        "cycle_close_artifact": True,
        "command_activity": True,
        "market_event_activity": True,
        "trusted_market_events": True,
        "market_provider_match": True,
        "fee_contract_match": True,
        "replay_version_pinned": True,
    }

    adapter.flush_shadow(cycle_id)
    later_gate = load_json(output / "dualtrack" / "cutover" / "shadow_gate_current.json")[-1]
    assert later_gate["observed_consecutive_passes"] == 1
    assert load_json(output / "dualtrack" / "reconciliation" / f"{cycle_id}.json")[-1][
        "shadow_evidence"
    ]["qualifies_for_cutover"] is True
