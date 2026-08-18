"""Paper-only executor for the explicit legacy clean-slate migration."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from services.park_cutover_guard import evaluate_park_cutover
from services.park_legacy_cutover import (
    ParkLegacyCutoverError,
    ParkLegacyCutoverLedger,
    normalize_order_identities,
)
from services.park_paper_mutation_gate import _mint_park_paper_capability
from services.park_safety_evidence import build_park_safety_evidence
from services.park_strategy_session import ParkStrategyIdentityJournal
from services.park_telegram_control import ParkTelegramLedger
from services.scheduler_ownership import SchedulerOwnershipGuard
from services.strategy_control_plane import production_mutation_lock


LEGACY_TICK_UNIT_PATH = Path("/etc/systemd/system/gridmind-live-tick.service")


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _blocker(code: str, detail: str, *, proposal: Mapping[str, Any], observed_at: str, retryable: bool) -> dict[str, Any]:
    return {
        "status": "blocked",
        "code": code,
        "detail": str(detail)[:500],
        "proposal_id": str(proposal.get("proposal_id") or ""),
        "proposal_digest": str(proposal.get("proposal_digest") or ""),
        "recorded_at": observed_at,
        "paper_only": True,
        "next_action": "retry_legacy_cutover" if retryable else "notify_park_and_wait",
    }


def _queue_message(output_root: Path, *, park_user_id: str, chat_id: str, key: str, message_type: str, text: str) -> None:
    ParkTelegramLedger(output_root, park_user_id=park_user_id, chat_id=chat_id).queue_outbound(
        idempotency_key=key,
        message_type=message_type,
        text=text,
        binding=None,
    )


def inspect_legacy_runner_quarantine(output_root: Path, *, unit_path: Path = LEGACY_TICK_UNIT_PATH) -> dict[str, Any]:
    """Prove the Cloud scheduler cannot concurrently run the legacy runner."""

    try:
        ownership = SchedulerOwnershipGuard(Path(output_root)).verify()
    except Exception as exc:  # noqa: BLE001 - ownership uncertainty blocks cleanup.
        return {"ok": False, "code": "scheduler_ownership_unavailable", "detail": type(exc).__name__}
    if ownership.get("ok") is not True:
        return {
            "ok": False,
            "code": "scheduler_ownership_blocked",
            "detail": str(ownership.get("blocker") or "ownership guard did not pass"),
            "ownership": ownership,
        }
    try:
        unit_text = Path(unit_path).read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        return {"ok": False, "code": "legacy_tick_unit_unavailable", "detail": type(exc).__name__}
    has_park_control = "pipelines.park_control" in unit_text
    has_legacy_runner = "pipelines.dualtrack_cycle_runner" in unit_text
    return {
        "ok": has_park_control and not has_legacy_runner,
        "code": "ok" if has_park_control and not has_legacy_runner else "legacy_runner_not_quarantined",
        "unit_path": str(unit_path),
        "park_control": has_park_control,
        "legacy_cycle_runner": has_legacy_runner,
        "ownership": ownership,
    }


def run_legacy_cutover_once(
    output_root: Path,
    *,
    config: Mapping[str, Any],
    park_user_id: str,
    chat_id: str,
    repo_root: Path,
    interpreter: str | Path | None = None,
    now: Callable[[], str] | None = None,
    market_reader: Callable[[], Mapping[str, Any]] | None = None,
    account_reader: Callable[[Path, str], Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Re-read and cancel exactly one Park-confirmed legacy order set.

    The function is deliberately independent of the normal strategy runtime:
    it cannot create a session, submit an entry, flatten a position, or
    reverse direction.  A failed re-read is durable and never widened into a
    best-effort account-wide cancellation.
    """

    output_root = Path(output_root)
    observed_at = (now or _utc_now)()
    ledger = ParkLegacyCutoverLedger(output_root, park_user_id=park_user_id, chat_id=chat_id)
    pending = ledger.confirmed_pending()
    if not pending:
        completed = next((row for row in reversed(ledger.rows()) if row.get("event") == "completed"), None)
        return {
            "status": "already_completed" if completed else "idle",
            "paper_only": True,
            "next_action": "await_new_park_strategy" if completed else "await_legacy_cutover_confirmation",
        }
    if len(pending) != 1:
        detail = "multiple legacy cutover proposals are pending"
        for proposal in pending:
            ledger.record(proposal=proposal, event="blocked", code="multiple_pending_legacy_cutovers", detail=detail, retryable=False)
        return _blocker("multiple_pending_legacy_cutovers", detail, proposal=pending[-1], observed_at=observed_at, retryable=False)
    proposal = pending[0]
    identity = ParkStrategyIdentityJournal(output_root)
    if identity.active_session() is not None:
        detail = "an active Park strategy identity exists; legacy cutover cannot cancel it"
        ledger.record(proposal=proposal, event="blocked", code="active_park_strategy", detail=detail, retryable=False)
        return _blocker("active_park_strategy", detail, proposal=proposal, observed_at=observed_at, retryable=False)

    from services.park_paper_runtime import build_park_authoritative_adapter
    from services.park_telegram_runtime import (
        default_account_reader,
        default_market_reader,
    )

    read_account = account_reader or default_account_reader
    read_market = market_reader or default_market_reader
    try:
        # The initial config is intentionally default-off.  This adapter is
        # used for reads until the exact receipt proves clean slate.
        binding = build_park_authoritative_adapter(
            output_root,
            config=config,
            allow_disabled_read=True,
        )
    except Exception as exc:  # noqa: BLE001 - unknown authority blocks.
        detail = f"authoritative Paper adapter unavailable: {type(exc).__name__}"
        ledger.record(proposal=proposal, event="blocked", code="paper_adapter_unavailable", detail=detail, retryable=True)
        return _blocker("paper_adapter_unavailable", detail, proposal=proposal, observed_at=observed_at, retryable=True)

    try:
        expected_orders = normalize_order_identities([dict(row) for row in proposal.get("orders") or []])
    except ParkLegacyCutoverError as exc:
        ledger.record(proposal=proposal, event="blocked", code=exc.code, detail=str(exc), retryable=False)
        return _blocker(exc.code, str(exc), proposal=proposal, observed_at=observed_at, retryable=False)
    expected_by_id = {str(row["order_id"]): row for row in expected_orders}
    cycle_ids = sorted({str(row["cycle_id"]) for row in expected_orders})
    if not cycle_ids:
        detail = "legacy cutover proposal has no exact order cycles"
        ledger.record(proposal=proposal, event="blocked", code="legacy_order_identity_missing", detail=detail, retryable=False)
        return _blocker("legacy_order_identity_missing", detail, proposal=proposal, observed_at=observed_at, retryable=False)

    with production_mutation_lock(output_root):
        try:
            facts = dict(read_account(output_root, cycle_ids[0], config=config))
            snapshot = facts.get("snapshot") if isinstance(facts.get("snapshot"), Mapping) else {}
            exposure = snapshot.get("account_wide_legacy_exposure") if isinstance(snapshot, Mapping) else {}
            exposure = exposure if isinstance(exposure, Mapping) else {}
            actual_orders = normalize_order_identities([dict(row) for row in exposure.get("orders") or [] if isinstance(row, Mapping)])
            actual_positions = [dict(row) for row in exposure.get("positions") or [] if isinstance(row, Mapping)]
            actual_by_id = {str(row["order_id"]): row for row in actual_orders}
            if set(actual_by_id) != set(expected_by_id):
                raise ParkLegacyCutoverError("legacy_order_set_drift", "authoritative accepted order set changed since confirmation")
            if actual_positions:
                raise ParkLegacyCutoverError("legacy_positions_present", "a position appeared; cutover will not flatten it")
            if facts.get("reconciliation_healthy") is not True:
                raise ParkLegacyCutoverError("legacy_reconciliation_unhealthy", "authoritative Paper reconciliation is not healthy")
            quarantine = inspect_legacy_runner_quarantine(output_root) if facts.get("unresolved_runtime") is True else {"ok": True, "code": "not_required"}
            if quarantine.get("ok") is not True:
                raise ParkLegacyCutoverError("legacy_runner_not_quarantined", str(quarantine.get("detail") or quarantine.get("code") or "legacy runner quarantine is unproven"))
            market = dict(read_market())
            if market.get("trusted") is not True or market.get("fresh") is not True:
                raise ParkLegacyCutoverError("market_not_authoritative", "legacy cutover retains the trusted fresh market gate")

            effective_config = dict(config)
            effective_config["feature_enabled"] = True
            evidence = build_park_safety_evidence(
                output_root,
                config=effective_config,
                repo_root=repo_root,
                interpreter=interpreter,
                adapter=binding.adapter,
            )
            evidence = dict(evidence)
            evidence.update(
                {
                    "trusted_market": True,
                    "tick_freshness": True,
                    "stale_cycle_state": not bool(facts.get("unresolved_runtime")),
                    "reconciliation": True,
                    "park_risk_confirmation": True,
                }
            )
            if facts.get("unresolved_runtime") is True and quarantine.get("ok") is True:
                evidence["stale_cycle_state"] = True
            gate = evaluate_park_cutover(effective_config, safety_evidence=evidence)
            if gate.get("status") != "pass":
                raise ParkLegacyCutoverError("park_cutover_blocked", ",".join(str(value) for value in gate.get("blockers") or []))

            proposal_id = str(proposal["proposal_id"])
            binding.authorize(
                _mint_park_paper_capability(
                    {
                        "issuer": "ParkLegacyCutoverRuntime.run_once",
                        "cycle_id": "legacy-cutover",
                        "strategy_session_id": f"legacy-cutover-session-{proposal_id}",
                        "strategy_revision_id": f"legacy-cutover-revision-{proposal_id}",
                        "plan_digest": f"legacy-cutover-plan:{proposal['proposal_digest']}",
                        "park_confirmation_digest": str(proposal["proposal_digest"]),
                        "cutover_status": "pass",
                        "legacy_cutover_id": proposal_id,
                        "operation": "cancel_legacy_orders",
                    }
                )
            )
            grouped: dict[str, list[str]] = defaultdict(list)
            for row in expected_orders:
                grouped[str(row["cycle_id"])].append(str(row["order_id"]))
            cancellations: list[dict[str, Any]] = []
            for cycle_id, order_ids in sorted(grouped.items()):
                cancellation = dict(
                    binding.adapter.cancel_orders(
                        cycle_id,
                        order_ids=sorted(order_ids),
                        ts=observed_at,
                        reason="park_legacy_clean_slate",
                        legacy_cutover_id=proposal_id,
                    )
                )
                flush_commands = getattr(binding.adapter, "flush_commands", None)
                if callable(flush_commands):
                    cancellation["flush"] = dict(
                        flush_commands(cycle_id, legacy_cutover_id=proposal_id)
                    )
                cancellations.append(cancellation)
            final_facts = dict(read_account(output_root, cycle_ids[0], config=config))
            final_snapshot = final_facts.get("snapshot") if isinstance(final_facts.get("snapshot"), Mapping) else {}
            final_exposure = final_snapshot.get("account_wide_legacy_exposure") if isinstance(final_snapshot, Mapping) else {}
            final_exposure = final_exposure if isinstance(final_exposure, Mapping) else {}
            final_orders = [row for row in final_exposure.get("orders") or [] if isinstance(row, Mapping)]
            final_positions = [row for row in final_exposure.get("positions") or [] if isinstance(row, Mapping)]
            remaining_ids = sorted(str(row.get("order_id") or "") for row in final_orders if str(row.get("order_id") or ""))
            if remaining_ids or final_positions or final_facts.get("reconciliation_healthy") is not True:
                raise ParkLegacyCutoverError("legacy_cutover_verification_failed", f"remaining_orders={remaining_ids}; positions={len(final_positions)}")
            completed = ledger.record(
                proposal=proposal,
                event="completed",
                code="legacy_cutover_completed",
                clean_slate_verified=True,
                enabled_park_paper=True,
                cancelled_order_count=len(expected_orders),
                cancelled_order_ids=[str(row["order_id"]) for row in expected_orders],
                cancellations=cancellations,
                final_reconciliation=final_facts.get("reconciliation"),
                cutover_gate=gate,
                safety_evidence_ref={
                    "release_sha": str(evidence.get("release_sha") or ""),
                    "source_tree_sha": str(evidence.get("source_tree_sha") or ""),
                },
                legacy_runner_quarantine=quarantine,
                release_sha=str(evidence.get("release_sha") or ""),
                source_tree_sha=str(evidence.get("source_tree_sha") or ""),
                retryable=False,
            )
            _queue_message(
                output_root,
                park_user_id=park_user_id,
                chat_id=chat_id,
                key=f"park-legacy-cutover-completed:{proposal_id}",
                message_type="legacy_cutover_completed",
                text=(
                    f"Clean slate 已完成：已精确撤销 {len(expected_orders)} 个旧挂单，0 个持仓；Park Paper 已启用。\n"
                    "现在可以发送新的 DCA 或 Grid 策略；确认前仍不会下单。"
                ),
            )
            return {
                "status": "completed",
                "proposal": proposal,
                "receipt": completed,
                "cancellations": cancellations,
                "paper_only": True,
                "next_action": "await_new_park_strategy",
            }
        except ParkLegacyCutoverError as exc:
            retryable = exc.code in {"paper_adapter_unavailable", "market_not_authoritative", "park_cutover_blocked", "legacy_reconciliation_unhealthy"}
            blocked = ledger.record(
                proposal=proposal,
                event="blocked",
                code=exc.code,
                detail=str(exc),
                retryable=retryable,
            )
            _queue_message(
                output_root,
                park_user_id=park_user_id,
                chat_id=chat_id,
                key=f"park-legacy-cutover-blocked:{proposal['proposal_id']}:{exc.code}",
                message_type="legacy_cutover_blocker",
                text=f"Park clean slate 阻塞：{exc.code}；不会扩大撤单范围，也不会平仓。下一步：{'重试同一清单' if retryable else '人工处理后重新发起'}。",
            )
            return {**_blocker(exc.code, str(exc), proposal=proposal, observed_at=observed_at, retryable=retryable), "receipt": blocked}
        except Exception as exc:  # noqa: BLE001 - any uncertain mutation is fail-closed.
            blocked = ledger.record(
                proposal=proposal,
                event="blocked",
                code="legacy_cutover_exception",
                detail=type(exc).__name__,
                retryable=False,
            )
            _queue_message(
                output_root,
                park_user_id=park_user_id,
                chat_id=chat_id,
                key=f"park-legacy-cutover-exception:{proposal['proposal_id']}",
                message_type="legacy_cutover_blocker",
                text="Park clean slate 阻塞：状态不确定；系统没有继续撤单，也没有平仓。请人工核对后重新发起。",
            )
            return {**_blocker("legacy_cutover_exception", type(exc).__name__, proposal=proposal, observed_at=observed_at, retryable=False), "receipt": blocked}
        finally:
            binding.revoke()
