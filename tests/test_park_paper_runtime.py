from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from services.park_paper_runtime import ParkPaperRuntime, build_park_authoritative_adapter
from services.park_recording_track import ParkRecordingError, REQUIRED_CATEGORIES
from services.park_strategy_plan import normalize_park_input
from services.park_strategy_plan import build_deterministic_risk_plan
from services.park_strategy_session import ParkStrategyIdentityJournal
from services.park_telegram_runtime import ParkTelegramRouter


def _update(update_id: int, text: str) -> dict:
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id + 100,
            "from": {"id": "park-user"},
            "chat": {"id": "park-chat"},
            "text": text,
        },
    }


class FakePaperAdapter:
    name = "nautilus_paper"

    def __init__(self) -> None:
        self.submit_calls: list[dict] = []
        self.process_calls: list[dict] = []
        self.cancel_calls: list[dict] = []
        self.orders: list[dict] = []
        self.positions: list[dict] = []

    def submit_order(self, command: dict) -> dict:
        self.submit_calls.append(copy.deepcopy(command))
        order_id = f"paper-order-{len(self.submit_calls)}"
        receipt = {
            "order_id": order_id,
            "state": "accepted",
            "cycle_id": command["cycle_id"],
            "side": command["side"],
            "event": command["event"],
            "price": command["price"],
            "quantity": command["quantity"],
        }
        self.orders.append({**receipt, **{key: command[key] for key in ("strategy_session_id", "strategy_revision_id", "plan_digest")}})
        if command.get("event") in {"exit", "stop", "target", "flatten"}:
            target = str(command.get("target_position_id") or command.get("position_id") or "")
            for position in self.positions:
                if str(position.get("position_id") or "") == target:
                    position["status"] = "closed"
                    position["remaining_units"] = 0.0
        return receipt

    def cancel_orders(self, cycle_id: str, *, order_ids=None, strategy_plan_id=None, ts=None, reason="") -> dict:
        self.cancel_calls.append({"cycle_id": cycle_id, "order_ids": list(order_ids or []), "reason": reason})
        ids = {str(value) for value in order_ids or []}
        for row in self.orders:
            if str(row.get("order_id")) in ids:
                row["state"] = "cancelled"
        return {"status": "cancelled", "cancelled_order_ids": sorted(ids)}

    def process_market_event(self, event: dict) -> dict:
        self.process_calls.append(copy.deepcopy(event))
        for order in self.orders:
            if order.get("event") in {"exit", "stop", "target", "flatten"} and order.get("state") == "accepted":
                order["state"] = "filled"
        return {"status": "processed", "event_id": event["event_id"]}

    def snapshot(self, cycle_id: str, *, mark_price=None, mark_fresh=False, mark_source="") -> dict:
        return {
            "schema_version": "dualtrack-execution-v1",
            "engine": self.name,
            "cycle_id": cycle_id,
            "orders": copy.deepcopy(self.orders),
            "fills": [],
            "positions": copy.deepcopy(self.positions),
            "account": {"equity": 10000.0},
            "pnl": {"realized": 0.0, "unrealized": 0.0},
            "capabilities": {"immutable_fill_guard": True, "paper_only": True},
        }

    def reconcile(self, cycle_id: str) -> dict:
        return {"status": "ok", "issues": [], "cycle_id": cycle_id}


def _config() -> dict:
    return {
        "feature_enabled": True,
        "execution_track_count": 1,
        "runtime_mode": "paper_only",
        "control_plane": "telegram",
        "autonomous": False,
        "shadow_mutation": False,
        "feishu_control": False,
        "deployment_required": False,
        "market_data": {"symbol": "GOLD"},
    }


def _evidence() -> dict:
    return {
        "release_sha": "a" * 40,
        "boot_verified": True,
        "trusted_market": True,
        "tick_freshness": True,
        "stale_cycle_state": True,
        "reconciliation": True,
        "immutable_fill": True,
        "park_risk_confirmation": True,
        "paper_only": True,
        "release_sha_ownership": True,
        "boot": True,
        "supervisor_fail_closed": True,
    }


def _router(output: Path, market_ref: dict) -> ParkTelegramRouter:
    return ParkTelegramRouter(
        output,
        park_user_id="park-user",
        chat_id="park-chat",
        market_reader=lambda: dict(market_ref),
        account_reader=lambda _root, _cycle: {
            "equity": 10000.0,
            "reconciliation_healthy": True,
            "open_positions": 0,
            "open_or_accepted_orders": 0,
            "unresolved_runtime": False,
            "pending_terminal_actions": False,
        },
        now=lambda: "2026-08-14T10:00:00+00:00",
        cycle_id_provider=lambda _now: "2026-08-14_DAY",
    )


def _runtime(output: Path, adapter: FakePaperAdapter, market_ref: dict) -> ParkPaperRuntime:
    return ParkPaperRuntime(
        output,
        adapter=adapter,
        park_user_id="park-user",
        chat_id="park-chat",
        config=_config(),
        market_reader=lambda: dict(market_ref),
        now=lambda: "2026-08-14T10:00:00+00:00",
        safety_evidence_reader=_evidence,
    )


def test_confirmation_is_the_only_path_to_paper_mutation(tmp_path: Path) -> None:
    output = tmp_path / "outputs"
    market = {
        "price": 4300.0,
        "trusted": True,
        "fresh": True,
        "source": "paper-feed",
        "provider": "paper-provider",
        "observed_at": "2026-08-14T10:00:00+00:00",
        "symbol": "GOLD",
    }
    router = _router(output, market)
    adapter = FakePaperAdapter()
    runtime = _runtime(output, adapter, market)

    proposal = router.handle_update(_update(1, "做空 DCA，最多 10 倍杠杆，价格区间是 4444~4200，止损4450，止盈4210"))
    awaiting = runtime.run_once()
    assert awaiting["status"] == "awaiting_confirmation"
    assert adapter.submit_calls == []
    assert adapter.process_calls == []

    confirmation = router.handle_update(_update(2, f"confirm {proposal['proposal']['plan_digest']}"))
    assert confirmation["status"] == "confirmed"
    active = runtime.run_once()
    assert active["status"] == "active"
    assert len(adapter.submit_calls) == 1
    command = adapter.submit_calls[0]
    assert command["strategy_session_id"].startswith("session-")
    assert command["strategy_revision_id"].startswith("revision-")
    assert command["plan_digest"] == proposal["proposal"]["plan_digest"]
    assert command["cycle_id"].startswith("park-session-")
    assert active["paper_only"] is True


def test_expired_unconfirmed_runtime_session_is_released_without_mutation(
    tmp_path: Path, monkeypatch
) -> None:
    output = tmp_path / "outputs"
    market = {
        "price": 4300.0,
        "trusted": True,
        "fresh": True,
        "source": "paper-feed",
        "provider": "paper-provider",
        "observed_at": "2026-08-14T10:00:00+00:00",
        "symbol": "GOLD",
    }
    clock = {"value": 1000.0}
    monkeypatch.setattr("services.park_telegram_runtime.time.time", lambda: clock["value"])
    router = _router(output, market)
    adapter = FakePaperAdapter()
    runtime = _runtime(output, adapter, market)
    proposal = router.handle_update(_update(5, "short DCA 10x 4444~4200 stop 4444 tp 4200"))
    assert proposal["status"] == "proposal_created"

    clock["value"] = 2000.0
    released = runtime.run_once()

    assert released["status"] == "idle"
    assert released["reason"] == "confirmation_expired"
    assert adapter.submit_calls == []
    assert adapter.process_calls == []
    assert ParkStrategyIdentityJournal(output).active_session() is None


def test_expired_unconfirmed_runtime_session_stays_blocked_with_exposure(
    tmp_path: Path, monkeypatch
) -> None:
    output = tmp_path / "outputs"
    market = {
        "price": 4300.0,
        "trusted": True,
        "fresh": True,
        "source": "paper-feed",
        "provider": "paper-provider",
        "observed_at": "2026-08-14T10:00:00+00:00",
        "symbol": "GOLD",
    }
    clock = {"value": 1000.0}
    monkeypatch.setattr("services.park_telegram_runtime.time.time", lambda: clock["value"])
    router = _router(output, market)
    adapter = FakePaperAdapter()
    adapter.positions.append({"position_id": "external-1", "status": "open", "remaining_units": 1.0})
    runtime = _runtime(output, adapter, market)
    assert router.handle_update(_update(6, "short DCA 10x 4444~4200 stop 4444 tp 4200"))["status"] == "proposal_created"

    clock["value"] = 2000.0
    blocked = runtime.run_once()

    assert blocked["status"] == "blocked"
    assert blocked["code"] == "confirmation_expired_requires_clean_slate"
    assert ParkStrategyIdentityJournal(output).active_session() is not None
    assert adapter.submit_calls == []


def test_confirmed_neutral_grid_submits_both_owned_legs_and_flattens_at_hard_stop(tmp_path: Path) -> None:
    output = tmp_path / "outputs"
    market = {
        "price": 4300.0,
        "trusted": True,
        "fresh": True,
        "source": "paper-feed",
        "provider": "paper-provider",
        "observed_at": "2026-08-14T10:00:00+00:00",
        "symbol": "GOLD",
    }
    router = _router(output, market)
    adapter = FakePaperAdapter()
    runtime = _runtime(output, adapter, market)

    proposal = router.handle_update(_update(11, "中性网格策略 4450 4100 最大20x杠杆"))
    assert proposal["status"] == "proposal_created"
    confirmed = router.handle_update(_update(12, f"confirm {proposal['proposal']['plan_digest']}"))
    assert confirmed["status"] == "confirmed"

    active = runtime.run_once()
    assert active["status"] == "active"
    assert len(adapter.submit_calls) == 30
    assert {command["side"] for command in adapter.submit_calls} == {"buy", "sell"}
    assert all(command["strategy_session_id"].startswith("session-") for command in adapter.submit_calls)
    assert all(command["strategy_revision_id"].startswith("revision-") for command in adapter.submit_calls)
    assert all("tp" in command and "sl" not in command for command in adapter.submit_calls)
    assert sum(command["price"] * command["quantity"] for command in adapter.submit_calls) <= proposal["plan"]["risk"]["maximum_notional"]
    assert all(
        command["price"] < command["tp"]
        if command["side"] == "buy"
        else command["tp"] < command["price"]
        for command in adapter.submit_calls
    )

    session = ParkStrategyIdentityJournal(output).active_session()
    adapter.positions.append(
        {
            "position_id": "neutral-position-1",
            "trade_id": "neutral-trade-1",
            "status": "open",
            "side": "long",
            "remaining_units": 0.1,
            "strategy_session_id": session["strategy_session_id"],
            "strategy_revision_id": session["strategy_revision_id"],
            "plan_digest": proposal["proposal"]["plan_digest"],
        }
    )
    market.update({"price": 4450.0, "observed_at": "2026-08-14T10:01:00+00:00"})
    paused = runtime.run_once()
    assert paused["status"] == "paused"
    assert paused["terminal_reason"] == "upper_boundary_invalidated"
    assert paused["positions_preserved"] == 0
    assert len(adapter.cancel_calls) == 1
    assert adapter.submit_calls[-1]["event"] == "stop"
    lifecycle_rows = json.loads(
        (output / "park_strategy" / "lifecycle.jsonl").read_text(encoding="utf-8").splitlines()[-1]
    )
    assert lifecycle_rows["event"] == "terminal_action_plan"
    assert lifecycle_rows["boundary"] == "upper"


def test_explicit_stop_and_take_profit_are_parsed_but_never_inferred() -> None:
    normalized = normalize_park_input("做空 DCA，10 倍杠杆，区间 4444~4200，止损 4450，止盈 4210")
    assert normalized["stop_price"] == 4450.0
    assert normalized["take_profit_price"] == 4210.0
    plan = build_deterministic_risk_plan(
        normalized,
        market={
            "price": 4300.0,
            "trusted": True,
            "fresh": True,
            "source": "paper-feed",
            "observed_at": "2026-08-14T10:00:00+00:00",
        },
        account_equity=10000.0,
    )
    assert plan["risk"]["risk_boundary"] == 4450.0
    assert plan["risk"]["risk_boundary_source"] == "explicit_stop_price"


def test_boundary_cancels_entries_closes_only_owned_positions_and_pauses(tmp_path: Path) -> None:
    output = tmp_path / "outputs"
    market = {
        "price": 4300.0,
        "trusted": True,
        "fresh": True,
        "source": "paper-feed",
        "provider": "paper-provider",
        "observed_at": "2026-08-14T10:00:00+00:00",
        "symbol": "GOLD",
    }
    router = _router(output, market)
    adapter = FakePaperAdapter()
    runtime = _runtime(output, adapter, market)
    proposal = router.handle_update(_update(3, "short DCA 10x 4444~4200 stop 4444 tp 4200"))
    router.handle_update(_update(4, f"confirm {proposal['proposal']['plan_digest']}"))
    assert runtime.run_once()["status"] == "active"
    active_session = ParkStrategyIdentityJournal(output).active_session()
    adapter.positions.append(
        {
            "position_id": "park-position-1",
            "trade_id": "park-trade-1",
            "status": "open",
            "side": "short",
            "remaining_units": 0.1,
            "strategy_session_id": active_session["strategy_session_id"],
            "strategy_revision_id": active_session["strategy_revision_id"],
            "plan_digest": proposal["proposal"]["plan_digest"],
        }
    )

    market.update({"price": 4444.0, "observed_at": "2026-08-14T10:01:00+00:00"})
    paused = runtime.run_once()
    assert paused["status"] == "paused"
    assert paused["terminal_reason"] == "stop_price"
    assert len(adapter.cancel_calls) == 1
    assert len(adapter.submit_calls) == 2
    assert adapter.submit_calls[-1]["event"] == "stop"
    assert adapter.submit_calls[-1]["target_position_id"] == "park-position-1"
    assert ParkStrategyIdentityJournal(output).active_session() is None
    assert router.telegram.pending_outbound()

    repeated = runtime.run_once()
    assert repeated["status"] == "idle"
    assert len(adapter.submit_calls) == 2


def test_missing_cutover_evidence_blocks_before_any_mutation(tmp_path: Path) -> None:
    output = tmp_path / "outputs"
    market = {
        "price": 4300.0,
        "trusted": True,
        "fresh": True,
        "source": "paper-feed",
        "provider": "paper-provider",
        "observed_at": "2026-08-14T10:00:00+00:00",
    }
    router = _router(output, market)
    adapter = FakePaperAdapter()
    proposal = router.handle_update(_update(5, "short DCA 10x 4444~4200 stop 4444 tp 4200"))
    router.handle_update(_update(6, f"confirm {proposal['proposal']['plan_digest']}"))
    runtime = ParkPaperRuntime(
        output,
        adapter=adapter,
        park_user_id="park-user",
        chat_id="park-chat",
        config=_config(),
        market_reader=lambda: dict(market),
        now=lambda: "2026-08-14T10:00:00+00:00",
        safety_evidence_reader=lambda: {},
    )
    result = runtime.run_once()
    assert result["status"] == "blocked"
    assert result["code"] == "park_cutover_blocked"
    assert adapter.submit_calls == []
    assert adapter.process_calls == []


def test_confirmation_that_arrives_after_boundary_does_not_open_then_cancel(tmp_path: Path) -> None:
    output = tmp_path / "outputs"
    market = {
        "price": 4300.0,
        "trusted": True,
        "fresh": True,
        "source": "paper-feed",
        "provider": "paper-provider",
        "observed_at": "2026-08-14T10:00:00+00:00",
        "symbol": "GOLD",
    }
    router = _router(output, market)
    adapter = FakePaperAdapter()
    runtime = _runtime(output, adapter, market)
    proposal = router.handle_update(_update(9, "short DCA 10x 4444~4200 stop 4444 tp 4200"))
    market.update({"price": 4444.0, "observed_at": "2026-08-14T10:01:00+00:00"})
    router.handle_update(_update(10, f"confirm {proposal['proposal']['plan_digest']}"))

    result = runtime.run_once()
    assert result["status"] == "paused"
    assert adapter.submit_calls == []
    assert adapter.cancel_calls == []


def test_dca_explicit_take_profit_terminal_closes_owned_position_and_notifies_result(tmp_path: Path) -> None:
    output = tmp_path / "outputs"
    market = {
        "price": 4300.0,
        "trusted": True,
        "fresh": True,
        "source": "paper-feed",
        "provider": "paper-provider",
        "observed_at": "2026-08-14T10:00:00+00:00",
        "symbol": "GOLD",
    }
    router = _router(output, market)
    adapter = FakePaperAdapter()
    runtime = _runtime(output, adapter, market)
    proposal = router.handle_update(_update(24, "short DCA 10x 4444~4200 stop 4450 tp 4210"))
    router.handle_update(_update(25, f"confirm {proposal['proposal']['plan_digest']}"))
    assert runtime.run_once()["status"] == "active"
    active = ParkStrategyIdentityJournal(output).active_session()
    adapter.positions.append({
        "position_id": "dca-owned-1",
        "trade_id": "dca-trade-1",
        "status": "open",
        "side": "short",
        "remaining_units": 0.1,
        "strategy_session_id": active["strategy_session_id"],
        "strategy_revision_id": active["strategy_revision_id"],
        "plan_digest": proposal["proposal"]["plan_digest"],
    })
    market.update({"price": 4210.0, "observed_at": "2026-08-14T10:01:00+00:00"})

    terminal = runtime.run_once()

    assert terminal["status"] == "paused"
    assert terminal["terminal_reason"] == "take_profit_price"
    assert terminal["positions_preserved"] == 0
    assert any(row.get("message_type") == "park_terminal" and "owned_exits=1" in row.get("text", "") for row in runtime.telegram.outbox_rows())
    assert all("sl" not in command and "tp" not in command for command in adapter.submit_calls[:-1])


def test_confirmed_reverse_flattens_old_owned_exposure_before_starting_new_revision(tmp_path: Path) -> None:
    output = tmp_path / "outputs"
    market = {
        "price": 4300.0,
        "trusted": True,
        "fresh": True,
        "source": "paper-feed",
        "provider": "paper-provider",
        "observed_at": "2026-08-14T10:01:00+00:00",
        "symbol": "GOLD",
    }
    old_digest = "sha256:" + "1" * 64
    new_digest = "sha256:" + "2" * 64
    identity = ParkStrategyIdentityJournal(output)
    identity.start_clean_session(
        observed_at="2026-08-14T10:00:00+00:00",
        plan_digest=old_digest,
        reconciliation_healthy=True,
        strategy_session_id="session-old",
        strategy_revision_id="revision-old",
    )
    plans_path = output / "park_strategy" / "plans.jsonl"
    plans_path.parent.mkdir(parents=True, exist_ok=True)
    old_plan = {
        "event": "plan_proposed",
        "plan_digest": old_digest,
        "strategy_session_id": "session-old",
        "strategy_revision_id": "revision-old",
        "normalized_input": {"strategy_type": "dca", "direction": "short", "upper_price_boundary": 4444.0, "lower_price_boundary": 4200.0, "stop_price": 4450.0, "take_profit_price": 4100.0},
        "risk": {"effective_leverage": 5.0, "theoretical_max_loss": 100.0, "order_count": 1, "per_order_quantity": 1.0},
    }
    new_plan = {
        "event": "plan_proposed",
        "plan_digest": new_digest,
        "strategy_session_id": "session-new",
        "strategy_revision_id": "revision-new",
        "normalized_input": {"strategy_type": "dca", "direction": "long", "upper_price_boundary": 4500.0, "lower_price_boundary": 4200.0, "stop_price": 4100.0, "take_profit_price": 4600.0, "maximum_leverage": 5.0, "order_count": 1},
        "market": {"price": 4300.0, "trusted": True, "fresh": True, "source": "paper-feed", "observed_at": "2026-08-14T10:01:00+00:00"},
        "risk": {"maximum_notional": 50000.0, "effective_leverage": 5.0, "theoretical_max_loss": 2325.58139535, "order_count": 1, "per_order_quantity": 11.627906976744},
    }
    plans_path.write_text("\n".join(json.dumps(row) for row in (old_plan, new_plan)) + "\n", encoding="utf-8")
    confirmations_path = output / "park_strategy" / "confirmations.jsonl"
    confirmations_path.write_text("\n".join(json.dumps({"event": "confirmed", "plan_digest": digest, "strategy_session_id": session, "strategy_revision_id": revision, "execution_authorized": True}) for digest, session, revision in ((old_digest, "session-old", "revision-old"), (new_digest, "session-new", "revision-new"))) + "\n", encoding="utf-8")
    reverse_path = output / "park_strategy" / "reverse_requests.jsonl"
    reverse_path.write_text(json.dumps({
        "schema_version": "park-reverse-request-v1",
        "event": "reverse_request",
        "request_id": "reverse-test",
        "status": "confirmed_pending_transition",
        "old_strategy": {"strategy_session_id": "session-old", "strategy_revision_id": "revision-old", "plan_digest": old_digest},
        "new_strategy_session_id": "session-new",
        "new_strategy_revision_id": "revision-new",
        "new_plan_digest": new_digest,
        "disposition": {"position_action": "flatten", "entry_action": "cancel", "exit_action": "keep"},
    }) + "\n", encoding="utf-8")
    adapter = FakePaperAdapter()
    adapter.orders.append({"order_id": "old-order", "state": "accepted", "side": "sell", "event": "entry", "price": 4300.0, "quantity": 1.0, "strategy_session_id": "session-old", "strategy_revision_id": "revision-old", "plan_digest": old_digest})
    adapter.positions.append({"position_id": "old-position", "trade_id": "old-trade", "status": "open", "side": "short", "remaining_units": 1.0, "entry_price": 4300.0, "strategy_session_id": "session-old", "strategy_revision_id": "revision-old", "plan_digest": old_digest})
    runtime = _runtime(output, adapter, market)

    result = runtime.run_once()

    assert result["status"] == "reverse_transitioned"
    assert identity.active_session()["strategy_session_id"] == "session-new"
    assert adapter.cancel_calls and adapter.cancel_calls[0]["order_ids"] == ["old-order"]
    assert any(command["event"] == "flatten" for command in adapter.submit_calls)
    assert adapter.positions[0]["status"] == "closed"


def test_reverse_rejects_material_market_drift_before_old_mutation() -> None:
    plan = {
        "market": {"price": 4000.0},
        "normalized_input": {"strategy_type": "dca", "direction": "long", "upper_price_boundary": 4500.0, "lower_price_boundary": 4200.0, "stop_price": 4100.0, "take_profit_price": 4600.0, "maximum_leverage": 5.0, "order_count": 1},
        "risk": {"maximum_notional": 50000.0, "theoretical_max_loss": 2325.58139535, "effective_leverage": 5.0, "order_count": 1},
    }

    drift = ParkPaperRuntime._reverse_plan_drift(
        plan,
        market={"price": 4300.0, "trusted": True, "fresh": True, "source": "paper-feed", "observed_at": "2026-08-14T10:01:00+00:00"},
        account={"equity": 10000.0},
    )

    assert drift is not None
    assert "market price drifted" in drift


def test_blocked_reverse_freezes_old_strategy_from_new_entries(tmp_path: Path) -> None:
    output = tmp_path / "outputs"
    identity = ParkStrategyIdentityJournal(output)
    identity.start_clean_session(
        observed_at="2026-08-14T10:00:00+00:00",
        plan_digest="sha256:" + "1" * 64,
        reconciliation_healthy=True,
        strategy_session_id="session-old",
        strategy_revision_id="revision-old",
    )
    reverse_path = output / "park_strategy" / "reverse_requests.jsonl"
    reverse_path.parent.mkdir(parents=True, exist_ok=True)
    reverse_path.write_text(json.dumps({
        "event": "reverse_request",
        "request_id": "reverse-blocked",
        "status": "blocked",
        "blocker_code": "reverse_foreign_exposure",
        "detail": "foreign exposure",
        "old_strategy": {"strategy_session_id": "session-old", "strategy_revision_id": "revision-old", "plan_digest": "sha256:" + "1" * 64},
    }) + "\n", encoding="utf-8")
    adapter = FakePaperAdapter()
    market = {"price": 4300.0, "trusted": True, "fresh": True, "source": "paper-feed", "provider": "paper-provider", "observed_at": "2026-08-14T10:01:00+00:00", "symbol": "GOLD"}
    runtime = _runtime(output, adapter, market)

    result = runtime.run_once()

    assert result["status"] == "blocked"
    assert result["code"] == "reverse_transition_blocked"
    assert adapter.submit_calls == []


def test_reverse_account_wide_gate_sees_foreign_cycle_snapshot(tmp_path: Path) -> None:
    adapter = FakePaperAdapter()
    adapter.output_root = tmp_path / "outputs"
    snapshot_dir = adapter.output_root / "dualtrack" / "nautilus_authoritative" / "snapshots"
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    (snapshot_dir / "other-cycle.json").write_text(json.dumps([{
        "cycle_id": "park-session-other",
        "orders": [{"order_id": "foreign-order", "state": "accepted"}],
        "positions": [],
    }]), encoding="utf-8")
    runtime = _runtime(tmp_path / "runtime", adapter, {"price": 4300.0, "trusted": True, "fresh": True, "source": "paper-feed", "provider": "paper-provider", "observed_at": "2026-08-14T10:00:00+00:00"})

    foreign = runtime._account_wide_foreign_exposure("session-old", "revision-old", "sha256:" + "1" * 64)

    assert foreign == [{"cycle_id": "park-session-other", "kind": "order", "id": "foreign-order"}]


def test_dca_terminal_retry_keeps_first_persisted_trigger_after_reconciliation_failure(tmp_path: Path) -> None:
    class DriftOnceAdapter(FakePaperAdapter):
        def __init__(self) -> None:
            super().__init__()
            self.reconcile_calls = 0
            self.fail_terminal = False

        def reconcile(self, cycle_id: str) -> dict:
            self.reconcile_calls += 1
            if self.fail_terminal and self.reconcile_calls == 5:
                return {"status": "drift", "issues": ["terminal_test_drift"], "cycle_id": cycle_id}
            return super().reconcile(cycle_id)

    output = tmp_path / "outputs"
    market = {
        "price": 4300.0,
        "trusted": True,
        "fresh": True,
        "source": "paper-feed",
        "provider": "paper-provider",
        "observed_at": "2026-08-14T10:00:00+00:00",
        "symbol": "GOLD",
    }
    router = _router(output, market)
    adapter = DriftOnceAdapter()
    runtime = _runtime(output, adapter, market)
    proposal = router.handle_update(_update(26, "short DCA 10x 4444~4200 stop 4444 tp 4200"))
    router.handle_update(_update(27, f"confirm {proposal['proposal']['plan_digest']}"))
    assert runtime.run_once()["status"] == "active"
    adapter.fail_terminal = True
    market.update({"price": 4444.0, "observed_at": "2026-08-14T10:01:00+00:00"})

    blocked = runtime.run_once()

    assert blocked["status"] == "blocked"
    assert blocked["code"] == "terminal_reconciliation_blocked"
    lifecycle = [
        json.loads(line)
        for line in (output / "park_strategy" / "lifecycle.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert lifecycle[-1]["event"] == "terminal_action_plan"
    assert lifecycle[-1]["trigger"] == "stop_price"

    adapter.fail_terminal = False
    market.update({"price": 4300.0, "observed_at": "2026-08-14T10:02:00+00:00"})
    recovered = runtime.run_once()

    assert recovered["status"] == "paused"
    assert recovered["terminal_reason"] == "stop_price"
    assert lifecycle[-1]["trigger"] == "stop_price"


def test_direct_adapter_factory_is_default_deny_without_park_release_or_runtime(tmp_path: Path) -> None:
    try:
        build_park_authoritative_adapter(tmp_path, config={**_config(), "feature_enabled": False}, environ={})
    except Exception as exc:
        assert getattr(exc, "code", "") == "park_track_disabled"
    else:  # pragma: no cover - defensive assertion
        raise AssertionError("disabled Park config must not build an adapter")


@pytest.mark.parametrize(
    ("before", "after", "record_window_id"),
    (
        ("2026-08-14T08:59:00+08:00", "2026-08-14T09:01:00+08:00", "2026-08-13_NIGHT"),
        ("2026-08-14T20:59:00+08:00", "2026-08-14T21:01:00+08:00", "2026-08-14_DAY"),
    ),
)
def test_recording_window_boundary_only_closes_package_and_keeps_strategy_identity(
    tmp_path: Path,
    before: str,
    after: str,
    record_window_id: str,
) -> None:
    output = tmp_path / "outputs"
    market = {
        "price": 4300.0,
        "trusted": True,
        "fresh": True,
        "source": "paper-feed",
        "provider": "paper-provider",
        "observed_at": before,
        "symbol": "GOLD",
    }
    now_ref = {"value": before}
    router = ParkTelegramRouter(
        output,
        park_user_id="park-user",
        chat_id="park-chat",
        market_reader=lambda: dict(market),
        account_reader=lambda _root, _cycle: {
            "equity": 10000.0,
            "reconciliation_healthy": True,
            "open_positions": 0,
            "open_or_accepted_orders": 0,
            "unresolved_runtime": False,
            "pending_terminal_actions": False,
        },
        now=lambda: now_ref["value"],
        cycle_id_provider=lambda _now: "2026-08-14_DAY",
    )
    adapter = FakePaperAdapter()
    runtime = ParkPaperRuntime(
        output,
        adapter=adapter,
        park_user_id="park-user",
        chat_id="park-chat",
        config=_config(),
        market_reader=lambda: dict(market),
        now=lambda: now_ref["value"],
        safety_evidence_reader=_evidence,
    )
    proposal = router.handle_update(_update(7, "short DCA 10x 4444~4200 stop 4444 tp 4200"))
    router.handle_update(_update(8, f"confirm {proposal['proposal']['plan_digest']}"))
    assert runtime.run_once()["status"] == "active"
    active_before = ParkStrategyIdentityJournal(output).active_session()

    now_ref["value"] = after
    market.update({"observed_at": now_ref["value"]})
    after_window = runtime.run_once()
    assert after_window["status"] == "active"
    assert ParkStrategyIdentityJournal(output).active_session()["strategy_session_id"] == active_before["strategy_session_id"]
    active_after = ParkStrategyIdentityJournal(output).active_session()
    assert active_after["strategy_revision_id"] == active_before["strategy_revision_id"]
    assert active_after["plan_digest"] == active_before["plan_digest"]
    assert adapter.cancel_calls == []
    assert adapter.submit_calls and len(adapter.submit_calls) == 1
    execution_events = [
        json.loads(line)
        for line in (output / "park_strategy" / "executions.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert not any(
        row.get("event") in {"cancel", "flatten", "stop", "reverse", "handoff", "terminal_paused"}
        for row in execution_events
    )
    assert any(row.get("record_window_id") == record_window_id for row in runtime.recording.packages())


def test_recording_package_failure_blocks_evidence_without_execution_mutation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    output = tmp_path / "outputs"
    market = {
        "price": 4300.0,
        "trusted": True,
        "fresh": True,
        "source": "paper-feed",
        "provider": "paper-provider",
        "observed_at": "2026-08-14T12:59:00+00:00",
        "symbol": "GOLD",
    }
    now_ref = {"value": "2026-08-14T12:59:00+00:00"}
    router = ParkTelegramRouter(
        output,
        park_user_id="park-user",
        chat_id="park-chat",
        market_reader=lambda: dict(market),
        account_reader=lambda _root, _cycle: {
            "equity": 10000.0,
            "reconciliation_healthy": True,
            "open_positions": 0,
            "open_or_accepted_orders": 0,
            "unresolved_runtime": False,
            "pending_terminal_actions": False,
        },
        now=lambda: now_ref["value"],
        cycle_id_provider=lambda _now: "2026-08-14_DAY",
    )
    adapter = FakePaperAdapter()
    runtime = ParkPaperRuntime(
        output,
        adapter=adapter,
        park_user_id="park-user",
        chat_id="park-chat",
        config=_config(),
        market_reader=lambda: dict(market),
        now=lambda: now_ref["value"],
        safety_evidence_reader=_evidence,
    )
    proposal = router.handle_update(_update(17, "short DCA 10x 4444~4200 stop 4444 tp 4200"))
    router.handle_update(_update(18, f"confirm {proposal['proposal']['plan_digest']}"))
    assert runtime.run_once()["status"] == "active"
    active_before = ParkStrategyIdentityJournal(output).active_session()
    submit_count = len(adapter.submit_calls)
    cancel_count = len(adapter.cancel_calls)

    def fail_close(**_kwargs):
        raise ParkRecordingError("package_write_failed", "test package failure")

    monkeypatch.setattr(runtime.recording, "close_package", fail_close)
    now_ref["value"] = "2026-08-14T13:01:00+00:00"
    market["observed_at"] = now_ref["value"]

    result = runtime.run_once()

    assert result["status"] == "active"
    assert result["next_action"] == "retry_recording_package"
    assert result["recording_blocker"]["code"] == "recording_package_blocked"
    assert len(adapter.submit_calls) == submit_count
    assert len(adapter.cancel_calls) == cancel_count
    assert ParkStrategyIdentityJournal(output).active_session() == active_before
    blockers = (output / "park_strategy" / "runtime_blockers.jsonl").read_text(encoding="utf-8")
    assert "recording_package_blocked" in blockers
    assert any(row.get("message_type") == "park_blocker" for row in runtime.telegram.outbox_rows())


def test_recording_facts_failure_gates_new_entries_until_recovered(
    tmp_path: Path,
    monkeypatch,
) -> None:
    output = tmp_path / "outputs"
    market = {
        "price": 4300.0,
        "trusted": True,
        "fresh": True,
        "source": "paper-feed",
        "provider": "paper-provider",
        "observed_at": "2026-08-14T10:00:00+00:00",
        "symbol": "GOLD",
    }
    router = _router(output, market)
    adapter = FakePaperAdapter()
    runtime = _runtime(output, adapter, market)
    proposal = router.handle_update(_update(21, "short DCA 10x 4444~4200 stop 4444 tp 4200"))
    router.handle_update(_update(22, f"confirm {proposal['proposal']['plan_digest']}"))
    original_record_window_facts = runtime._record_window_facts

    def fail_facts(*_args, **_kwargs):
        raise ParkRecordingError("facts_write_failed", "test facts failure")

    monkeypatch.setattr(runtime, "_record_window_facts", fail_facts)
    first = runtime.run_once()
    assert first["status"] == "active"
    assert first["recording_blocker"]["code"] == "recording_facts_blocked"
    submitted_count = len(adapter.submit_calls)

    monkeypatch.setattr(runtime, "_record_window_facts", original_record_window_facts)
    second = runtime.run_once()
    assert second["status"] == "active"
    assert second["next_action"] == "retry_recording_package"
    assert second["submitted"] == []
    assert len(adapter.submit_calls) == submitted_count

    third = runtime.run_once()
    assert third["recording_failures"] == []
    assert third["next_action"] == "continue_trusted_fresh_ticks"


def test_runtime_retries_blocked_recording_after_late_event_without_mutation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    output = tmp_path / "outputs"
    market = {
        "price": 4300.0,
        "trusted": True,
        "fresh": True,
        "source": "paper-feed",
        "provider": "paper-provider",
        "observed_at": "2026-08-14T12:59:00+00:00",
        "symbol": "GOLD",
    }
    now_ref = {"value": "2026-08-14T12:59:00+00:00"}
    router = ParkTelegramRouter(
        output,
        park_user_id="park-user",
        chat_id="park-chat",
        market_reader=lambda: dict(market),
        account_reader=lambda _root, _cycle: {
            "equity": 10000.0,
            "reconciliation_healthy": True,
            "open_positions": 0,
            "open_or_accepted_orders": 0,
            "unresolved_runtime": False,
            "pending_terminal_actions": False,
        },
        now=lambda: now_ref["value"],
        cycle_id_provider=lambda _now: "2026-08-14_DAY",
    )
    adapter = FakePaperAdapter()
    runtime = ParkPaperRuntime(
        output,
        adapter=adapter,
        park_user_id="park-user",
        chat_id="park-chat",
        config=_config(),
        market_reader=lambda: dict(market),
        now=lambda: now_ref["value"],
        safety_evidence_reader=_evidence,
    )
    proposal = router.handle_update(_update(19, "short DCA 10x 4444~4200 stop 4444 tp 4200"))
    router.handle_update(_update(20, f"confirm {proposal['proposal']['plan_digest']}"))

    original_record_event = runtime.recording.record_event
    skipped = {"fills": False}

    def omit_first_fill(**kwargs):
        if kwargs.get("category") == "fills" and not skipped["fills"]:
            skipped["fills"] = True
            return {"skipped": True}
        return original_record_event(**kwargs)

    monkeypatch.setattr(runtime.recording, "record_event", omit_first_fill)
    assert runtime.run_once()["status"] == "active"

    now_ref["value"] = "2026-08-14T13:01:00+00:00"
    market["observed_at"] = now_ref["value"]
    blocked = runtime.run_once()
    assert blocked["status"] == "active"
    assert blocked["next_action"] == "retry_recording_package"
    assert blocked["recording_failures"][0]["error_type"] == "recording_incomplete"
    assert runtime.recording.packages()[-1]["status"] == "blocked_incomplete"

    monkeypatch.setattr(runtime.recording, "record_event", original_record_event)
    amendment = runtime.recording.amend_late_event(
        record_window_id="2026-08-14_DAY",
        strategy_session_id=proposal["proposal"]["strategy_session_id"],
        strategy_revision_id=proposal["proposal"]["strategy_revision_id"],
        category="fills",
        event_type="late_fill",
        source="test",
        occurred_at="2026-08-14T13:01:30+00:00",
        payload={"fill_id": "late-1"},
    )
    assert amendment["status"] == "complete"

    now_ref["value"] = "2026-08-14T13:02:00+00:00"
    market["observed_at"] = now_ref["value"]
    recovered = runtime.run_once()
    assert recovered["status"] == "active"
    assert recovered["recording_failures"] == []
    assert runtime.recording.packages()[-1]["status"] == "complete"
    assert runtime.recording.packages()[-1]["review_status"] == "complete"
    assert len(adapter.submit_calls) == 1


def test_multi_session_recording_close_keeps_new_active_identity_open(tmp_path: Path) -> None:
    output = tmp_path / "outputs"
    market = {
        "price": 4300.0,
        "trusted": True,
        "fresh": True,
        "source": "paper-feed",
        "provider": "paper-provider",
        "observed_at": "2026-08-14T10:00:00+00:00",
        "symbol": "GOLD",
    }
    runtime = _runtime(output, FakePaperAdapter(), market)
    old = runtime.identity.start_clean_session(
        observed_at="2026-08-14T10:00:00+00:00",
        plan_digest="sha256:" + "1" * 64,
        reconciliation_healthy=True,
        strategy_session_id="session-old",
        strategy_revision_id="revision-old",
    )
    runtime.identity.close_session(
        strategy_session_id=old["strategy_session_id"],
        strategy_revision_id=old["strategy_revision_id"],
        observed_at="2026-08-14T11:00:00+00:00",
        reason="boundary",
    )
    new = runtime.identity.start_clean_session(
        observed_at="2026-08-14T11:30:00+00:00",
        plan_digest="sha256:" + "2" * 64,
        reconciliation_healthy=True,
        strategy_session_id="session-new",
        strategy_revision_id="revision-new",
    )
    for session, revision in (
        (old["strategy_session_id"], old["strategy_revision_id"]),
        (new["strategy_session_id"], new["strategy_revision_id"]),
    ):
        runtime.recording.start_window(
            record_window_id="2026-08-14_DAY",
            strategy_session_id=session,
            strategy_revision_id=revision,
            starts_at="2026-08-14T01:00:00Z",
            ends_at="2026-08-14T13:00:00Z",
        )
        for category in REQUIRED_CATEGORIES:
            runtime.recording.record_event(
                record_window_id="2026-08-14_DAY",
                strategy_session_id=session,
                strategy_revision_id=revision,
                category=category,
                event_type=f"{category}_observed",
                source="test",
                occurred_at="2026-08-14T12:30:00Z",
                payload={"open_count": 2} if category == "positions" else {},
            )

    runtime._close_due_recording_packages("2026-08-14T13:01:00+00:00")

    package = runtime.recording.packages()[0]
    assert package["status"] == "complete"
    assert package["strategy_open"] is True
    assert package["positions_open"] == 2
    assert package["strategy_session_id"] == ""
    assert package["strategy_session_ids"] == ["session-new", "session-old"]
    assert package["strategy_revision_ids"] == ["revision-new", "revision-old"]
    assert package["execution_mutations"] == []
