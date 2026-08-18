"""Paper-only execution boundary for the Park Strategy Track.

The legacy cycle runner is deliberately not imported here.  A Park session is
an execution identity that survives the 09:00/21:00 recording windows, while
the Paper adapter namespace is stable for that session.  This module accepts
only an exact, unexpired Park confirmation and a passing cutover evidence
bundle; otherwise it records a blocker and performs no broker mutation.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from services.execution_engine_port import ExecutionEngineAdapter
from services.park_confirmation import ParkConfirmationLedger
from services.park_cutover_guard import evaluate_park_cutover, load_default_config
from services.park_dca_track import ParkDcaLifecycle
from services.park_grid_track import ParkGridLifecycle
from services.park_paper_preflight import ParkPaperPreflightError, build_park_paper_preflight
from services.park_paper_mutation_gate import (
    ParkPaperAdapterBinding,
    ParkPaperMutationCapability,
    _mint_park_paper_capability,
)
from services.park_recording_track import ParkRecordingTrack
from services.park_strategy_snapshot import record_strategy_snapshot_terminal
from services.park_strategy_lifecycle import ParkStrategyLifecycleLedger
from services.park_strategy_session import (
    ParkStrategyIdentityJournal,
    recording_window,
)
from services.park_telegram_control import ParkTelegramLedger
from services.strategy_control_plane import production_mutation_lock


PARK_PAPER_RUNTIME_SCHEMA = "park-paper-runtime-v1"
_SAFE_NAMESPACE = re.compile(r"[^a-zA-Z0-9_-]+")


class ParkPaperRuntimeError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _text(value: Any, field: str) -> str:
    result = str(value or "").strip()
    if not result:
        raise ParkPaperRuntimeError("identity_missing", f"{field} is required")
    return result


def _digest(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _append_jsonl(path: Path, row: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(dict(row), ensure_ascii=False, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            value = json.loads(line)
            if isinstance(value, dict):
                rows.append(value)
    return rows


def park_paper_namespace(strategy_session_id: str) -> str:
    """Return a stable adapter namespace that is independent of record windows."""

    session = _text(strategy_session_id, "strategy_session_id")
    safe = _SAFE_NAMESPACE.sub("-", session).strip("-")
    if not safe:
        raise ParkPaperRuntimeError("identity_invalid", "strategy session namespace is invalid")
    return f"park-{safe[:120]}"


def _owned(artifact: Mapping[str, Any], session: str, revision: str, digest: str) -> bool:
    return (
        str(artifact.get("strategy_session_id") or "") == session
        and str(artifact.get("strategy_revision_id") or "") == revision
        and str(artifact.get("plan_digest") or artifact.get("strategy_plan_id") or "") == digest
    )


class ParkPaperRuntime:
    """Run one bounded, idempotent Paper tick for the active Park revision."""

    def __init__(
        self,
        output_root: Path,
        *,
        adapter: ExecutionEngineAdapter,
        park_user_id: str,
        chat_id: str,
        config: Mapping[str, Any] | None = None,
        market_reader: Callable[[], Mapping[str, Any]] | None = None,
        now: Callable[[], str] | None = None,
        safety_evidence_reader: Callable[[], Mapping[str, Any]] | None = None,
        mutation_authorizer: Callable[[ParkPaperMutationCapability], None] | None = None,
        mutation_revoker: Callable[[], None] | None = None,
    ) -> None:
        self.output_root = Path(output_root)
        self.adapter = adapter
        self.config = dict(config or load_default_config())
        if str(getattr(adapter, "name", "")).strip() != "nautilus_paper":
            raise ParkPaperRuntimeError(
                "authoritative_paper_adapter_required",
                "Park requires the direct authoritative nautilus_paper adapter",
            )
        self.park_user_id = _text(park_user_id, "park_user_id")
        self.chat_id = _text(chat_id, "chat_id")
        self.market_reader = market_reader
        self.now = now or _utc_now
        self.safety_evidence_reader = safety_evidence_reader or (lambda: {})
        self.mutation_authorizer = mutation_authorizer
        self.mutation_revoker = mutation_revoker
        self.identity = ParkStrategyIdentityJournal(self.output_root)
        self.recording = ParkRecordingTrack(self.output_root)
        self.telegram = ParkTelegramLedger(
            self.output_root,
            park_user_id=self.park_user_id,
            chat_id=self.chat_id,
        )
        self.confirmations = ParkConfirmationLedger(self.output_root, park_user_id=self.park_user_id)
        self.execution_path = self.output_root / "park_strategy" / "executions.jsonl"
        self.blocker_path = self.output_root / "park_strategy" / "runtime_blockers.jsonl"
        self.review_path = self.output_root / "park_strategy" / "recording" / "reviews.jsonl"

    def run_once(self) -> dict[str, Any]:
        observed_at = self.now()
        recording_failures = self._close_due_recording_packages(observed_at)
        recording_blocker = None
        if recording_failures:
            active_for_recording = self.identity.active_session() or {}
            recording_blocker = self._record_recording_blocker(
                recording_failures,
                observed_at=observed_at,
                strategy_session_id=active_for_recording.get("strategy_session_id"),
                strategy_revision_id=active_for_recording.get("strategy_revision_id"),
            )
        active = self.identity.active_session()
        if not active:
            return self._result(
                "idle",
                observed_at=observed_at,
                next_action="retry_recording_package" if recording_failures else "await_new_park_strategy",
                recording_failures=recording_failures,
                recording_blocker=recording_blocker,
            )
        session = _text(active.get("strategy_session_id"), "strategy_session_id")
        revision = _text(active.get("strategy_revision_id"), "strategy_revision_id")
        digest = _text(active.get("plan_digest"), "plan_digest")
        facts_gate_failure, facts_gate_blocker = self._recording_facts_gate(
            active,
            observed_at=observed_at,
        )
        if facts_gate_failure:
            recording_failures.append(facts_gate_failure)
            recording_blocker = recording_blocker or facts_gate_blocker
        plan = self._plan_for_digest(digest)
        if plan is None:
            return self._blocked(
                "plan_missing",
                "active Park session has no immutable plan",
                session=session,
                revision=revision,
                digest=digest,
                observed_at=observed_at,
            )
        pending_terminal = self._pending_terminal_action(session, revision)
        confirmation = self._confirmed_for(digest, session, revision)
        if confirmation is None:
            expired = self._release_expired_unconfirmed_session(
                active,
                session=session,
                revision=revision,
                digest=digest,
                observed_at=observed_at,
            )
            if expired is not None:
                return expired
            facts_failure, facts_blocker = self._record_window_facts_safely(
                active,
                observed_at=observed_at,
                status="awaiting_confirmation",
                plan=plan,
                market=None,
                snapshot=None,
                reconciliation=None,
            )
            all_recording_failures = recording_failures + ([facts_failure] if facts_failure else [])
            return self._result(
                "awaiting_confirmation",
                observed_at=observed_at,
                session=session,
                revision=revision,
                digest=digest,
                next_action="await_exact_park_confirmation",
                recording_failures=all_recording_failures,
                recording_blocker=facts_blocker or recording_blocker,
            )

        market_started = time.monotonic()
        try:
            market = dict(self.market_reader() if self.market_reader else self._default_market_reader())
        except Exception as exc:  # noqa: BLE001 - runtime must fail closed.
            return self._blocked(
                "market_unavailable",
                type(exc).__name__,
                session=session,
                revision=revision,
                digest=digest,
                observed_at=observed_at,
            )
        market_elapsed_ms = round((time.monotonic() - market_started) * 1000, 3)
        if market.get("trusted") is not True or market.get("fresh") is not True:
            return self._blocked(
                "market_not_authoritative",
                "Park Paper tick requires trusted and fresh market evidence",
                session=session,
                revision=revision,
                digest=digest,
                observed_at=observed_at,
                market=market,
            )
        try:
            current_price = float(market.get("price"))
        except (TypeError, ValueError):
            return self._blocked(
                "market_price_invalid",
                "market price is not numeric",
                session=session,
                revision=revision,
                digest=digest,
                observed_at=observed_at,
                market=market,
            )
        cycle_id = park_paper_namespace(session)
        with production_mutation_lock(self.output_root):
            try:
                snapshot = dict(
                    self.adapter.snapshot(
                        cycle_id,
                        mark_price=current_price,
                        mark_fresh=True,
                        mark_source=str(market.get("source") or ""),
                    )
                )
                reconciliation = dict(self.adapter.reconcile(cycle_id))
            except Exception as exc:  # noqa: BLE001 - no mutation when state is unknown.
                return self._blocked(
                    "paper_state_unavailable",
                    type(exc).__name__,
                    session=session,
                    revision=revision,
                    digest=digest,
                    observed_at=observed_at,
                    market=market,
                )
            gate = self._admission(
                market=market,
                snapshot=snapshot,
                reconciliation=reconciliation,
                confirmation=confirmation,
            )
            if gate.get("status") != "pass":
                return self._blocked(
                    "park_cutover_blocked",
                    ",".join(str(value) for value in gate.get("blockers") or []),
                    session=session,
                    revision=revision,
                    digest=digest,
                    observed_at=observed_at,
                    market=market,
                    snapshot=snapshot,
                    reconciliation=reconciliation,
                    gate=gate,
                )
            self._grant_adapter_mutation(
                session=session,
                revision=revision,
                digest=digest,
                cycle_id=cycle_id,
                confirmation=confirmation,
                gate=gate,
            )
            try:
                pre_terminal = (
                    self._boundary_from_terminal_action(pending_terminal, current_price)
                    if pending_terminal
                    else self._terminal_reason(plan, current_price)
                )
                has_prior_entries = any(
                    row.get("event") == "entry_submitted"
                    and _owned(row, session, revision, digest)
                    for row in _read_jsonl(self.execution_path)
                )
                submitted = (
                    []
                    if recording_failures or pre_terminal
                    else self._submit_entries_if_needed(
                        plan=plan,
                        confirmation=confirmation,
                        session=session,
                        revision=revision,
                        digest=digest,
                        cycle_id=cycle_id,
                        current_price=current_price,
                        market=market,
                        observed_at=observed_at,
                    )
                )
                event_result = self.adapter.process_market_event(
                    self._market_event(
                        market,
                        cycle_id=cycle_id,
                        observed_at=observed_at,
                    )
                )
                snapshot = dict(
                    self.adapter.snapshot(
                        cycle_id,
                        mark_price=current_price,
                        mark_fresh=True,
                        mark_source=str(market.get("source") or ""),
                    )
                )
                reconciliation = dict(self.adapter.reconcile(cycle_id))
            except Exception as exc:  # noqa: BLE001 - partial execution is durable and blocked.
                return self._blocked(
                    "paper_mutation_blocked",
                    type(exc).__name__,
                    session=session,
                    revision=revision,
                    digest=digest,
                    observed_at=observed_at,
                    market=market,
                    snapshot=snapshot,
                    reconciliation=reconciliation,
                )
            boundary = (
                self._boundary_from_terminal_action(pending_terminal, current_price)
                if pending_terminal
                else self._terminal_reason(plan, current_price)
            )
            if boundary:
                terminal = self._terminal(
                    plan=plan,
                    session=session,
                    revision=revision,
                    digest=digest,
                    cycle_id=cycle_id,
                    boundary=boundary,
                    current_price=current_price,
                    observed_at=observed_at,
                    market=market,
                    snapshot=snapshot,
                    reconciliation=reconciliation,
                )
                if terminal.get("status") != "paused":
                    return terminal
                self._revoke_adapter_mutation()
                result = {
                    **terminal,
                    "submitted": submitted,
                    "market_event": event_result,
                    "market_read_ms": market_elapsed_ms,
                }
            else:
                facts_failure, facts_blocker = self._record_window_facts_safely(
                    self.identity.active_session() or active,
                    observed_at=observed_at,
                    status="active",
                    plan=plan,
                    market=market,
                    snapshot=snapshot,
                    reconciliation=reconciliation,
                )
                all_recording_failures = recording_failures + ([facts_failure] if facts_failure else [])
                result = self._result(
                    "active",
                    observed_at=observed_at,
                    session=session,
                    revision=revision,
                    digest=digest,
                    submitted=submitted,
                    market_event=event_result,
                    snapshot=snapshot,
                    reconciliation=reconciliation,
                    market_read_ms=market_elapsed_ms,
                    next_action="retry_recording_package" if all_recording_failures else "continue_trusted_fresh_ticks",
                    recording_failures=all_recording_failures,
                    recording_blocker=facts_blocker or recording_blocker,
                )
                self._revoke_adapter_mutation()
            return result

    def _submit_entries_if_needed(
        self,
        *,
        plan: Mapping[str, Any],
        confirmation: Mapping[str, Any],
        session: str,
        revision: str,
        digest: str,
        cycle_id: str,
        current_price: float,
        market: Mapping[str, Any],
        observed_at: str,
    ) -> list[dict[str, Any]]:
        existing = {
            str(row.get("entry_id") or ""): dict(row)
            for row in _read_jsonl(self.execution_path)
            if row.get("event") == "entry_submitted"
            and _owned(row, session, revision, digest)
        }
        normalized = dict(plan.get("normalized_input") or {})
        if str(normalized.get("strategy_type") or "") == "dca":
            lifecycle = ParkDcaLifecycle(
                plan,
                confirmation_receipt=confirmation,
                output_root=self.output_root,
                park_user_id=self.park_user_id,
                chat_id=self.chat_id,
            )
            commands = lifecycle.entry_commands()
        elif str(normalized.get("strategy_type") or "") == "grid":
            lifecycle = ParkGridLifecycle(
                plan,
                confirmation_receipt=confirmation,
                output_root=self.output_root,
                park_user_id=self.park_user_id,
                chat_id=self.chat_id,
            )
            commands = lifecycle.levels()
        else:
            raise ParkPaperRuntimeError("strategy_type_invalid", "Park strategy type is unsupported")
        receipts: list[dict[str, Any]] = []
        for source in commands:
            entry_id = _text(source.get("entry_id") or source.get("level_id"), "entry_id")
            if entry_id in existing:
                receipts.append(dict(existing[entry_id].get("receipt") or {}))
                continue
            command = self._entry_command(
                source,
                plan=plan,
                session=session,
                revision=revision,
                digest=digest,
                cycle_id=cycle_id,
                current_price=current_price,
                market=market,
                observed_at=observed_at,
            )
            receipt = dict(self.adapter.submit_order(command))
            _append_jsonl(
                self.execution_path,
                {
                    "schema_version": PARK_PAPER_RUNTIME_SCHEMA,
                    "event": "entry_submitted",
                    "entry_id": entry_id,
                    "strategy_session_id": session,
                    "strategy_revision_id": revision,
                    "plan_digest": digest,
                    "cycle_id": cycle_id,
                    "command": command,
                    "receipt": receipt,
                    "recorded_at": observed_at,
                },
            )
            receipts.append(receipt)
        return receipts

    def _entry_command(
        self,
        source: Mapping[str, Any],
        *,
        plan: Mapping[str, Any],
        session: str,
        revision: str,
        digest: str,
        cycle_id: str,
        current_price: float,
        market: Mapping[str, Any],
        observed_at: str,
    ) -> dict[str, Any]:
        normalized = dict(plan.get("normalized_input") or {})
        price = float(source.get("price") or 0.0)
        quantity = float(source.get("quantity") or 0.0)
        if price <= 0 or quantity <= 0:
            raise ParkPaperRuntimeError("risk_incomplete", "Park entry command has no positive price/quantity")
        direction = str(source.get("direction") or normalized.get("direction") or "")
        if direction not in {"long", "short", "neutral"}:
            raise ParkPaperRuntimeError("direction_invalid", "Park entry direction is invalid")
        if direction == "neutral":
            side = str(source.get("side") or "").lower()
            if side not in {"buy", "sell"}:
                raise ParkPaperRuntimeError("neutral_grid_side_invalid", "neutral Grid entry must declare buy or sell")
        else:
            side = "buy" if direction == "long" else "sell"
        command: dict[str, Any] = {
            "cycle_id": cycle_id,
            "ts": observed_at,
            "event": "entry",
            "side": side,
            "order_type": "limit",
            "price": price,
            "market_price": current_price,
            "quantity": quantity,
            "notional": round(price * quantity, 12),
            "source": "park_telegram",
            "source_fill_id": str(source.get("entry_id") or source.get("level_id")),
            "strategy_plan_id": digest,
            "strategy_plan_version": str(plan.get("schema_version") or ""),
            "strategy_session_id": session,
            "strategy_revision_id": revision,
            "plan_digest": digest,
            "symbol": str(market.get("symbol") or self._paper_instrument_symbol()),
            "instrument_id": str(market.get("instrument_id") or self._paper_instrument_symbol()),
            "market_timestamp": str(market.get("observed_at") or observed_at),
            "market_source": str(market.get("source") or ""),
            "market_fresh": True,
        }
        for source_key, target_key, normalized_key in (
            ("sl", "sl", "stop_price"),
            ("tp", "tp", "take_profit_price"),
        ):
            if str(normalized.get("strategy_type") or "") == "dca":
                # DCA has one aggregate strategy exit; do not duplicate TP/SL
                # protection on each entry command.
                continue
            value = source.get(source_key)
            if value in (None, ""):
                value = normalized.get(normalized_key)
            if value not in (None, ""):
                command[target_key] = value
        for key in ("grid_line_id", "grid_generation", "grid_rearm_enabled", "rearm_of_order_id"):
            if source.get(key) not in (None, ""):
                command[key] = source[key]
        return command

    def _terminal(
        self,
        *,
        plan: Mapping[str, Any],
        session: str,
        revision: str,
        digest: str,
        cycle_id: str,
        boundary: Mapping[str, Any],
        current_price: float,
        observed_at: str,
        market: Mapping[str, Any],
        snapshot: Mapping[str, Any],
        reconciliation: Mapping[str, Any],
    ) -> dict[str, Any]:
        lifecycle_ledger: ParkStrategyLifecycleLedger | None = None
        normalized_strategy_type = str((plan.get("normalized_input") or {}).get("strategy_type") or "")
        if normalized_strategy_type in {"dca", "grid"}:
            lifecycle_ledger = ParkStrategyLifecycleLedger(self.output_root)
            pending_action = lifecycle_ledger.pending_terminal_action(
                strategy_session_id=session,
                strategy_revision_id=revision,
            )
            if pending_action:
                boundary = self._boundary_from_terminal_action(pending_action, current_price)
            else:
                lifecycle_ledger.activate({
                    **dict(plan.get("normalized_input") or {}),
                    "strategy_session_id": session,
                    "strategy_revision_id": revision,
                    "plan_digest": digest,
                    "maximum_leverage": (plan.get("risk") or {}).get("effective_leverage"),
                    "maximum_acceptable_loss": (plan.get("risk") or {}).get("theoretical_max_loss"),
                })
                if normalized_strategy_type == "dca":
                    lifecycle_ledger.terminal_action_plan(
                        strategy_session_id=session,
                        strategy_revision_id=revision,
                        trigger=str(boundary["reason"]),
                        observed_price=current_price,
                        trusted_market=True,
                        fresh_tick=True,
                    )
                else:
                    reason = str(boundary["reason"])
                    boundary_name = "upper" if reason.startswith("upper_") else "lower"
                    lifecycle_ledger.boundary_action_plan(
                        strategy_session_id=session,
                        strategy_revision_id=revision,
                        boundary=boundary_name,
                        observed_price=current_price,
                        trusted_market=True,
                        fresh_tick=True,
                    )
        terminal_key = f"{session}:{revision}:{boundary['reason']}"
        existing = next(
            (
                row
                for row in reversed(_read_jsonl(self.execution_path))
                if row.get("event") == "terminal_paused" and row.get("terminal_key") == terminal_key
            ),
            None,
        )
        if existing:
            return dict(existing.get("result") or {})
        accepted_ids = {
            str(row.get("order_id") or "")
            for row in snapshot.get("orders") or []
            if str(row.get("state") or "").lower() == "accepted"
            and self._order_owned(row, session, revision, digest)
        }
        if accepted_ids:
            cancel = self.adapter.cancel_orders(
                cycle_id,
                order_ids=sorted(accepted_ids),
                strategy_plan_id=digest,
                ts=observed_at,
                reason=f"park_{boundary['reason']}",
            )
        else:
            cancel = {"status": "idempotent", "cancelled_order_ids": []}
        # A boundary is an explicit, Park-confirmed invalidation trigger. Only
        # positions carrying the exact Park ownership identity are eligible
        # for this terminal close; unrelated exposure is never touched.
        open_positions = [
            dict(row)
            for row in snapshot.get("positions") or []
            if str(row.get("status") or "").lower() == "open"
            and self._position_owned(row, session, revision, digest)
        ]
        exit_receipts: list[dict[str, Any]] = []
        if boundary.get("close_positions"):
            for position in open_positions:
                exit_receipts.append(
                    self.adapter.submit_order(
                        self._exit_command(
                            position,
                            plan=plan,
                            session=session,
                            revision=revision,
                            digest=digest,
                            cycle_id=cycle_id,
                            current_price=current_price,
                            market=market,
                            observed_at=observed_at,
                            reason=str(boundary["reason"]),
                        )
                    )
                )
        if exit_receipts:
            terminal_event = self._market_event(
                market,
                cycle_id=cycle_id,
                observed_at=observed_at,
            )
            terminal_event["event_id"] = _digest(
                {"base_event_id": terminal_event["event_id"], "terminal": terminal_key}
            )
            self.adapter.process_market_event(terminal_event)
        final_snapshot = dict(
            self.adapter.snapshot(
                cycle_id,
                mark_price=current_price,
                mark_fresh=True,
                mark_source=str(market.get("source") or ""),
            )
        )
        final_reconciliation = dict(self.adapter.reconcile(cycle_id))
        remaining_accepted = [
            row
            for row in final_snapshot.get("orders") or []
            if str(row.get("state") or "").lower() == "accepted"
            and self._order_owned(row, session, revision, digest)
        ]
        if remaining_accepted or final_reconciliation.get("status") != "ok" or final_reconciliation.get("issues"):
            return self._blocked(
                "terminal_reconciliation_blocked",
                "owned entries or reconciliation remain unresolved",
                session=session,
                revision=revision,
                digest=digest,
                observed_at=observed_at,
                snapshot=final_snapshot,
                reconciliation=final_reconciliation,
            )
        result = {
            "schema_version": PARK_PAPER_RUNTIME_SCHEMA,
            "status": "paused",
            "session": session,
            "revision": revision,
            "plan_digest": digest,
            "terminal_reason": boundary["reason"],
            "observed_price": current_price,
            "cancel": cancel,
            "exit_receipts": exit_receipts,
            "positions_preserved": sum(
                1
                for row in final_snapshot.get("positions") or []
                if str(row.get("status") or "").lower() == "open"
                and self._position_owned(row, session, revision, digest)
            ),
            "reconciliation": final_reconciliation,
            "paper_only": True,
            "next_action": "await_park_next_strategy",
        }
        self.identity.close_session(
            strategy_session_id=session,
            strategy_revision_id=revision,
            observed_at=observed_at,
            reason=str(boundary["reason"]),
        )
        self.telegram.queue_outbound(
            idempotency_key=f"park-terminal:{session}:{revision}:{boundary['reason']}",
            message_type="park_terminal",
            text=(
                f"Park strategy paused: {boundary['reason']} at {current_price}; "
                f"cancelled={len(cancel.get('cancelled_order_ids') or [])}, "
                f"owned_exits={len(exit_receipts)}, positions_preserved={result['positions_preserved']}, "
                f"reconciliation={final_reconciliation.get('status')}; "
                f"next_action=await_park_next_strategy."
            ),
            binding={"strategy_session_id": session, "strategy_revision_id": revision},
        )
        _append_jsonl(
            self.execution_path,
            {
                "schema_version": PARK_PAPER_RUNTIME_SCHEMA,
                "event": "terminal_paused",
                "terminal_key": terminal_key,
                "strategy_session_id": session,
                "strategy_revision_id": revision,
                "plan_digest": digest,
                "result": result,
                "recorded_at": observed_at,
            },
        )
        record_strategy_snapshot_terminal(
            self.output_root,
            strategy_session_id=session,
            strategy_revision_id=revision,
            plan_digest=digest,
            reason=str(boundary["reason"]),
            observed_at=observed_at,
        )
        facts_failure, facts_blocker = self._record_window_facts_safely(
            {**(self.identity.active_session() or {}), **{"strategy_session_id": session, "strategy_revision_id": revision}},
            observed_at=observed_at,
            status="paused",
            plan=plan,
            market=market,
            snapshot=final_snapshot,
            reconciliation=final_reconciliation,
        )
        if facts_failure:
            result["recording_failures"] = [facts_failure]
            result["recording_blocker"] = facts_blocker
        return result

    def _exit_command(
        self,
        position: Mapping[str, Any],
        *,
        plan: Mapping[str, Any],
        session: str,
        revision: str,
        digest: str,
        cycle_id: str,
        current_price: float,
        market: Mapping[str, Any],
        observed_at: str,
        reason: str,
    ) -> dict[str, Any]:
        side = str(position.get("side") or "").lower()
        target_side = "long" if side in {"long", "buy"} else "short" if side in {"short", "sell"} else ""
        if not target_side:
            raise ParkPaperRuntimeError("position_side_invalid", "owned position side is invalid")
        position_id = _text(position.get("position_id"), "position_id")
        quantity = float(position.get("remaining_units") or position.get("quantity") or 0.0)
        if quantity <= 0:
            raise ParkPaperRuntimeError("position_quantity_invalid", "owned position quantity is invalid")
        return {
            "cycle_id": cycle_id,
            "ts": observed_at,
            "event": "stop" if reason in {
                "stop_price",
                "upper_boundary_invalidated",
                "lower_boundary_invalidated",
            } else "target",
            "side": "sell" if target_side == "long" else "buy",
            "order_type": "market",
            "price": current_price,
            "market_price": current_price,
            "quantity": quantity,
            "source_fill_id": f"park-terminal:{session}:{revision}:{reason}:{position_id}",
            "position_id": position_id,
            "trade_id": str(position.get("trade_id") or ""),
            "target_position_id": position_id,
            "target_position_side": target_side,
            "strategy_plan_id": digest,
            "strategy_plan_version": str(plan.get("schema_version") or ""),
            "strategy_session_id": session,
            "strategy_revision_id": revision,
            "plan_digest": digest,
            "source": "park_telegram",
            "symbol": str(market.get("symbol") or self._paper_instrument_symbol()),
            "instrument_id": str(market.get("instrument_id") or self._paper_instrument_symbol()),
            "market_timestamp": str(market.get("observed_at") or observed_at),
            "market_source": str(market.get("source") or ""),
            "market_fresh": True,
        }

    def _terminal_reason(self, plan: Mapping[str, Any], price: float) -> dict[str, Any] | None:
        normalized = dict(plan.get("normalized_input") or {})
        try:
            upper = float(normalized.get("upper_price_boundary"))
            lower = float(normalized.get("lower_price_boundary"))
        except (TypeError, ValueError):
            return {"reason": "boundary_invalid"}
        direction = str(normalized.get("direction") or "")
        strategy_type = str(normalized.get("strategy_type") or "")
        stop = normalized.get("stop_price")
        target = normalized.get("take_profit_price")
        if strategy_type == "dca":
            if stop in (None, "") or target in (None, ""):
                return None
            stop_value = float(stop)
            target_value = float(target)
            if (direction == "long" and price <= stop_value) or (direction == "short" and price >= stop_value):
                return {"reason": "stop_price", "close_positions": True}
            if (direction == "long" and price >= target_value) or (direction == "short" and price <= target_value):
                return {"reason": "take_profit_price", "close_positions": True}
            return None
        if price >= upper:
            return {
                "reason": "upper_boundary_invalidated",
                "close_positions": True,
            }
        if price <= lower:
            return {
                "reason": "lower_boundary_invalidated",
                "close_positions": True,
            }
        if stop not in (None, ""):
            stop_value = float(stop)
            if (direction == "long" and price <= stop_value) or (direction == "short" and price >= stop_value):
                return {"reason": "stop_price", "close_positions": True}
        if target not in (None, ""):
            target_value = float(target)
            if (direction == "long" and price >= target_value) or (direction == "short" and price <= target_value):
                return {"reason": "take_profit_price", "close_positions": True}
        return None

    def _pending_terminal_action(self, session: str, revision: str) -> dict[str, Any] | None:
        return ParkStrategyLifecycleLedger(self.output_root).pending_terminal_action(
            strategy_session_id=session,
            strategy_revision_id=revision,
        )

    @staticmethod
    def _boundary_from_terminal_action(action: Mapping[str, Any] | None, current_price: float) -> dict[str, Any] | None:
        if not action:
            return None
        trigger = str(action.get("trigger") or "")
        if not trigger:
            boundary = str(action.get("boundary") or "")
            trigger = f"{boundary}_boundary_invalidated" if boundary in {"upper", "lower"} else ""
        if not trigger:
            return None
        return {
            "reason": trigger,
            "close_positions": action.get("position_authority") == "close_strategy_owned_positions",
            "observed_price": action.get("observed_price", current_price),
        }

    def _admission(
        self,
        *,
        market: Mapping[str, Any],
        snapshot: Mapping[str, Any],
        reconciliation: Mapping[str, Any],
        confirmation: Mapping[str, Any],
    ) -> dict[str, Any]:
        evidence = dict(self.safety_evidence_reader() or {})
        evidence.setdefault("trusted_market", market.get("trusted") is True)
        evidence.setdefault("tick_freshness", market.get("fresh") is True)
        evidence.setdefault("stale_cycle_state", evidence.get("unresolved_runtime") is not True)
        evidence.setdefault(
            "reconciliation",
            reconciliation.get("status") == "ok" and not reconciliation.get("issues"),
        )
        evidence.setdefault("park_risk_confirmation", confirmation.get("execution_authorized") is True)
        evidence.setdefault("paper_only", getattr(self.adapter, "name", "") == "nautilus_paper")
        evidence.setdefault(
            "immutable_fill",
            (snapshot.get("capabilities") or {}).get("immutable_fill_guard") is True,
        )
        return evaluate_park_cutover(self.config, safety_evidence=evidence)

    def _paper_instrument_symbol(self) -> str:
        paper_execution = self.config.get("paper_execution")
        instrument = paper_execution.get("instrument") if isinstance(paper_execution, Mapping) else {}
        return str(
            (instrument or {}).get("symbol")
            or (self.config.get("market_data") or {}).get("symbol")
            or "GOLD"
        )

    def _market_event(self, market: Mapping[str, Any], *, cycle_id: str, observed_at: str) -> dict[str, Any]:
        price = float(market.get("price"))
        source = str(market.get("source") or "")
        provider = str(market.get("provider") or source)
        if not source or not provider:
            raise ParkPaperRuntimeError("market_evidence_incomplete", "market source/provider is required")
        timestamp = str(market.get("observed_at") or observed_at)
        return {
            "cycle_id": cycle_id,
            "ts_event": timestamp,
            "event_started_at": str(market.get("event_started_at") or timestamp),
            "price": price,
            "open": market.get("open", price),
            "high": market.get("high", price),
            "low": market.get("low", price),
            "fresh": True,
            "is_synthetic": False,
            "source": source,
            "provider": provider,
            "instrument_id": str(market.get("instrument_id") or market.get("symbol") or self._paper_instrument_symbol()),
            "event_id": _digest({"cycle_id": cycle_id, "ts_event": timestamp, "price": price, "source": source}),
        }

    def _plan_for_digest(self, digest: str) -> dict[str, Any] | None:
        return next(
            (
                dict(row)
                for row in reversed(_read_jsonl(self.output_root / "park_strategy" / "plans.jsonl"))
                if row.get("event") == "plan_proposed" and str(row.get("plan_digest") or "") == digest
            ),
            None,
        )

    def _confirmed_for(self, digest: str, session: str, revision: str) -> dict[str, Any] | None:
        return next(
            (
                dict(row)
                for row in reversed(_read_jsonl(self.output_root / "park_strategy" / "confirmations.jsonl"))
                if row.get("event") == "confirmed"
                and row.get("execution_authorized") is True
                and row.get("plan_digest") == digest
                and row.get("strategy_session_id") == session
                and row.get("strategy_revision_id") == revision
            ),
            None,
        )

    def _release_expired_unconfirmed_session(
        self,
        active: Mapping[str, Any],
        *,
        session: str,
        revision: str,
        digest: str,
        observed_at: str,
    ) -> dict[str, Any] | None:
        pending = self.confirmations.pending_proposals(active)
        if not pending:
            return None
        if len(pending) > 1:
            return self._blocked(
                "multiple_pending_proposals",
                "multiple Park proposals are bound to the active revision",
                session=session,
                revision=revision,
                digest=digest,
                observed_at=observed_at,
            )
        proposal = pending[0]
        try:
            expired = float(proposal.get("expires_at") or 0) <= time.time()
        except (TypeError, ValueError):
            return self._blocked(
                "confirmation_expiry_invalid",
                "Park proposal expiry is invalid",
                session=session,
                revision=revision,
                digest=digest,
                observed_at=observed_at,
            )
        if not expired:
            return None
        cycle_id = park_paper_namespace(session)
        try:
            snapshot = dict(self.adapter.snapshot(cycle_id))
            reconciliation = dict(self.adapter.reconcile(cycle_id))
        except Exception as exc:  # noqa: BLE001 - unknown state cannot be released.
            return self._blocked(
                "paper_state_unavailable",
                type(exc).__name__,
                session=session,
                revision=revision,
                digest=digest,
                observed_at=observed_at,
            )
        open_positions = [
            row for row in snapshot.get("positions") or []
            if str(row.get("status") or "").lower() == "open"
        ]
        accepted_orders = [
            row for row in snapshot.get("orders") or []
            if str(row.get("state") or "").lower() == "accepted"
        ]
        if open_positions or accepted_orders or reconciliation.get("status") != "ok" or reconciliation.get("issues"):
            return self._blocked(
                "confirmation_expired_requires_clean_slate",
                "expired proposal cannot be released while Paper exposure or reconciliation uncertainty remains",
                session=session,
                revision=revision,
                digest=digest,
                observed_at=observed_at,
                snapshot=snapshot,
                reconciliation=reconciliation,
            )
        self.identity.close_session(
            strategy_session_id=session,
            strategy_revision_id=revision,
            observed_at=observed_at,
            reason="confirmation_expired",
        )
        result = self._result(
            "idle",
            observed_at=observed_at,
            session=session,
            revision=revision,
            digest=digest,
            next_action="await_new_park_strategy",
            reason="confirmation_expired",
        )
        self.telegram.queue_outbound(
            idempotency_key=f"park-confirmation-expired:{proposal.get('proposal_id')}",
            message_type="confirmation_expired",
            text="上一个 Paper 计划未确认且已过期；当前仍是 clean slate，系统已释放它。请重新描述策略。",
            binding=None,
        )
        _append_jsonl(
            self.execution_path,
            {
                "schema_version": PARK_PAPER_RUNTIME_SCHEMA,
                "event": "confirmation_expired",
                "strategy_session_id": session,
                "strategy_revision_id": revision,
                "plan_digest": digest,
                "result": result,
                "recorded_at": observed_at,
            },
        )
        return result

    def _order_owned(self, row: Mapping[str, Any], session: str, revision: str, digest: str) -> bool:
        if _owned(row, session, revision, digest):
            return True
        order_id = str(row.get("order_id") or "")
        return any(
            str(item.get("receipt", {}).get("order_id") or item.get("receipt", {}).get("fill_id") or "") == order_id
            and _owned(item, session, revision, digest)
            for item in _read_jsonl(self.execution_path)
            if item.get("event") == "entry_submitted"
        )

    def _position_owned(self, row: Mapping[str, Any], session: str, revision: str, digest: str) -> bool:
        if _owned(row, session, revision, digest):
            return True
        position_id = str(row.get("position_id") or "")
        trade_id = str(row.get("trade_id") or "")
        if str(row.get("strategy_plan_id") or "") == digest and (
            position_id in self._derived_position_ids(session, revision, digest)
            or trade_id in self._derived_position_ids(session, revision, digest)
        ):
            return True
        return any(
            position_id in {
                str(item.get("receipt", {}).get("position_id") or ""),
                str(item.get("receipt", {}).get("trade_id") or ""),
            }
            and _owned(item, session, revision, digest)
            for item in _read_jsonl(self.execution_path)
            if item.get("event") == "entry_submitted"
        )

    def _derived_position_ids(self, session: str, revision: str, digest: str) -> set[str]:
        """Derive Nautilus replay position/trade IDs from Park-owned commands."""

        result: set[str] = set()
        for item in _read_jsonl(self.execution_path):
            if item.get("event") != "entry_submitted" or not _owned(item, session, revision, digest):
                continue
            command = dict(item.get("command") or {})
            source_id = str(command.get("source_fill_id") or "")
            if not source_id:
                continue
            command_id = "nautilus-command-" + hashlib.sha256(source_id.encode("utf-8")).hexdigest()[:20]
            result.update({command_id, f"POS-{command_id}"})
        return result

    def _recording_facts_gate(
        self,
        active: Mapping[str, Any],
        *,
        observed_at: str,
    ) -> tuple[dict[str, str] | None, dict[str, Any] | None]:
        """Return an unresolved current-window facts failure that gates new entries."""

        session = str(active.get("strategy_session_id") or "")
        revision = str(active.get("strategy_revision_id") or "")
        if not session or not revision:
            return None, None
        window_id = str(recording_window(observed_at)["record_window_id"])
        try:
            rows = _read_jsonl(self.blocker_path)
        except Exception as exc:  # noqa: BLE001 - unknown evidence remains fail-closed.
            failure = {
                "record_window_id": window_id,
                "error_type": type(exc).__name__,
                "detail": str(exc)[:300],
            }
            return failure, None
        blocked_indices = [
            index
            for index, row in enumerate(rows)
            if row.get("code") == "recording_facts_blocked"
            and str(row.get("strategy_session_id") or "") == session
            and str(row.get("strategy_revision_id") or "") == revision
            and any(
                str(item.get("record_window_id") or "") == window_id
                for item in row.get("recording_windows") or []
                if isinstance(item, Mapping)
            )
        ]
        if not blocked_indices:
            return None, None
        recovery_indices = [
            index
            for index, row in enumerate(rows)
            if row.get("code") == "recording_facts_recovered"
            and str(row.get("strategy_session_id") or "") == session
            and str(row.get("strategy_revision_id") or "") == revision
            and str(row.get("record_window_id") or "") == window_id
        ]
        latest_blocker_index = blocked_indices[-1]
        if recovery_indices and recovery_indices[-1] > latest_blocker_index:
            return None, None
        latest_blocker = rows[latest_blocker_index]
        failure = {
            "record_window_id": window_id,
            "error_type": "recording_facts_blocked",
            "detail": str(latest_blocker.get("detail") or "recording facts are unresolved")[:300],
        }
        return failure, dict(latest_blocker)

    def _record_recording_recovery_if_needed(
        self,
        active: Mapping[str, Any],
        *,
        observed_at: str,
    ) -> None:
        session = str(active.get("strategy_session_id") or "")
        revision = str(active.get("strategy_revision_id") or "")
        if not session or not revision:
            return
        window_id = str(recording_window(observed_at)["record_window_id"])
        try:
            rows = _read_jsonl(self.blocker_path)
        except Exception:
            return
        blocked_indices = [
            index
            for index, row in enumerate(rows)
            if row.get("code") == "recording_facts_blocked"
            and str(row.get("strategy_session_id") or "") == session
            and str(row.get("strategy_revision_id") or "") == revision
            and any(
                str(item.get("record_window_id") or "") == window_id
                for item in row.get("recording_windows") or []
                if isinstance(item, Mapping)
            )
        ]
        recovery_indices = [
            index
            for index, row in enumerate(rows)
            if row.get("code") == "recording_facts_recovered"
            and str(row.get("strategy_session_id") or "") == session
            and str(row.get("strategy_revision_id") or "") == revision
            and str(row.get("record_window_id") or "") == window_id
        ]
        if blocked_indices and (
            not recovery_indices or recovery_indices[-1] <= blocked_indices[-1]
        ):
            try:
                _append_jsonl(
                    self.blocker_path,
                    {
                        "schema_version": PARK_PAPER_RUNTIME_SCHEMA,
                        "event": "recording_recovered",
                        "code": "recording_facts_recovered",
                        "recorded_at": observed_at,
                        "record_window_id": window_id,
                        "strategy_session_id": session,
                        "strategy_revision_id": revision,
                        "paper_only": True,
                    },
                )
            except Exception:
                # The successful facts write is still retained. If recovery
                # journaling is unavailable, the next tick remains gated by
                # the unresolved blocker rather than opening new exposure.
                return

    def _record_window_facts_safely(
        self,
        active: Mapping[str, Any],
        *,
        observed_at: str,
        status: str,
        plan: Mapping[str, Any],
        market: Mapping[str, Any] | None,
        snapshot: Mapping[str, Any] | None,
        reconciliation: Mapping[str, Any] | None,
    ) -> tuple[dict[str, str] | None, dict[str, Any] | None]:
        """Contain recording-journal failures without changing Paper execution."""

        try:
            self._record_window_facts(
                active,
                observed_at=observed_at,
                status=status,
                plan=plan,
                market=market,
                snapshot=snapshot,
                reconciliation=reconciliation,
            )
        except Exception as exc:  # noqa: BLE001 - recording must not take the engine down.
            window = recording_window(observed_at)
            failure = {
                "record_window_id": str(window["record_window_id"]),
                "error_type": type(exc).__name__,
                "detail": str(exc)[:300],
            }
            blocker = self._record_recording_blocker(
                [failure],
                observed_at=observed_at,
                strategy_session_id=str(active.get("strategy_session_id") or "") or None,
                strategy_revision_id=str(active.get("strategy_revision_id") or "") or None,
                blocker_code="recording_facts_blocked",
            )
            return failure, blocker
        self._record_recording_recovery_if_needed(active, observed_at=observed_at)
        return None, None

    def _record_window_facts(
        self,
        active: Mapping[str, Any],
        *,
        observed_at: str,
        status: str,
        plan: Mapping[str, Any],
        market: Mapping[str, Any] | None,
        snapshot: Mapping[str, Any] | None,
        reconciliation: Mapping[str, Any] | None,
    ) -> None:
        session = str(active.get("strategy_session_id") or "")
        revision = str(active.get("strategy_revision_id") or "")
        if not session or not revision:
            return
        window = recording_window(observed_at)
        self.recording.start_window(
            record_window_id=str(window["record_window_id"]),
            strategy_session_id=session,
            strategy_revision_id=revision,
            starts_at=str(window["starts_at"]),
            ends_at=str(window["ends_at"]),
        )
        snapshot_data = dict(snapshot or {})
        reconciliation_data = dict(reconciliation or {})
        base = {
            "record_window_id": window["record_window_id"],
            "strategy_session_id": session,
            "strategy_revision_id": revision,
        }
        facts = (
            ("control", "runtime_status", {"status": status}),
            ("plan", "plan_proposed", {"plan_digest": plan.get("plan_digest")}),
            ("orders", "snapshot", {"count": len(snapshot_data.get("orders") or [])}),
            ("fills", "snapshot", {"count": len(snapshot_data.get("fills") or [])}),
            ("positions", "snapshot", {"count": len(snapshot_data.get("positions") or []), "open_count": sum(1 for row in snapshot_data.get("positions") or [] if str(row.get("status") or "").lower() == "open")}),
            ("exits", "terminal_or_none", {"status": status}),
            ("telegram", "ledger", {"inbox": len(self.telegram.inbox_rows()), "outbox": len(self.telegram.outbox_rows())}),
            ("provider", "market_read", {"source": (market or {}).get("source"), "elapsed_ms": None}),
            ("market_tick", "observed", dict(market or {})),
            ("runtime", "tick", {"status": status, "engine": getattr(self.adapter, "name", "")}),
            ("reconciliation", "snapshot", reconciliation_data),
            ("execution_path", "mutation_boundary", {"paper_only": True, "legacy_cycle_runner": False}),
        )
        for category, event_type, payload in facts:
            self.recording.record_event(
                **base,
                category=category,
                event_type=event_type,
                source="park_paper_runtime",
                occurred_at=observed_at,
                payload=payload,
            )

    def _close_due_recording_packages(self, observed_at: str) -> list[dict[str, str]]:
        try:
            current = datetime.fromisoformat(str(observed_at).replace("Z", "+00:00"))
        except ValueError:
            return []
        failures: list[dict[str, str]] = []
        try:
            recording_events = self.recording.events()
            recording_packages = self.recording.packages()
        except Exception as exc:  # noqa: BLE001 - corrupt recording must be visible, not crash the tick.
            return [
                {
                    "record_window_id": "unknown",
                    "error_type": type(exc).__name__,
                    "detail": str(exc)[:300],
                }
            ]
        manifests = [row for row in recording_events if row.get("event") == "manifest_started"]
        package_by_window = {
            str(row.get("record_window_id")): row for row in recording_packages
        }
        manifests_by_window: dict[str, list[dict[str, Any]]] = {}
        for manifest in manifests:
            window_id = str(manifest.get("record_window_id") or "")
            if window_id:
                manifests_by_window.setdefault(window_id, []).append(manifest)
        active = self.identity.active_session()
        active_pair = (
            str(active.get("strategy_session_id") or ""),
            str(active.get("strategy_revision_id") or ""),
        ) if active else ("", "")
        for window_id, window_manifests in manifests_by_window.items():
            package_state = package_by_window.get(window_id) or {}
            if (
                package_state.get("status") == "complete"
                and str(package_state.get("review_status") or "complete") == "complete"
            ):
                continue
            try:
                ends = datetime.fromisoformat(
                    str(window_manifests[-1].get("ends_at") or "").replace("Z", "+00:00")
                )
            except ValueError:
                continue
            if ends > current:
                continue
            window_events = [
                row for row in recording_events if row.get("record_window_id") == window_id
            ]
            window_pairs = {
                (
                    str(row.get("strategy_session_id") or ""),
                    str(row.get("strategy_revision_id") or ""),
                )
                for row in window_events
                if str(row.get("strategy_session_id") or "")
                and str(row.get("strategy_revision_id") or "")
            }
            selected = window_manifests[-1]
            if active_pair != ("", "") and active_pair in window_pairs:
                selected_pair = active_pair
                strategy_open = True
            else:
                selected_pair = (
                    str(selected.get("strategy_session_id") or ""),
                    str(selected.get("strategy_revision_id") or ""),
                )
                strategy_open = False
            session, revision = selected_pair
            latest_positions = [
                row
                for row in reversed(window_events)
                if row.get("category") == "positions"
                and (
                    str(row.get("strategy_session_id") or ""),
                    str(row.get("strategy_revision_id") or ""),
                ) == selected_pair
            ]
            if not latest_positions:
                latest_positions = [
                    row for row in reversed(window_events) if row.get("category") == "positions"
                ]
            payload = dict((latest_positions[0] if latest_positions else {}).get("payload") or {})
            package: dict[str, Any] | None = None
            try:
                package = self.recording.close_package(
                    record_window_id=window_id,
                    strategy_session_id=session,
                    strategy_revision_id=revision,
                    strategy_open=strategy_open,
                    positions_open=int(payload.get("open_count") or 0),
                )
                if package.get("status") != "complete":
                    failures.append(
                        {
                            "record_window_id": window_id,
                            "error_type": "recording_incomplete",
                            "detail": ",".join(str(item) for item in package.get("missing_categories") or [])[:300],
                        }
                    )
                review = self.recording.review(record_window_id=window_id)
                _append_jsonl(self.review_path, {"record_window_id": window_id, "review": review, "package": package})
                self.recording.mark_review_complete(record_window_id=window_id)
            except Exception as exc:  # noqa: BLE001 - package/review failure must not mutate execution.
                failures.append(
                    {
                        "record_window_id": window_id,
                        "error_type": type(exc).__name__,
                        "detail": str(exc)[:300],
                    }
                )
                if package is not None:
                    try:
                        self.recording.mark_review_blocked(
                            record_window_id=window_id,
                            error_type=type(exc).__name__,
                            detail=str(exc),
                        )
                    except Exception:
                        # The primary blocker is still returned below; a broken
                        # package journal must never turn into execution mutation.
                        pass
        return failures

    def _blocked(self, code: str, detail: str, **context: Any) -> dict[str, Any]:
        self._revoke_adapter_mutation()
        row = {
            "schema_version": PARK_PAPER_RUNTIME_SCHEMA,
            "event": "runtime_blocked",
            "code": code,
            "detail": str(detail)[:500],
            "recorded_at": context.pop("observed_at", _utc_now()),
            **context,
            "paper_only": True,
            "next_action": "notify_park_and_wait",
        }
        _append_jsonl(self.blocker_path, row)
        session = str(row.get("session") or row.get("strategy_session_id") or "")
        revision = str(row.get("revision") or row.get("strategy_revision_id") or "")
        binding = {"strategy_session_id": session, "strategy_revision_id": revision} if session and revision else None
        self.telegram.queue_outbound(
            idempotency_key=f"park-runtime-blocker:{session}:{revision}:{code}",
            message_type="park_blocker",
            text=f"Park Paper runtime blocked: {code}; next_action=notify_park_and_wait",
            binding=binding,
        )
        return {"schema_version": PARK_PAPER_RUNTIME_SCHEMA, "status": "blocked", **row}

    def _record_recording_blocker(
        self,
        failures: list[dict[str, str]],
        *,
        observed_at: str,
        strategy_session_id: str | None,
        strategy_revision_id: str | None,
        blocker_code: str = "recording_package_blocked",
    ) -> dict[str, Any]:
        row = {
            "schema_version": PARK_PAPER_RUNTIME_SCHEMA,
            "event": "recording_blocked",
            "code": blocker_code,
            "recorded_at": observed_at,
            "strategy_session_id": strategy_session_id,
            "strategy_revision_id": strategy_revision_id,
            "recording_windows": failures,
            "paper_only": True,
            "next_action": "retry_recording_package",
        }
        _append_jsonl(self.blocker_path, row)
        binding = (
            {"strategy_session_id": strategy_session_id, "strategy_revision_id": strategy_revision_id}
            if strategy_session_id and strategy_revision_id
            else None
        )
        for failure in failures:
            window_id = str(failure.get("record_window_id") or "unknown")
            self.telegram.queue_outbound(
                idempotency_key=f"park-recording-blocker:{blocker_code}:{window_id}:{failure.get('error_type')}",
                message_type="park_blocker",
                text=f"Park Paper recording blocked: {window_id}; code={blocker_code}; next_action=retry_recording_package",
                binding=binding,
            )
        return dict(row)

    def _grant_adapter_mutation(
        self,
        *,
        session: str,
        revision: str,
        digest: str,
        cycle_id: str,
        confirmation: Mapping[str, Any],
        gate: Mapping[str, Any],
    ) -> None:
        if not callable(self.mutation_authorizer):
            return
        self.mutation_authorizer(
            _mint_park_paper_capability(
                {
                    "issuer": "ParkPaperRuntime.run_once",
                    "cycle_id": cycle_id,
                    "strategy_session_id": session,
                    "strategy_revision_id": revision,
                    "plan_digest": digest,
                    "park_confirmation_digest": str(confirmation.get("plan_digest") or ""),
                    "cutover_status": str(gate.get("status") or ""),
                }
            )
        )

    def _revoke_adapter_mutation(self) -> None:
        if callable(self.mutation_revoker):
            self.mutation_revoker()

    @staticmethod
    def _result(status: str, **payload: Any) -> dict[str, Any]:
        return {
            "schema_version": PARK_PAPER_RUNTIME_SCHEMA,
            "status": status,
            "paper_only": True,
            **payload,
        }

    @staticmethod
    def _default_market_reader() -> Mapping[str, Any]:
        from services.park_telegram_runtime import default_market_reader

        return default_market_reader()


def build_park_authoritative_adapter(
    output_root: Path,
    *,
    config: Mapping[str, Any] | None = None,
    environ: Mapping[str, str] | None = None,
) -> ParkPaperAdapterBinding:
    """Build only the direct attended Nautilus Paper adapter; never a wrapper."""

    settings = dict(config or load_default_config())
    environment = dict(os.environ if environ is None else environ)
    if settings.get("feature_enabled") is not True:
        raise ParkPaperRuntimeError("park_track_disabled", "Park Strategy Track is disabled")
    if settings.get("runtime_mode") != "paper_only" or settings.get("control_plane") != "telegram":
        raise ParkPaperRuntimeError("park_contract_invalid", "Park runtime contract is not Paper-only Telegram-only")
    engine_settings = dict(settings.get("execution_engine") or {})
    engine = str(engine_settings.get("authoritative") or "nautilus_paper").lower()
    if engine not in {"nautilus", "nautilus_paper"}:
        raise ParkPaperRuntimeError("authoritative_paper_adapter_required", "Park requires nautilus_paper authority")
    runtime_path = str(environment.get("TRADING_ORCHESTRATOR_NAUTILUS_PYTHON") or "").strip()
    if not runtime_path:
        raise ParkPaperRuntimeError("paper_runtime_path_missing", "isolated Nautilus Paper runtime path is required")
    if environment.get("TRADING_ORCHESTRATOR_NAUTILUS_PAPER_SWITCH_APPROVED") != "1":
        raise ParkPaperRuntimeError("paper_switch_unapproved", "attended Paper switch approval is required")
    from services.dualtrack_config import dualtrack_config
    from services.execution_plugin_composition import build_park_direct_paper_adapter
    dualtrack_settings = dualtrack_config()
    configured = dict(dualtrack_settings)
    paper_execution = dict(settings.get("paper_execution") or {})
    configured.update(paper_execution)
    try:
        preflight = build_park_paper_preflight(Path(output_root), settings)
    except ParkPaperPreflightError as exc:
        raise ParkPaperRuntimeError(exc.code, str(exc)) from exc
    configured["paper_fee_model"] = dict(preflight.get("fee_model") or {})
    configured["park_paper_preflight_config_digest"] = str(preflight.get("config_digest") or "")

    try:
        return build_park_direct_paper_adapter(
            Path(output_root),
            config=configured,
            nautilus_python=runtime_path,
            preflight_path=Path(output_root) / "park_strategy" / "paper_preflight_current.json",
            environ=environment,
        )
    except Exception as exc:  # noqa: BLE001 - startup is fail closed.
        raise ParkPaperRuntimeError("paper_adapter_unavailable", type(exc).__name__) from exc
