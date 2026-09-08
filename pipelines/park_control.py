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
from services.telegram_bot_transport import (
    TelegramBotTransport,
    TelegramBotTransportError,
)


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
    if scheduler.status().get("status") == "idle" and str(coordinator_status.get("status") or "") in {"grid_running", "dca_running"}:
        scheduler.attach(str(coordinator_status.get("activation_id") or ""))
    if scheduler.status().get("status") not in {"active", "reconcile_required"}:
        return {"status": "not_applicable", "reason": "testnet_scheduler_not_active", "paper_only": True}
    tick_id = "park-control:" + datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    callbacks = None
    if str(coordinator_status.get("status") or "") in {"grid_running", "dca_running"}:
        try:
            callbacks = _build_testnet_tick_callbacks(output_root, coordinator_status)
        except Exception as exc:  # noqa: BLE001 - scheduler records the typed blocker.
            callbacks = (lambda _event: {"status": "blocked", "reason": f"testnet_tick_setup_failed:{type(exc).__name__}"}, None)
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
    market = dict(HyperliquidTestnetMarketReader().read(instrument_id))
    if plan is None:
        plan = _park_plan_to_lifecycle_plan(stored, config=config, market=market)
    from services.broker_composition import BrokerBuildContext, build_broker_execution_port
    context = BrokerBuildContext(
        output_root=Path(output_root), execution_mode="live", live_trading_enabled=False,
        broker_config={"provider": "standard_broker", "broker_id": "hyperliquid", "environment": "testnet", "transport_profile": TESTNET_PROFILE, "account_id": config.account_address, "runtime_id": config.runtime_id, "release_sha": config.release_sha, "standard_broker_release_sha": config.standard_broker_release_sha, "capability_revision": config.capability_revision, "secret_file": str(config.secret_file), "instrument_id": instrument_id, "instrument_binding": {"instrument_id": instrument_id}, "market_source": {"source_id": str(market.get("source") or "hyperliquid.external_testnet"), "broker_id": "hyperliquid", "environment": "testnet", "instrument_id": instrument_id}, "execution_scope": "hypercore:default"},
    )
    broker = build_broker_execution_port(context)
    from services.testnet_execution import ExternalTestnetExecutionPort
    port = ExternalTestnetExecutionPort(broker)
    coordinator = TestnetAutomationCoordinator(output_root)

    def facts() -> dict[str, Any]:
        raw = port.read_facts(instrument_id=instrument_id)
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
        timestamp = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        family = str(coordinator_status.get("strategy_family") or "").lower()
        if family == "grid":
            result = coordinator.status()
            for fill in fills:
                result = coordinator.advance_grid_session(plan, broker=broker, fill=dict(fill), market=market, timestamp=timestamp)
            return result if fills else coordinator.advance_grid_session(plan, broker=broker, price=float(market.get("price") or 0), market=market, timestamp=timestamp)
        result = coordinator.status()
        for fill in fills:
            result = coordinator.advance_dca_session(plan, broker=broker, fill=dict(fill), market=market, timestamp=timestamp)
        return result if fills else coordinator.advance_dca_session(plan, broker=broker, price=float(market.get("price") or 0), market=market, timestamp=timestamp)

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
