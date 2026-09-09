"""Run one Telegram ingress + Park Paper execution pass.

The command is suitable for a short systemd/launchd timer.  It never calls
the legacy cycle runner, autonomous supervisor, Shadow wrapper, Feishu, or a
live adapter.  Missing release evidence or secrets produces a typed blocked
receipt rather than a best-effort start.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from services.journal_store import load_json
from services.park_ai_provider_gateway import ParkAiProviderGateway
from services.park_legacy_cutover import load_effective_park_config
from services.park_legacy_cutover_runtime import run_legacy_cutover_once
from services.park_paper_runtime import (
    ParkPaperRuntime,
    ParkPaperRuntimeError,
    build_park_authoritative_adapter,
    prepare_park_paper_config,
)
from services.park_safety_evidence import build_park_safety_evidence
from services.broker_port import BrokerOrderRequest
from services.hyperliquid_testnet_market_reader import HyperliquidTestnetMarketReader
from services.hyperliquid_testnet_runtime import (
    HyperliquidTestnetRuntimeConfig,
    TESTNET_PROFILE,
    _park_plan_to_lifecycle_plan,
    build_park_account_reader,
    build_testnet_start_handler,
)
from services.park_telegram_runtime import (
    ParkTelegramRouter,
    ParkTelegramRuntimeError,
    ParkTelegramWorker,
)
from services.scheduler_ownership import SchedulerOwnershipGuard
from services.testnet_automation_coordinator import TestnetAutomationCoordinator
from services.testnet_scheduler import TestnetScheduler
from services.testnet_plan_builder import build_plan
from pipelines.testnet_proof_driver import ProofDriverError, read_coherent_market
from services.telegram_bot_transport import (
    TelegramBotTransport,
    TelegramBotTransportError,
)


class OrderIdentityHydrationError(RuntimeError):
    """One active lifecycle row cannot restore its durable broker identity."""


def _latest(path: Path) -> dict[str, Any]:
    rows = load_json(path)
    return dict(rows[-1]) if rows and isinstance(rows[-1], dict) else {}


def _load_testnet_plan(output_root: Path, plan_digest: str) -> dict[str, Any] | None:
    """Read the matching immutable plan event from the JSON Lines journal."""
    path = Path(output_root) / "park_strategy" / "plans.jsonl"
    if not path.exists():
        return None
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            row = json.loads(line)
            if isinstance(row, Mapping):
                rows.append(row)
    return next(
        (
            dict(row)
            for row in reversed(rows)
            if str(row.get("plan_digest") or "") == str(plan_digest or "")
        ),
        None,
    )


def _load_dashboard_plan(output_root: Path, activation_id: str, plan_digest: str) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """Load and digest-bind the Dashboard preview/confirmation pair."""
    try:
        previews = load_json(Path(output_root) / "dashboard_control_plane" / "previews.json")
        confirmations = load_json(Path(output_root) / "dashboard_control_plane" / "confirmations.json")
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return None
    preview_rows = previews if isinstance(previews, list) else [previews]
    confirmation_rows = confirmations if isinstance(confirmations, list) else [confirmations]
    confirmed = [row for row in confirmation_rows if isinstance(row, Mapping)
                 and str(row.get("activation_id") or "") == activation_id
                 and row.get("status") == "confirmed"
                 and str(row.get("preview_digest") or row.get("plan_digest") or "") == plan_digest]
    if not confirmed:
        return None
    confirmation = dict(confirmed[-1])
    matching = [row for row in preview_rows if isinstance(row, Mapping)
                and str(row.get("preview_digest") or "") == plan_digest]
    if not matching:
        return None
    return dict(matching[-1]), confirmation


def hydrate_order_identities(broker: object, state: Mapping[str, Any]) -> None:
    """Restore active lifecycle order identities into a fresh broker binding."""
    recovery_states = {
        "accepted": "resting",
        "cancel_pending": "cancel_pending",
        "partial": "partially_filled",
        "partially_filled": "partially_filled",
    }
    cycle_id = str(state.get("cycle_id") or "").strip()
    if not cycle_id:
        raise OrderIdentityHydrationError("lifecycle:cycle_id_missing")
    for index, row in enumerate(state.get("orders") or []):
        if not isinstance(row, Mapping):
            raise OrderIdentityHydrationError(f"order[{index}]:row_invalid")
        recovered_state = recovery_states.get(str(row.get("state") or "").strip().lower())
        if recovered_state is None:
            continue
        broker_order_id = str(row.get("broker_order_id") or "").strip()
        if not broker_order_id:
            raise OrderIdentityHydrationError(f"order[{index}]:broker_order_id_missing")
        if not str(row.get("client_order_id") or "").strip():
            raise OrderIdentityHydrationError(f"order[{index}]:client_order_id_missing")
        if not str(row.get("ticket_id") or "").strip():
            raise OrderIdentityHydrationError(f"order[{index}]:ticket_id_missing")
        try:
            broker.recover(
                BrokerOrderRequest(run_date=cycle_id, ticket=dict(row)),
                broker_order_id=broker_order_id,
                state=recovered_state,
            )
        except Exception as exc:  # noqa: BLE001 - caller converts this to a typed tick blocker.
            raise OrderIdentityHydrationError(
                f"order[{index}]:recover_{type(exc).__name__}"
            ) from exc


def _load_testnet_lifecycle_state(
    output_root: Path,
    plan: Mapping[str, Any],
    *,
    strategy_family: str,
) -> dict[str, Any]:
    plan_id = str(plan.get("strategy_plan_id") or "").strip()
    if not plan_id:
        raise OrderIdentityHydrationError("lifecycle:strategy_plan_id_missing")
    family = str(strategy_family or "").strip().lower()
    if family not in {"grid", "dca"}:
        raise OrderIdentityHydrationError("lifecycle:strategy_family_invalid")
    path = Path(output_root) / "dualtrack" / f"{family}_testnet_lifecycle" / f"{plan_id}.json"
    try:
        rows = load_json(path)
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise OrderIdentityHydrationError("lifecycle:state_unreadable") from exc
    if not isinstance(rows, list) or not rows or not isinstance(rows[-1], Mapping):
        raise OrderIdentityHydrationError("lifecycle:state_missing")
    state = dict(rows[-1])
    if str(state.get("strategy_plan_id") or "").strip() != plan_id:
        raise OrderIdentityHydrationError("lifecycle:strategy_plan_id_mismatch")
    plan_digest = str(plan.get("plan_digest") or "").strip()
    if plan_digest and str(state.get("plan_digest") or "").strip() != plan_digest:
        raise OrderIdentityHydrationError("lifecycle:plan_digest_mismatch")
    return state


def run_testnet_control_tick(output_root: Path, *, owner_id: str = "local-mac") -> dict[str, Any]:
    """Run the local Testnet scheduler heartbeat in the Park control pass.

    The scheduler is deliberately dormant until its local ownership receipt
    and activation already exist. A missing/blocked Testnet session therefore
    cannot block the independent Paper pass.
    """
    coordinator = TestnetAutomationCoordinator(output_root)
    scheduler = TestnetScheduler(output_root, coordinator, owner_id=owner_id, runtime_mode="local")
    guard = scheduler.guard.verify()
    if not guard.get("ok"):
        return {"status": "not_applicable", "reason": guard.get("blocker"), "paper_only": True}
    coordinator_status = coordinator.status()
    coordinator_activation_id = str(coordinator_status.get("activation_id") or "")
    coordinator_running = str(coordinator_status.get("status") or "") in {
        "grid_running", "dca_running", "grid_blocked", "dca_blocked",
    }
    if coordinator_running and scheduler.can_reattach(coordinator_activation_id):
        scheduler.attach(coordinator_activation_id)
    scheduler_status = scheduler.status()
    scheduler_activation_id = str(scheduler_status.get("activation_id") or "")
    if (
        coordinator_running
        and coordinator_activation_id
        and scheduler_activation_id
        and coordinator_activation_id != scheduler_activation_id
    ):
        return {
            "status": "blocked",
            "reason": "testnet_scheduler_active_session_conflict",
            "blocker": "testnet_scheduler_active_session_conflict",
            "scheduler_activation_id": scheduler_activation_id,
            "coordinator_activation_id": coordinator_activation_id,
            "paper_only": True,
        }
    if scheduler_status.get("status") not in {"active", "reconcile_required"}:
        return {"status": "not_applicable", "reason": "testnet_scheduler_not_active", "paper_only": True}
    tick_id = "park-control:" + datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    callbacks = None
    if str(coordinator_status.get("status") or "") in {"grid_running", "dca_running", "grid_blocked", "dca_blocked"}:
        try:
            callbacks = _build_testnet_tick_callbacks(output_root, coordinator_status)
        except Exception as exc:  # noqa: BLE001 - scheduler records the typed blocker.
            # Exception variables are cleared when an ``except`` block ends;
            # bind the redacted type now so the deferred callback cannot raise
            # a secondary NameError on the scheduler tick.
            error_type = type(exc).__name__
            callbacks = (
                lambda _event, error_type=error_type: {
                    "status": "blocked",
                    "reason": f"testnet_tick_setup_failed:{error_type}",
                },
                None,
            )
    return scheduler.tick(tick_id=tick_id, event={"kind": "market_heartbeat"}, advance=callbacks[0] if callbacks else None, reconcile=callbacks[1] if callbacks else None)


def _build_testnet_tick_callbacks(output_root: Path, coordinator_status: Mapping[str, Any]) -> tuple[Callable[[Mapping[str, Any]], Mapping[str, Any]], Callable[[], Mapping[str, Any]]] | None:
    """Compose the protected external broker for one running Testnet session."""
    config = HyperliquidTestnetRuntimeConfig.from_environment()
    if config is None:
        return (lambda _event: {"status": "blocked", "reason": "config_not_ready:broker_config"}, None)
    if not config.start_ready:
        return (lambda _event: {"status": "blocked", "reason": "config_not_ready:start_ready"}, None)
    digest = str(coordinator_status.get("plan_digest") or "")
    activation_id = str(coordinator_status.get("activation_id") or "")
    dashboard = _load_dashboard_plan(output_root, activation_id, digest)
    if dashboard is not None:
        preview, confirmation = dashboard
        plan = build_plan(preview, confirmation)
    else:
        stored = _load_testnet_plan(output_root, digest)
        if stored is None:
            return (lambda _event: {"status": "blocked", "reason": f"plan_source_missing:{digest}"}, None)
        plan = None
    instrument_id = str(coordinator_status.get("selected_instrument_id") or coordinator_status.get("instrument_id") or config.instrument_id)
    market_reader = HyperliquidTestnetMarketReader()
    market = dict(market_reader.read(instrument_id))
    if plan is None:
        plan = _park_plan_to_lifecycle_plan(stored, config=config, market=market)
    from services.broker_composition import BrokerBuildContext, build_broker_execution_port
    approval_id = str(
        (confirmation.get("confirmation_id") if dashboard is not None else None)
        or coordinator_status.get("approval_id")
        or ""
    ).strip()
    approved_by = str(
        (confirmation.get("operator_id") if dashboard is not None else None)
        or coordinator_status.get("approved_by")
        or ""
    ).strip()
    context = BrokerBuildContext(
        output_root=Path(output_root), execution_mode="live", live_trading_enabled=False,
        broker_config={"provider": "standard_broker", "broker_id": "hyperliquid", "environment": "testnet", "transport_profile": TESTNET_PROFILE, "account_id": config.account_address, "runtime_id": config.runtime_id, "release_sha": config.release_sha, "standard_broker_release_sha": config.standard_broker_release_sha, "capability_revision": config.capability_revision, "approval_id": approval_id, "approved_by": approved_by, "secret_file": str(config.secret_file), "instrument_id": instrument_id, "instrument_binding": {"instrument_id": instrument_id}, "market_source": {"source_id": str(market.get("source") or "hyperliquid.external_testnet"), "broker_id": "hyperliquid", "environment": "testnet", "instrument_id": instrument_id}, "execution_scope": "hypercore:default"},
    )
    broker = build_broker_execution_port(context)
    coordinator = TestnetAutomationCoordinator(output_root)
    family = str(coordinator_status.get("strategy_family") or "").lower()
    try:
        lifecycle_state = _load_testnet_lifecycle_state(
            output_root,
            plan,
            strategy_family=family,
        )
        hydrate_order_identities(broker, lifecycle_state)
    except OrderIdentityHydrationError as exc:
        reason = f"order_identity_hydration_failed:{exc}"
        return (
            lambda _event, reason=reason: {"status": "blocked", "reason": reason},
            None,
        )

    def facts() -> dict[str, Any]:
        raw = broker.read_public_facts(instrument_id=instrument_id)
        if isinstance(raw, Mapping):
            return dict(raw)
        return {key: getattr(raw, key, None) for key in ("status", "cursor", "fills", "positions", "open_orders")}

    def reconcile() -> Mapping[str, Any]:
        evidence = facts()
        evidence.update({"status": evidence.get("status") if evidence.get("status") in {"pass", "ok"} else ("pass" if evidence.get("cursor") else "unknown"), "activation_id": activation_id, "environment": "testnet", "account_fingerprint": coordinator_status.get("account_fingerprint"), "release_sha": coordinator_status.get("release_sha")})
        return evidence

    def advance(_event: Mapping[str, Any]) -> Mapping[str, Any]:
        evidence = facts()
        fills = evidence.get("fills") or []
        if not isinstance(fills, (list, tuple)):
            raise ValueError("testnet_fill_facts_unknown")
        # Every tick gets one binding market_fact and one same-attempt public
        # reader snapshot.  Keep this read outside the lifecycle so a quality
        # failure becomes a scheduler warning before the coordinator gate.
        try:
            tick_market, market_checks = read_coherent_market(
                preview if isinstance(preview.get("market"), Mapping) else {"market": {}},
                broker,
                instrument_id=instrument_id,
                market_reader=market_reader,
                read_reader_always=True,
            )
        except ProofDriverError as exc:
            return {
                "status": "failed",
                "reason": f"testnet_market_not_authoritative:{exc.reason_code}",
                "market_failure": {"reason": exc.reason_code, **exc.details},
            }
        # The Coordinator compares this execution time with market.observed_at.
        # Sample it only after the coherent market read so a slow read cannot
        # make a fresh market appear to come from the future.
        timestamp = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        if family == "grid":
            result = coordinator.status()
            for fill in fills:
                result = coordinator.advance_grid_session(plan, broker=broker, fill=dict(fill), market=tick_market, timestamp=timestamp)
            result = result if fills else coordinator.advance_grid_session(plan, broker=broker, price=float(tick_market.get("price") or 0), market=tick_market, timestamp=timestamp)
            return {**result, "market_checks": market_checks}
        result = coordinator.status()
        for fill in fills:
            result = coordinator.advance_dca_session(plan, broker=broker, fill=dict(fill), market=tick_market, timestamp=timestamp)
        result = result if fills else coordinator.advance_dca_session(plan, broker=broker, price=float(tick_market.get("price") or 0), market=tick_market, timestamp=timestamp)
        return {**result, "market_checks": market_checks}

    return advance, reconcile


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run one Park Telegram + Paper pass.")
    parser.add_argument("--output-root", default=os.getenv("TRADING_ORCHESTRATOR_OUTPUT_ROOT", "outputs"))
    parser.add_argument("--park-config", default=os.getenv("TRADING_ORCHESTRATOR_PARK_CONFIG", "configs/park_strategy_track.json"))
    parser.add_argument("--park-user-id", default=os.getenv("TRADING_ORCHESTRATOR_TELEGRAM_PARK_USER_ID", ""))
    parser.add_argument("--chat-id", default=os.getenv("TRADING_ORCHESTRATOR_TELEGRAM_CHAT_ID", ""))
    parser.add_argument("--timeout-seconds", type=int, default=20)
    args = parser.parse_args(argv)
    output_root = Path(args.output_root)
    try:
        ownership = SchedulerOwnershipGuard(output_root).verify()
        if not ownership.get("ok"):
            result = {
                "schema_version": "park-control-v1",
                "status": "blocked",
                "code": ownership.get("blocker") or "scheduler_ownership_blocked",
                "ownership": ownership,
                "paper_only": True,
            }
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
            return 79
        config = json.loads(Path(args.park_config).read_text(encoding="utf-8"))
        config = load_effective_park_config(output_root, config)
        try:
            config = prepare_park_paper_config(config)
        except Exception:
            # The read-only router will persist the typed contract blocker;
            # never invent an instrument or fee model at the process boundary.
            pass
        transport = TelegramBotTransport(chat_id=args.chat_id)
        intent_parser = ParkAiProviderGateway()
        testnet_runtime = HyperliquidTestnetRuntimeConfig.from_environment()
        testnet_account_reader = (
            build_park_account_reader(testnet_runtime)
            if testnet_runtime is not None
            else None
        )
        testnet_start_handler = build_testnet_start_handler(
            output_root,
            config=testnet_runtime,
            park_user_id=args.park_user_id,
            chat_id=args.chat_id,
        )
        router = ParkTelegramRouter(
            output_root,
            park_user_id=args.park_user_id,
            chat_id=args.chat_id,
            intent_parser=intent_parser,
            testnet_market_reader=HyperliquidTestnetMarketReader().read,
            testnet_account_reader=testnet_account_reader,
            testnet_start_handler=testnet_start_handler,
            config=config,
        )
        telegram = ParkTelegramWorker(router, timeout_seconds=args.timeout_seconds).run_once(transport)
        testnet_control = run_testnet_control_tick(output_root)
        legacy_cutover = run_legacy_cutover_once(
            output_root,
            config=config,
            park_user_id=args.park_user_id,
            chat_id=args.chat_id,
            repo_root=Path(__file__).resolve().parents[1],
            interpreter=os.getenv("TRADING_ORCHESTRATOR_NAUTILUS_PYTHON") or None,
        )
        # A completed exact-set receipt is the only way the default-off static
        # config becomes effective for the next Park runtime pass.
        config = load_effective_park_config(output_root, config)
        try:
            binding = build_park_authoritative_adapter(output_root, config=config)
            build_park_safety_evidence(
                output_root,
                config=config,
                repo_root=Path(__file__).resolve().parents[1],
                interpreter=os.getenv("TRADING_ORCHESTRATOR_NAUTILUS_PYTHON") or None,
                adapter=binding.adapter,
            )
            evidence = _latest(output_root / "park_strategy" / "safety_evidence.json")
            execution = ParkPaperRuntime(
                output_root,
                adapter=binding.adapter,
                park_user_id=args.park_user_id,
                chat_id=args.chat_id,
                config=config,
                safety_evidence_reader=lambda: evidence,
                mutation_authorizer=binding.authorize,
                mutation_revoker=binding.revoke,
            ).run_once()
        except ParkPaperRuntimeError as exc:
            execution = {
                "schema_version": "park-paper-runtime-v1",
                "status": "blocked",
                "code": exc.code,
                "paper_only": True,
            }
        delivery = router.drain_outbound(transport)
        result = {
            "schema_version": "park-control-v1",
            "status": "pass" if execution.get("status") != "blocked" else "blocked",
            "telegram": telegram,
            "testnet_control": testnet_control,
            "legacy_cutover": legacy_cutover,
            "execution": execution,
            "delivery": delivery,
            "paper_only": True,
        }
    except (TelegramBotTransportError, ParkTelegramRuntimeError, ValueError, OSError, json.JSONDecodeError) as exc:
        result = {
            "schema_version": "park-control-v1",
            "status": "blocked",
            "code": getattr(exc, "code", "park_control_blocked"),
            "paper_only": True,
        }
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result.get("status") == "pass" else 2


if __name__ == "__main__":
    sys.exit(main())
