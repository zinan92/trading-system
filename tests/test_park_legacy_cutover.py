from __future__ import annotations

from pathlib import Path

from services.park_codex_intent_parser import deterministic_legacy_clean_slate_candidate
from services.park_legacy_cutover import (
    ParkLegacyCutoverLedger,
    load_effective_park_config,
)
from services.park_legacy_cutover_runtime import (
    inspect_legacy_runner_quarantine,
    run_legacy_cutover_once,
)
from services.park_paper_mutation_gate import (
    _mint_park_paper_capability,
    _new_park_paper_mutation_gate,
)
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


def _facts(orders: list[dict], positions: list[dict] | None = None) -> dict:
    return {
        "equity": 10000.0,
        "reconciliation_healthy": True,
        "open_positions": len(positions or []),
        "open_or_accepted_orders": len(orders),
        "unresolved_runtime": False,
        "pending_terminal_actions": False,
        "snapshot": {
            "account_wide_legacy_exposure": {
                "orders": orders,
                "positions": positions or [],
                "ownership": "legacy_cycle_or_unknown",
            }
        },
        "reconciliation": {"status": "ok", "issues": []},
    }


def test_deterministic_legacy_phrase_is_provider_independent() -> None:
    candidate = deterministic_legacy_clean_slate_candidate(
        "取消这19个旧挂单，确认 clean slate，启用 Park Paper"
    )
    assert candidate == {
        "schema_version": "park-codex-intent-v1",
        "intent": "legacy_clean_slate_cutover",
        "expected_order_count": 19,
        "source_text": "取消这19个旧挂单，确认 clean slate，启用 Park Paper",
        "explicit_confirmation": True,
    }


def test_router_confirms_explicit_legacy_phrase_without_codex(tmp_path: Path) -> None:
    orders = [
        {
            "order_id": "old-1",
            "cycle_id": "2026-08-18_DAY",
            "strategy_plan_id": "legacy-plan",
            "state": "accepted",
            "side": "sell",
            "price": 4400.0,
            "quantity": 1.0,
        }
    ]
    router = ParkTelegramRouter(
        tmp_path,
        park_user_id="park-user",
        chat_id="park-chat",
        account_reader=lambda *_args, **_kwargs: _facts(orders),
        market_reader=lambda: {"price": 4300.0, "trusted": True, "fresh": True, "source": "paper"},
        now=lambda: "2026-08-18T03:00:00+00:00",
    )
    result = router.handle_update(_update(1, "取消这1个旧挂单，确认 clean slate，启用 Park Paper"))
    assert result["status"] == "legacy_cutover_confirmed"
    assert result["decision"]["execution_authorized"] is True
    assert router.legacy_cutover.confirmed_pending()[0]["orders"][0]["order_id"] == "old-1"


def test_router_recovers_old_ingested_message_after_provider_misclassification(tmp_path: Path) -> None:
    orders = [
        {
            "order_id": "old-1",
            "cycle_id": "2026-08-18_DAY",
            "strategy_plan_id": "legacy-plan",
            "state": "accepted",
            "side": "sell",
            "price": 4400.0,
            "quantity": 1.0,
        }
    ]
    router = ParkTelegramRouter(
        tmp_path,
        park_user_id="park-user",
        chat_id="park-chat",
        account_reader=lambda *_args, **_kwargs: _facts(orders),
        now=lambda: "2026-08-18T03:00:00+00:00",
    )
    update = _update(9, "取消这1个旧挂单，确认 clean slate，启用 Park Paper")
    router.telegram.ingest_update(update)
    router._remember_result(
        9,
        {"status": "blocked", "code": "missing_direction"},
        update_digest="sha256:previous",
    )
    recovered = router.recover_pending_legacy_cutovers()
    assert recovered[0]["status"] == "legacy_cutover_confirmed"
    assert router.legacy_cutover.confirmed_pending()[0]["proposal_digest"].startswith("sha256:")


def test_router_rebuilds_expired_confirmed_cutover_from_same_inbox_message(
    tmp_path: Path, monkeypatch
) -> None:
    orders = [
        {
            "order_id": "old-1",
            "cycle_id": "2026-08-18_DAY",
            "strategy_plan_id": "legacy-plan",
            "state": "accepted",
            "side": "sell",
            "price": 4400.0,
            "quantity": 1.0,
        }
    ]
    clock = {"value": 1000.0}
    monkeypatch.setattr("services.park_legacy_cutover.time.time", lambda: clock["value"])
    router = ParkTelegramRouter(
        tmp_path,
        park_user_id="park-user",
        chat_id="park-chat",
        account_reader=lambda *_args, **_kwargs: _facts(orders),
        now=lambda: "2026-08-18T03:00:00+00:00",
    )
    update = _update(10, "取消这1个旧挂单，确认 clean slate，启用 Park Paper")
    router.telegram.ingest_update(update)
    router._remember_result(10, {"status": "blocked", "code": "missing_direction"}, update_digest="sha256:old")
    first = router.recover_pending_legacy_cutovers()
    assert first[0]["status"] == "legacy_cutover_confirmed"
    first_proposal = first[0]["proposal"]["proposal_id"]
    clock["value"] = 2000.0

    recovered = router.recover_pending_legacy_cutovers()

    assert recovered[0]["status"] == "legacy_cutover_confirmed"
    assert recovered[0]["proposal"]["proposal_id"] == first_proposal
    assert router.legacy_cutover.confirmed_pending()[0]["expires_at"] > clock["value"]


def test_legacy_cutover_requires_exact_set_and_never_flattens(tmp_path: Path, monkeypatch) -> None:
    orders = [
        {
            "order_id": "old-1",
            "cycle_id": "2026-08-18_DAY",
            "strategy_plan_id": "legacy-plan",
            "state": "accepted",
            "side": "sell",
            "price": 4400.0,
            "quantity": 1.0,
        }
    ]
    ledger = ParkLegacyCutoverLedger(tmp_path, park_user_id="park-user", chat_id="park-chat")
    proposal = ledger.create_proposal(
        update_id=1,
        source_text_digest="sha256:source",
        expected_order_count=1,
        orders=orders,
        positions=[],
        reconciliation={"status": "ok", "issues": []},
    )
    ledger.confirm(proposal, update_id=2, mode="explicit_cutover_digest")

    class Adapter:
        name = "nautilus_paper"
        output_root = tmp_path

        def __init__(self) -> None:
            self.cancel_calls: list[dict] = []

        def cancel_orders(self, cycle_id, **kwargs):
            self.cancel_calls.append({"cycle_id": cycle_id, **kwargs})
            return {"status": "cancelled", "cancelled_order_ids": kwargs["order_ids"], "cancelled_order_count": 1}

    adapter = Adapter()
    class Binding:
        def __init__(self) -> None:
            self.adapter = adapter
            self.authorized = []
        def authorize(self, cap) -> None:
            self.authorized.append(cap)
        def revoke(self) -> None:
            pass
    binding = Binding()
    monkeypatch.setattr(
        "services.park_paper_runtime.build_park_authoritative_adapter",
        lambda *_args, **_kwargs: binding,
    )
    monkeypatch.setattr(
        "services.park_legacy_cutover_runtime.build_park_safety_evidence",
        lambda *_args, **_kwargs: {
            "status": "pass",
            "release_sha": "sha",
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
        },
    )
    monkeypatch.setattr(
        "services.park_legacy_cutover_runtime.evaluate_park_cutover",
        lambda *_args, **_kwargs: {"status": "pass", "blockers": []},
    )
    state = {"orders": orders}

    def account_reader(*_args, **_kwargs):
        current = state["orders"]
        state["orders"] = []
        return _facts(current)

    result = run_legacy_cutover_once(
        tmp_path,
        config={
            "feature_enabled": False,
            "execution_track_count": 1,
            "runtime_mode": "paper_only",
            "control_plane": "telegram",
            "autonomous": False,
            "shadow_mutation": False,
            "feishu_control": False,
            "execution_engine": {"real_money_eligible": False},
            "supervisor_execution_profile": "fail_closed",
        },
        park_user_id="park-user",
        chat_id="park-chat",
        repo_root=tmp_path,
        account_reader=account_reader,
        market_reader=lambda: {"price": 4300.0, "trusted": True, "fresh": True, "source": "paper"},
        now=lambda: "2026-08-18T03:00:00+00:00",
    )
    assert result["status"] == "completed"
    assert adapter.cancel_calls == [
        {
            "cycle_id": "2026-08-18_DAY",
            "order_ids": ["old-1"],
            "ts": "2026-08-18T03:00:00+00:00",
            "reason": "park_legacy_clean_slate",
            "legacy_cutover_id": proposal["proposal_id"],
        }
    ]
    assert load_effective_park_config(tmp_path, {"feature_enabled": False})["feature_enabled"] is True


def test_legacy_mutation_scope_cannot_submit_or_use_normal_operation() -> None:
    gate = _new_park_paper_mutation_gate()
    cap = _mint_park_paper_capability(
        {
            "issuer": "ParkLegacyCutoverRuntime.run_once",
            "cycle_id": "legacy-cutover",
            "strategy_session_id": "legacy-session",
            "strategy_revision_id": "legacy-revision",
            "plan_digest": "legacy-plan",
            "park_confirmation_digest": "sha256:" + "a" * 64,
            "cutover_status": "pass",
            "legacy_cutover_id": "legacy-proposal",
            "operation": "cancel_legacy_orders",
        }
    )
    gate._activate(cap)
    gate.require(cycle_id="old-cycle", operation="cancel_legacy_orders", legacy_cutover_id="legacy-proposal")
    try:
        gate.require(cycle_id="old-cycle", command={"event": "entry"})
    except RuntimeError as exc:
        assert "legacy cutover" in str(exc)
    else:
        raise AssertionError("legacy cutover capability must reject normal mutations")


def test_legacy_runner_quarantine_requires_park_control_only(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        "services.scheduler_ownership.SchedulerOwnershipGuard.verify",
        lambda _self: {"ok": True, "owner_id": "cloud-primary"},
    )
    unit = tmp_path / "gridmind-live-tick.service"
    unit.write_text("ExecStart=/opt/app -m pipelines.park_control --timeout-seconds 20\n", encoding="utf-8")
    assert inspect_legacy_runner_quarantine(tmp_path, unit_path=unit)["ok"] is True
    unit.write_text("ExecStart=/opt/app -m pipelines.dualtrack_cycle_runner --event live-tick\n", encoding="utf-8")
    assert inspect_legacy_runner_quarantine(tmp_path, unit_path=unit)["ok"] is False
