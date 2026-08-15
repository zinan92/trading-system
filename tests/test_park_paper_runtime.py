from __future__ import annotations

import copy
from pathlib import Path

from services.park_paper_runtime import ParkPaperRuntime, build_park_authoritative_adapter
from services.park_recording_track import REQUIRED_CATEGORIES
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

    proposal = router.handle_update(_update(1, "做空 DCA，最多 10 倍杠杆，价格区间是 4444~4200"))
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
    proposal = router.handle_update(_update(5, "short DCA 10x 4444~4200"))
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
    assert router.handle_update(_update(6, "short DCA 10x 4444~4200"))["status"] == "proposal_created"

    clock["value"] = 2000.0
    blocked = runtime.run_once()

    assert blocked["status"] == "blocked"
    assert blocked["code"] == "confirmation_expired_requires_clean_slate"
    assert ParkStrategyIdentityJournal(output).active_session() is not None
    assert adapter.submit_calls == []


def test_confirmed_neutral_grid_submits_both_owned_legs_and_preserves_boundary_positions(tmp_path: Path) -> None:
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
    assert all("sl" in command and "tp" in command for command in adapter.submit_calls)
    assert sum(command["price"] * command["quantity"] for command in adapter.submit_calls) <= proposal["plan"]["risk"]["maximum_notional"]
    assert all(
        command["sl"] < command["price"] < command["tp"]
        if command["side"] == "buy"
        else command["tp"] < command["price"] < command["sl"]
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
    assert paused["positions_preserved"] == 1
    assert len(adapter.cancel_calls) == 1
    assert not any(command["event"] in {"exit", "stop", "target", "flatten"} for command in adapter.submit_calls)


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
    proposal = router.handle_update(_update(3, "short DCA 10x 4444~4200"))
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
    assert paused["terminal_reason"] == "upper_boundary_invalidated"
    assert len(adapter.cancel_calls) == 1
    assert len(adapter.submit_calls) == 2
    assert adapter.submit_calls[-1]["event"] == "target"
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
    proposal = router.handle_update(_update(5, "short DCA 10x 4444~4200"))
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
    proposal = router.handle_update(_update(9, "short DCA 10x 4444~4200"))
    market.update({"price": 4444.0, "observed_at": "2026-08-14T10:01:00+00:00"})
    router.handle_update(_update(10, f"confirm {proposal['proposal']['plan_digest']}"))

    result = runtime.run_once()
    assert result["status"] == "paused"
    assert adapter.submit_calls == []
    assert adapter.cancel_calls == []


def test_direct_adapter_factory_is_default_deny_without_park_release_or_runtime(tmp_path: Path) -> None:
    try:
        build_park_authoritative_adapter(tmp_path, config={**_config(), "feature_enabled": False}, environ={})
    except Exception as exc:
        assert getattr(exc, "code", "") == "park_track_disabled"
    else:  # pragma: no cover - defensive assertion
        raise AssertionError("disabled Park config must not build an adapter")


def test_recording_window_boundary_only_closes_package_and_keeps_strategy_identity(tmp_path: Path) -> None:
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
    proposal = router.handle_update(_update(7, "short DCA 10x 4444~4200"))
    router.handle_update(_update(8, f"confirm {proposal['proposal']['plan_digest']}"))
    assert runtime.run_once()["status"] == "active"
    active_before = ParkStrategyIdentityJournal(output).active_session()

    now_ref["value"] = "2026-08-14T13:01:00+00:00"
    market.update({"observed_at": now_ref["value"]})
    after_window = runtime.run_once()
    assert after_window["status"] == "active"
    assert ParkStrategyIdentityJournal(output).active_session()["strategy_session_id"] == active_before["strategy_session_id"]
    assert adapter.cancel_calls == []
    assert adapter.submit_calls and len(adapter.submit_calls) == 1
    assert any(row.get("record_window_id") == "2026-08-14_DAY" for row in runtime.recording.packages())


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
