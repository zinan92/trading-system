"""Deterministic Park Telegram proposal/confirmation worker.

This worker is intentionally a pre-execution seam.  It can create a clean
Park proposal and record an exact confirmation, but it cannot submit an order.
The later Paper execution story must consume the immutable confirmation
capability behind its own cutover gate.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from services.journal_store import load_json, write_json
from services.park_codex_intent_parser import (
    deterministic_legacy_clean_slate_candidate,
    deterministic_neutral_grid_candidate,
)
from services.park_confirmation import (
    ParkConfirmationError,
    ParkConfirmationLedger,
    parse_confirmation_command,
    parse_confirmation_shortcut,
)
from services.park_legacy_cutover import (
    ParkLegacyCutoverError,
    ParkLegacyCutoverLedger,
    parse_legacy_cutover_digest,
)
from services.park_strategy_lifecycle import admit_clean_slate
from services.park_strategy_plan import (
    ParkStrategyPlanError,
    build_deterministic_risk_plan,
    normalize_park_input,
)
from services.park_strategy_session import (
    ParkStrategyIdentityError,
    ParkStrategyIdentityJournal,
    recording_window,
)
from services.park_telegram_control import ParkTelegramControlError, ParkTelegramLedger
from services.telegram_bot_transport import (
    TelegramBotTransport,
    TelegramBotTransportError,
)

PARK_TELEGRAM_RUNTIME_SCHEMA = "park-telegram-runtime-v1"


class ParkTelegramRuntimeError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _digest(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _safe_text(value: Any) -> str:
    return str(value or "").strip()


def _default_cycle_id(now: str) -> str:
    return str(recording_window(now)["record_window_id"])


def default_market_reader() -> dict[str, Any]:
    """Read one trusted market envelope without inventing a current price."""

    try:
        from pipelines.dashboard_server import build_dualtrack_market_bars_response

        source = dict(build_dualtrack_market_bars_response(timeframe="1m", limit=240))
    except Exception as exc:  # noqa: BLE001 - turned into a typed worker blocker.
        raise ParkTelegramRuntimeError("market_unavailable", type(exc).__name__) from exc
    return {
        "price": source.get("latest_close"),
        "trusted": source.get("status") in {"ready", "derived"}
        and source.get("is_synthetic") is False
        and bool(source.get("provider") or source.get("source_mode")),
        "fresh": bool(source.get("fresh")),
        "source": str(source.get("provider") or source.get("source_mode") or ""),
        "observed_at": str(source.get("latest_timestamp") or ""),
        "provider": str(source.get("provider") or ""),
        "raw_status": str(source.get("status") or ""),
    }


def default_account_reader(
    output_root: Path,
    cycle_id: str,
    *,
    config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Read the Park-authoritative Paper snapshot; never use a fake equity.

    Park admission must use the same direct Paper authority that the Park
    runtime will execute against.  The legacy configured execution builder
    carries the DualTrack Shadow cutover gate and is therefore not a valid
    account-read path for this single-track control plane.
    """

    try:
        from services.park_paper_runtime import build_park_authoritative_adapter

        build_kwargs: dict[str, Any] = {"config": config}
        # The initial migration must be able to inspect legacy Paper state
        # while the Park execution flag is still default-off.  The returned
        # adapter remains mutation-gated; this is a read-only composition
        # escape hatch, never an execution authorization.
        if isinstance(config, Mapping) and config.get("feature_enabled") is False:
            build_kwargs["allow_disabled_read"] = True
        adapter = build_park_authoritative_adapter(Path(output_root), **build_kwargs).adapter
        snapshot = dict(adapter.snapshot(cycle_id))
        reconciliation = dict(adapter.reconcile(cycle_id))
    except Exception as exc:  # noqa: BLE001 - turned into a typed worker blocker.
        raise ParkTelegramRuntimeError("paper_account_unavailable", type(exc).__name__) from exc
    positions = [
        dict(row)
        for row in snapshot.get("positions") or []
        if str(row.get("status") or "").lower() == "open"
    ]
    orders = [
        dict(row)
        for row in snapshot.get("orders") or []
        if str(row.get("state") or "").lower() == "accepted"
    ]
    # Clean-slate admission is account-wide, not recording-window-scoped.  A
    # legacy cycle in another namespace must remain visible and block Park;
    # never silently adopt, cancel, or flatten it.
    account_wide_orders: dict[str, dict[str, Any]] = {
        str(row.get("order_id") or f"current-order-{index}"): {
            **dict(row),
            "cycle_id": str(row.get("cycle_id") or cycle_id),
        }
        for index, row in enumerate(orders)
    }
    account_wide_positions: dict[str, dict[str, Any]] = {
        str(row.get("position_id") or row.get("trade_id") or f"current-position-{index}"): dict(row)
        for index, row in enumerate(positions)
    }
    account_wide_reconciliation_ok = reconciliation.get("status") == "ok" and not reconciliation.get("issues")
    adapter_root = getattr(adapter, "output_root", None)
    if adapter_root is not None:
        snapshot_dir = Path(adapter_root) / "dualtrack" / "nautilus_authoritative" / "snapshots"
        for path in sorted(snapshot_dir.glob("*.json")):
            try:
                rows = json.loads(path.read_text(encoding="utf-8"))
                row = rows[-1] if isinstance(rows, list) and rows else rows
                if not isinstance(row, Mapping) or not isinstance(row.get("orders"), list) or not isinstance(row.get("positions"), list):
                    raise ValueError("account_snapshot_shape_invalid")
                snapshot_cycle_id = str(row.get("cycle_id") or path.stem).strip()
                if not snapshot_cycle_id:
                    raise ValueError("account_snapshot_cycle_id_missing")
                for order in row["orders"]:
                    if not isinstance(order, Mapping):
                        raise ValueError("account_snapshot_order_invalid")
                    if str(order.get("state") or "").lower() == "accepted" and str(order.get("order_id") or ""):
                        account_wide_orders[str(order["order_id"])] = {
                            **dict(order),
                            "cycle_id": str(order.get("cycle_id") or snapshot_cycle_id),
                        }
                for position in row["positions"]:
                    if not isinstance(position, Mapping):
                        raise ValueError("account_snapshot_position_invalid")
                    if str(position.get("status") or "").lower() == "open":
                        key = str(position.get("position_id") or position.get("trade_id") or "")
                        if key:
                            account_wide_positions[key] = dict(position)
                try:
                    cycle_reconciliation = dict(adapter.reconcile(snapshot_cycle_id))
                except Exception as exc:  # noqa: BLE001 - unknown account facts block clean-slate admission.
                    raise ParkTelegramRuntimeError("paper_account_reconciliation_invalid", type(exc).__name__) from exc
                account_wide_reconciliation_ok = account_wide_reconciliation_ok and cycle_reconciliation.get("status") == "ok" and not cycle_reconciliation.get("issues")
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                raise ParkTelegramRuntimeError("paper_account_snapshot_invalid", type(exc).__name__) from exc
    account = dict(snapshot.get("account") or {})
    runtime = {}
    try:
        from services.strategy_control_plane import StrategyControlPlane

        runtime = StrategyControlPlane(Path(output_root)).runtime_state(cycle_id)
    except Exception:
        runtime = {}
    last_control_event = runtime.get("last_control_event")
    last_control_event = last_control_event if isinstance(last_control_event, Mapping) else {}
    runtime_after = last_control_event.get("runtime_after")
    runtime_after = runtime_after if isinstance(runtime_after, Mapping) else {}
    proven_stale_record = (
        runtime.get("stale_cycle") is True
        and runtime.get("previous_runtime_unresolved") is False
        and str(runtime_after.get("actual_state") or "") == "stopped"
        and str(runtime_after.get("desired_state") or "") == "stopped"
    )
    unresolved = bool(runtime.get("previous_runtime_unresolved")) or (
        str(runtime.get("actual_state") or "") in {"starting", "running", "replanning", "stopping"}
        and not proven_stale_record
    )
    return {
        "equity": account.get("equity"),
        "reconciliation_healthy": account_wide_reconciliation_ok,
        "open_positions": len(account_wide_positions),
        "open_or_accepted_orders": len(account_wide_orders),
        "unresolved_runtime": unresolved,
        "legacy_runtime_stale_record": proven_stale_record,
        "pending_terminal_actions": False,
        "snapshot": {
            **snapshot,
            "account_wide_legacy_exposure": {
                "orders": list(account_wide_orders.values()),
                "positions": list(account_wide_positions.values()),
                "ownership": "legacy_cycle_or_unknown",
            },
        },
        "reconciliation": reconciliation,
    }


class _Cursor:
    def __init__(self, output_root: Path) -> None:
        self.path = Path(output_root) / "park_strategy" / "telegram_cursor.json"

    def read(self) -> int | None:
        rows = load_json(self.path)
        if not rows or not isinstance(rows[-1], dict):
            return None
        value = rows[-1].get("next_offset")
        return int(value) if value not in (None, "") else None

    def advance(self, update_id: int) -> dict[str, Any]:
        next_offset = int(update_id) + 1
        row = {
            "schema_version": PARK_TELEGRAM_RUNTIME_SCHEMA,
            "event": "cursor_advanced",
            "last_update_id": int(update_id),
            "next_offset": next_offset,
            "recorded_at": _utc_now(),
        }
        write_json(self.path, [row])
        return row


class ParkTelegramRouter:
    """Translate Telegram text into durable Park proposal/receipt facts."""

    def __init__(
        self,
        output_root: Path,
        *,
        park_user_id: str,
        chat_id: str,
        market_reader: Callable[[], Mapping[str, Any]] | None = None,
        account_reader: Callable[[Path, str], Mapping[str, Any]] | None = None,
        now: Callable[[], str] | None = None,
        cycle_id_provider: Callable[[str], str] | None = None,
        confirmation_ttl_seconds: int = 900,
        intent_parser: Any | None = None,
        config: Mapping[str, Any] | None = None,
    ) -> None:
        self.output_root = Path(output_root)
        self.telegram = ParkTelegramLedger(self.output_root, park_user_id=park_user_id, chat_id=chat_id)
        self.result_path = self.output_root / "park_strategy" / "telegram_results.jsonl"
        self.identity = ParkStrategyIdentityJournal(self.output_root)
        self.confirmations = ParkConfirmationLedger(self.output_root, park_user_id=park_user_id)
        self.legacy_cutover = ParkLegacyCutoverLedger(
            self.output_root,
            park_user_id=park_user_id,
            chat_id=chat_id,
        )
        self.market_reader = market_reader or default_market_reader
        self.config = dict(config or {})
        self.account_reader = account_reader or (
            lambda root, cycle: default_account_reader(root, cycle, config=self.config)
        )
        self.now = now or _utc_now
        self.cycle_id_provider = cycle_id_provider or _default_cycle_id
        self.confirmation_ttl_seconds = max(60, int(confirmation_ttl_seconds))
        # Offline/tests may omit this seam.  The production pipeline supplies
        # a bounded Codex CLI parser explicitly; its output is untrusted.
        self.intent_parser = intent_parser
        self.provider_path = self.output_root / "park_strategy" / "provider_calls.jsonl"

    def handle_update(self, update: Mapping[str, Any]) -> dict[str, Any]:
        update_id = update.get("update_id")
        update_digest = _digest(update)
        prior = self._previous_result(update_id)
        if prior is not None:
            prior_digest = str(prior.get("_update_digest") or "")
            result = dict(prior.get("result") or {})
            if prior_digest and prior_digest != update_digest:
                self.telegram.record_rejected_update(
                    update=update,
                    code="duplicate_update_conflict",
                    detail="update_id was previously processed with different content",
                )
                return {
                    "status": "blocked",
                    "code": "duplicate_update_conflict",
                    "next_action": "notify_park_and_wait",
                }
            return result
        active = self.identity.active_session()
        binding = (
            {
                "strategy_session_id": active["strategy_session_id"],
                "strategy_revision_id": active["strategy_revision_id"],
            }
            if active
            else None
        )
        try:
            received = self.telegram.ingest_update(update, binding=binding)
        except ParkTelegramControlError as exc:
            result = self.telegram.record_rejected_update(update=update, code=exc.code, detail=str(exc))
            return self._remember_result(update_id, result, update_digest=update_digest)
        text = _safe_text(received.get("text"))
        legacy_candidate = deterministic_legacy_clean_slate_candidate(text)
        legacy_digest = parse_legacy_cutover_digest(text)
        if legacy_candidate is not None:
            result = self._handle_legacy_cutover(
                text,
                candidate=legacy_candidate,
                active=active,
                update_id=received.get("update_id"),
                text_digest=str(received.get("text_digest") or _digest(text)),
            )
        elif legacy_digest and self.legacy_cutover.proposal_for_digest(legacy_digest):
            result = self._handle_legacy_cutover_confirmation(
                legacy_digest,
                update_id=received.get("update_id"),
            )
        elif text.lower().startswith(("confirm", "reject")) or text.startswith(("确认", "拒绝")):
            result = self._handle_confirmation(text, active=active, update_id=received.get("update_id"))
        else:
            result = self._handle_strategy(text, active=active, update_id=received.get("update_id"))
        return self._remember_result(update_id, result, update_digest=update_digest)

    def recover_pending_legacy_cutovers(self) -> list[dict[str, Any]]:
        """Reprocess an explicit legacy phrase already ingested before a fix.

        Telegram's cursor is intentionally durable.  A provider failure may
        therefore leave a valid Park instruction behind the cursor.  Recovery
        is limited to that exact deterministic phrase and only replaces the
        prior typed parser result; it never replays arbitrary old commands.
        """

        recovered: list[dict[str, Any]] = []
        for received in self.telegram.inbox_rows():
            if received.get("event") != "inbound_received":
                continue
            candidate = deterministic_legacy_clean_slate_candidate(str(received.get("text") or ""))
            if candidate is None:
                continue
            update_id = received.get("update_id")
            prior = self._previous_result(update_id)
            prior_result = dict(prior.get("result") or {}) if prior else {}
            if prior_result.get("status") in {"legacy_cutover_confirmed", "legacy_cutover_proposal_created"}:
                continue
            active = self.identity.active_session()
            result = self._handle_legacy_cutover(
                str(received.get("text") or ""),
                candidate=candidate,
                active=active,
                update_id=update_id,
                text_digest=str(received.get("text_digest") or _digest(str(received.get("text") or ""))),
            )
            self._remember_result(
                update_id,
                result,
                update_digest=str((prior or {}).get("_update_digest") or _digest(received)),
            )
            recovered.append(result)
        return recovered

    def _handle_legacy_cutover(
        self,
        text: str,
        *,
        candidate: Mapping[str, Any],
        active: Mapping[str, Any] | None,
        update_id: Any,
        text_digest: str,
    ) -> dict[str, Any]:
        """Record and, for the bounded operator phrase, confirm exact cleanup."""

        if active:
            return self._block(
                code="legacy_cutover_active_strategy",
                message="已有 Park 策略处于 active，不能把旧挂单清理请求混入策略切换；先等当前策略终态。",
                binding=active,
                idempotency_key=f"park-legacy-cutover-blocked:{update_id}",
            )
        expected = candidate.get("expected_order_count")
        if expected in (None, ""):
            return self._block(
                code="legacy_order_count_missing",
                message="我需要旧挂单的明确数量才能建立精确清理清单。例如：取消这19个旧挂单，确认 clean slate，启用 Park Paper。",
                binding=None,
                idempotency_key=f"park-legacy-cutover-count:{update_id}",
            )
        try:
            expected_count = int(expected)
            facts = dict(self.account_reader(self.output_root, self.cycle_id_provider(self.now())))
            if facts.get("reconciliation_healthy") is not True:
                raise ParkLegacyCutoverError(
                    "legacy_reconciliation_unhealthy",
                    "authoritative Paper reconciliation is not healthy across the discovered namespaces",
                )
            snapshot = facts.get("snapshot") if isinstance(facts.get("snapshot"), Mapping) else {}
            exposure = snapshot.get("account_wide_legacy_exposure") if isinstance(snapshot, Mapping) else {}
            exposure = exposure if isinstance(exposure, Mapping) else {}
            orders = [dict(row) for row in exposure.get("orders") or [] if isinstance(row, Mapping)]
            positions = [dict(row) for row in exposure.get("positions") or [] if isinstance(row, Mapping)]
            proposal = self.legacy_cutover.create_proposal(
                update_id=int(update_id) if update_id not in (None, "") else None,
                source_text_digest=text_digest,
                expected_order_count=expected_count,
                orders=orders,
                positions=positions,
                reconciliation=dict(facts.get("reconciliation") or {}),
            )
            if candidate.get("explicit_confirmation") is True:
                decision = self.legacy_cutover.confirm(
                    proposal,
                    update_id=int(update_id) if update_id not in (None, "") else None,
                    mode="bounded_explicit_clean_slate_phrase",
                )
                ids = ", ".join(str(row.get("order_id") or "") for row in proposal.get("orders") or [])
                self.telegram.queue_outbound(
                    idempotency_key=f"park-legacy-cutover-confirmed:{proposal['proposal_id']}",
                    message_type="legacy_cutover_confirmed",
                    text=(
                        f"已收到 clean slate 指令：将只撤销 {expected_count} 个旧挂单（0 个持仓），不平仓、不反向。\n"
                        f"订单集合：{ids}\n"
                        f"cutover_digest={proposal['proposal_digest']}\n"
                        "下一步会再次核对同一订单集合；若有漂移会自动阻塞。"
                    ),
                    binding=None,
                )
                return {"status": "legacy_cutover_confirmed", "proposal": proposal, "decision": decision}
            self.telegram.queue_outbound(
                idempotency_key=f"park-legacy-cutover-proposal:{proposal['proposal_id']}",
                message_type="legacy_cutover_proposal",
                text=(
                    f"已生成旧挂单精确清理清单：{expected_count} 个挂单、0 个持仓。\n"
                    f"cutover_digest={proposal['proposal_digest']}\n"
                    f"请回复：确认清理旧挂单 {proposal['proposal_digest']}"
                ),
                binding=None,
            )
            return {"status": "legacy_cutover_proposal_created", "proposal": proposal}
        except (ParkLegacyCutoverError, ParkTelegramRuntimeError, ValueError, TypeError) as exc:
            return self._block(
                code=getattr(exc, "code", "legacy_cutover_blocked"),
                message=f"Park Paper clean slate 未接受：{str(exc)}",
                binding=None,
                idempotency_key=f"park-legacy-cutover-rejected:{update_id}",
            )

    def _handle_legacy_cutover_confirmation(self, digest: str, *, update_id: Any) -> dict[str, Any]:
        proposal = self.legacy_cutover.proposal_for_digest(digest)
        if proposal is None:
            return self._block(
                code="legacy_cutover_digest_unknown",
                message="这个 clean-slate digest 已不存在或不是当前待处理清单；系统不会猜测旧订单。",
                binding=None,
                idempotency_key=f"park-legacy-cutover-digest:{update_id}",
            )
        try:
            decision = self.legacy_cutover.confirm(
                proposal,
                update_id=int(update_id) if update_id not in (None, "") else None,
                mode="explicit_cutover_digest",
            )
            self.telegram.queue_outbound(
                idempotency_key=f"park-legacy-cutover-confirmed:{proposal['proposal_id']}",
                message_type="legacy_cutover_confirmed",
                text=(
                    f"已确认精确清理清单 {proposal['proposal_digest']}；下一次 Paper tick 会重新核对后只撤销这些旧挂单。"
                ),
                binding=None,
            )
            return {"status": "legacy_cutover_confirmed", "proposal": proposal, "decision": decision}
        except ParkLegacyCutoverError as exc:
            return self._block(
                code=exc.code,
                message=f"Park Paper clean slate 未确认：{str(exc)}",
                binding=None,
                idempotency_key=f"park-legacy-cutover-confirmation:{update_id}",
            )

    def _previous_result(self, update_id: Any) -> dict[str, Any] | None:
        if update_id in (None, ""):
            return None
        try:
            normalized_id = int(update_id)
        except (TypeError, ValueError):
            return None
        for row in reversed(_read_jsonl(self.result_path)):
            if row.get("update_id") == normalized_id:
                return {
                    "result": dict(row.get("result") or {}),
                    "_update_digest": str(row.get("update_digest") or ""),
                }
        return None

    def _remember_result(
        self,
        update_id: Any,
        result: Mapping[str, Any],
        *,
        update_digest: str,
    ) -> dict[str, Any]:
        try:
            normalized_id = int(update_id) if update_id not in (None, "") else None
        except (TypeError, ValueError):
            normalized_id = None
        if normalized_id is not None:
            _append_jsonl(
                self.result_path,
                {
                    "schema_version": PARK_TELEGRAM_RUNTIME_SCHEMA,
                    "update_id": normalized_id,
                    "update_digest": update_digest,
                    "result": dict(result),
                },
            )
        return dict(result)

    def _handle_strategy(
        self,
        text: str,
        *,
        active: Mapping[str, Any] | None,
        update_id: Any,
    ) -> dict[str, Any]:
        if text.lower() in {"/start", "start", "/help", "help"}:
            return self._block(
                code="strategy_input_help",
                message=self._help_message(),
                binding=None,
                idempotency_key=f"park-strategy-help:{update_id}",
            )
        if active:
            try:
                active = self._release_expired_unconfirmed_session(active)
            except ParkTelegramRuntimeError as exc:
                return self._block(
                    code=exc.code,
                    message=f"Park strategy is blocked: {str(exc)}",
                    binding=active,
                    idempotency_key=f"park-runtime-blocked:{update_id}:{exc.code}",
                )
        if active:
            return self._block(
                code="strategy_locked",
                message="Park strategy is already active and immutable; wait for terminal closure before a new clean-slate strategy.",
                binding=active,
                idempotency_key=f"park-strategy-locked:{update_id}",
            )
        provider: Mapping[str, Any] | None = None
        try:
            parsed = self._parse_intent(text, update_id=update_id)
            provider = parsed.get("metadata") if isinstance(parsed, Mapping) else None
            candidate = parsed.get("candidate") if isinstance(parsed, Mapping) else None
            if isinstance(candidate, Mapping):
                direction = str(candidate.get("direction") or "")
                strategy_type = str(candidate.get("strategy_type") or "")
                if direction == "neutral" and strategy_type not in {"", "grid"}:
                    return self._block(
                        code="neutral_direction_requires_grid",
                        message="我理解到的是‘中性’，但中性只适用于 Grid；请把策略类型说成 Grid。\n\n例如：中性网格，区间 4450~4100，最大20倍杠杆",
                        binding=None,
                        idempotency_key=f"park-neutral-direction:{update_id}",
                        provider=provider,
                    )
                normalized = normalize_park_input(candidate)
            else:
                normalized = normalize_park_input(text)
            if normalized.get("strategy_type") == "dca" and (
                normalized.get("stop_price") in (None, "")
                or normalized.get("take_profit_price") in (None, "")
            ):
                return self._block(
                    code="dca_exit_levels_missing",
                    message=(
                        "DCA 必须明确提供一个策略级止损和一个策略级止盈；系统不会用区间边界猜测。\n\n"
                        "例如：做空 DCA，区间 4444~4200，最大10倍杠杆，止损4450，止盈4210"
                    ),
                    binding=None,
                    idempotency_key=f"park-dca-exits:{update_id}",
                    provider=provider,
                )
            observed_at = self.now()
            cycle_id = self.cycle_id_provider(observed_at)
            facts = dict(self.account_reader(self.output_root, cycle_id))
            admission = admit_clean_slate(facts)
            if not admission.get("admitted"):
                return self._block(
                    code="clean_slate_blocked",
                    message=f"Park strategy not accepted: clean slate blocked by {','.join(admission.get('blockers') or [])}.",
                    binding=None,
                    idempotency_key=f"park-clean-slate:{update_id}",
                )
            market = dict(self.market_reader())
            session_id = f"session-{uuid.uuid4().hex}"
            revision_id = f"revision-{uuid.uuid4().hex}"
            normalized.update(
                {
                    "strategy_session_id": session_id,
                    "strategy_revision_id": revision_id,
                }
            )
            plan = build_deterministic_risk_plan(
                normalized,
                market=market,
                account_equity=facts.get("equity"),
            )
            plan["strategy_session_id"] = session_id
            plan["strategy_revision_id"] = revision_id
            started = self.identity.start_clean_session(
                observed_at=observed_at,
                plan_digest=str(plan["plan_digest"]),
                reconciliation_healthy=bool(facts.get("reconciliation_healthy")),
                open_positions=int(facts.get("open_positions") or 0),
                open_or_accepted_orders=int(facts.get("open_or_accepted_orders") or 0),
                unresolved_runtime=bool(facts.get("unresolved_runtime")),
                pending_terminal_actions=bool(facts.get("pending_terminal_actions")),
                strategy_session_id=session_id,
                strategy_revision_id=revision_id,
            )
            plan_path = self.output_root / "park_strategy" / "plans.jsonl"
            _append_jsonl(plan_path, {"event": "plan_proposed", **plan, "created_at": observed_at})
            proposal_id = f"park-proposal-{str(plan['plan_digest']).removeprefix('sha256:')[:24]}"
            risk_digest = _digest(plan.get("risk") or {})
            proposal = self.confirmations.create_proposal(
                proposal_id=proposal_id,
                strategy_session_id=session_id,
                strategy_revision_id=revision_id,
                plan_digest=str(plan["plan_digest"]),
                risk_digest=risk_digest,
                expires_at=time.time() + self.confirmation_ttl_seconds,
            )
            self.telegram.queue_outbound(
                idempotency_key=f"park-proposal:{proposal_id}",
                message_type="strategy_proposal",
                text=self._format_plan(plan, proposal),
                binding={"strategy_session_id": session_id, "strategy_revision_id": revision_id},
            )
            result: dict[str, Any] = {"status": "proposal_created", "plan": plan, "proposal": proposal, "session": started}
            if provider:
                result["provider"] = dict(provider)
            return result
        except (ParkStrategyPlanError, ParkStrategyIdentityError, ParkTelegramControlError) as exc:
            return self._block(
                code=getattr(exc, "code", "park_strategy_rejected"),
                message=f"Park strategy not accepted: {str(exc)}",
                binding=None,
                idempotency_key=f"park-strategy-rejected:{update_id}",
                provider=provider,
            )
        except ParkTelegramRuntimeError as exc:
            return self._block(
                code=exc.code,
                message=f"Park strategy is blocked: {str(exc)}",
                binding=None,
                idempotency_key=f"park-runtime-blocked:{update_id}:{exc.code}",
                provider=provider,
            )

    def _release_expired_unconfirmed_session(self, active: Mapping[str, Any]) -> Mapping[str, Any] | None:
        pending = self.confirmations.pending_proposals(active)
        if not pending:
            return active
        if len(pending) > 1:
            raise ParkTelegramRuntimeError(
                "multiple_pending_proposals",
                "multiple Park proposals are bound to the active revision",
            )
        proposal = pending[0]
        try:
            expired = float(proposal.get("expires_at") or 0) <= time.time()
        except (TypeError, ValueError):
            raise ParkTelegramRuntimeError(
                "confirmation_expiry_invalid",
                "Park proposal expiry is invalid",
            ) from None
        if not expired:
            return active
        cycle_id = self.cycle_id_provider(self.now())
        try:
            facts = dict(self.account_reader(self.output_root, cycle_id))
        except ParkTelegramRuntimeError:
            raise
        except Exception as exc:  # noqa: BLE001 - expiry release is fail closed.
            raise ParkTelegramRuntimeError("paper_account_unavailable", type(exc).__name__) from exc
        blockers: list[str] = []
        if facts.get("reconciliation_healthy") is not True:
            blockers.append("reconciliation_unhealthy")
        if int(facts.get("open_positions") or 0) != 0:
            blockers.append("open_positions")
        if int(facts.get("open_or_accepted_orders") or 0) != 0:
            blockers.append("open_or_accepted_orders")
        if facts.get("unresolved_runtime"):
            blockers.append("unresolved_runtime")
        if facts.get("pending_terminal_actions"):
            blockers.append("pending_terminal_actions")
        if blockers:
            raise ParkTelegramRuntimeError(
                "confirmation_expired_requires_clean_slate",
                f"expired proposal cannot be released: {','.join(blockers)}",
            )
        session = str(active.get("strategy_session_id") or "")
        revision = str(active.get("strategy_revision_id") or "")
        self.identity.close_session(
            strategy_session_id=session,
            strategy_revision_id=revision,
            observed_at=self.now(),
            reason="confirmation_expired",
        )
        self.telegram.queue_outbound(
            idempotency_key=f"park-confirmation-expired:{proposal.get('proposal_id')}",
            message_type="confirmation_expired",
            text="上一个 Paper 计划未确认且已过期；当前仍是 clean slate，系统已释放它。请重新描述策略。",
            binding=None,
        )
        return None

    def _parse_intent(self, text: str, *, update_id: Any) -> dict[str, Any]:
        if self.intent_parser is None:
            return {
                "status": "deterministic_fallback",
                "metadata": {"provider": "deterministic", "status": "not_configured"},
            }
        try:
            parsed = dict(self.intent_parser.parse(text) or {})
        except Exception as exc:  # noqa: BLE001 - NLU failure is a safe fallback.
            parsed = {
                "status": "unavailable",
                "metadata": {
                    "provider": "codex_cli",
                    "status": "adapter_error",
                    "error_type": type(exc).__name__,
                },
            }
        metadata = dict(parsed.get("metadata") or {})
        metadata.setdefault("provider", "codex_cli")
        if parsed.get("status") == "ok" and isinstance(parsed.get("candidate"), Mapping):
            self._record_provider(update_id=update_id, metadata=metadata)
            return {"status": "ok", "candidate": dict(parsed["candidate"]), "metadata": metadata}
        fallback_candidate = deterministic_neutral_grid_candidate(text)
        if fallback_candidate is not None:
            metadata = {**metadata, "fallback": "deterministic_neutral_grid"}
            self._record_provider(update_id=update_id, metadata=metadata)
            return {"status": "deterministic_fallback", "candidate": fallback_candidate, "metadata": metadata}
        self._record_provider(update_id=update_id, metadata=metadata)
        return {"status": "deterministic_fallback", "metadata": metadata}

    def _record_provider(self, *, update_id: Any, metadata: Mapping[str, Any]) -> None:
        safe = {
            "schema_version": PARK_TELEGRAM_RUNTIME_SCHEMA,
            "event": "provider_call",
            "provider": str(metadata.get("provider") or ""),
            "status": str(metadata.get("status") or ""),
            "elapsed_ms": metadata.get("elapsed_ms"),
            "exit_code": metadata.get("exit_code"),
            "timed_out": bool(metadata.get("timed_out")),
            "stderr_digest": metadata.get("stderr_digest"),
            "error_type": metadata.get("error_type"),
            "update_id": int(update_id) if str(update_id or "").isdigit() else None,
            "recorded_at": _utc_now(),
        }
        _append_jsonl(self.provider_path, safe)

    def _handle_confirmation(
        self,
        text: str,
        *,
        active: Mapping[str, Any] | None,
        update_id: Any,
    ) -> dict[str, Any]:
        if not active:
            return self._block(
                code="confirmation_without_active_strategy",
                message="No active Park proposal is waiting for confirmation.",
                binding=None,
                idempotency_key=f"park-confirmation-no-active:{update_id}",
            )
        try:
            mode = "exact_digest"
            try:
                verb, digest = parse_confirmation_command(text)
            except ParkConfirmationError as exact_error:
                try:
                    verb = parse_confirmation_shortcut(text)
                except ParkConfirmationError:
                    raise exact_error
                digest = ""
                mode = "pending_proposal_shortcut"
            if digest:
                proposal = next(
                    (
                        row
                        for row in reversed(self.confirmations.rows())
                        if row.get("event") == "proposal" and str(row.get("plan_digest") or "").lower() == digest
                    ),
                    None,
                )
            else:
                pending = self.confirmations.pending_proposals(active)
                if len(pending) != 1:
                    if pending and all(float(row.get("expires_at") or 0) <= time.time() for row in pending):
                        raise ParkConfirmationError("confirmation_expired", "the current Park proposal has expired; resend the strategy")
                    raise ParkConfirmationError("ambiguous_pending_proposals", "there is not exactly one pending Park proposal")
                proposal = pending[0]
                digest = str(proposal.get("plan_digest") or "").lower()
            if not proposal:
                raise ParkConfirmationError("proposal_missing", "proposal digest is unknown")
            decision = self.confirmations.decide(
                proposal_id=str(proposal["proposal_id"]),
                park_user_id=self.telegram.park_user_id,
                command_text=f"{verb} {digest}",
                current_binding=active,
                now=time.time(),
            )
            event = str(decision.get("event") or "decision")
            self.telegram.queue_outbound(
                idempotency_key=f"park-confirmation:{proposal['proposal_id']}:{event}",
                message_type="confirmation_receipt",
                text=(
                    f"Park {event}: {proposal['plan_digest']}. "
                    + ("Paper execution is authorized for the next trusted fresh tick; no live order will be sent." if event == "confirmed" else "No execution will be attempted; send a new clean-slate strategy after closure.")
                ),
                binding=active,
            )
            return {"status": event, "decision": decision, "confirmation_mode": mode}
        except ParkConfirmationError as exc:
            return self._block(
                code=exc.code,
                message=f"Park confirmation rejected: {str(exc)}",
                binding=active,
                idempotency_key=f"park-confirmation-rejected:{update_id}",
            )

    def _block(
        self,
        *,
        code: str,
        message: str,
        binding: Mapping[str, Any] | None,
        idempotency_key: str,
        provider: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        user_message = self._friendly_block_message(code, message)
        outbound = self.telegram.queue_outbound(
            idempotency_key=idempotency_key,
            message_type="park_blocker",
            text=user_message,
            binding=binding,
        )
        result: dict[str, Any] = {
            "status": "blocked",
            "code": code,
            "next_action": "notify_park_and_wait",
            "outbound": outbound,
        }
        if provider:
            result["provider"] = dict(provider)
        return result

    @staticmethod
    def _help_message() -> str:
        return (
            "直接用自然语言描述策略即可，我会先复述理解并计算 Paper 风险，不会直接下单。\n\n"
            "例如：\n"
            "1) 做空 DCA，价格区间 4444~4200，最大10倍杠杆，止损=……，止盈=……\n"
            "2) 中性网格，区间 4450~4100，最大20倍杠杆\n\n"
            "当前价格由系统读取；只有你确认精确计划后才会执行。"
        )

    @staticmethod
    def _neutral_grid_message(candidate: Mapping[str, Any]) -> str:
        upper = candidate.get("upper_price_boundary")
        lower = candidate.get("lower_price_boundary")
        leverage = candidate.get("maximum_leverage")
        maximum_loss = candidate.get("maximum_acceptable_loss")
        risk_text = (
            f"最大杠杆 {leverage}x"
            if leverage is not None
            else f"最大可接受亏损 {maximum_loss}"
        )
        return (
            "我理解你的意思是：中性网格"
            f"，区间 {lower}~{upper}，{risk_text}。\n"
            "中性 Grid 会拆成明确的买入腿和卖出腿；系统会先读取可信当前价，"
            "计算双边风险并发回规范化计划。确认前不会下单，也不会改动持仓。\n\n"
            "确认格式示例：confirm <plan_digest>；拒绝格式示例：reject <plan_digest>。"
        )

    @staticmethod
    def _friendly_block_message(code: str, message: str) -> str:
        if code == "strategy_input_help":
            return message
        if code == "missing_direction":
            return (
                "我还没读清楚你的方向。你可以直接说：做多、做空，或‘中性网格’。\n\n"
                "例如：\n"
                "1) 做空 DCA，区间 4444~4200，最大10倍杠杆\n"
                "2) 做多 Grid，区间 4450~4100，最大20倍杠杆\n\n"
                "当前价格我会自动读取，确认前不会下单。"
            )
        if code == "missing_strategy_type":
            return "我还没读清楚你要 DCA 还是 Grid。比如：做空 DCA，区间 4444~4200，最大10倍杠杆。"
        if code == "missing_price_boundary":
            return "我还缺价格区间。请像这样说：做空 DCA，区间 4444~4200，最大10倍杠杆。"
        if code == "missing_risk_authority":
            return "我还缺风险上限。请补充最大杠杆或最大可接受亏损，例如：最大10倍杠杆。"
        if code == "confirmation_incomplete":
            return "可以直接回复‘确认当前计划’或‘拒绝当前计划’；也可以回复 confirm <plan_digest>。"
        if code == "confirmation_expired":
            return "这个 Paper 计划已经过期，请重新发送策略；过期计划不会执行。"
        if code == "ambiguous_pending_proposals":
            return "当前有多个待确认计划，不能猜测你要确认哪一个；请使用计划摘要确认。"
        if code == "confirmation_expired_requires_clean_slate":
            return "上一个计划已过期，但账户状态不是 clean slate；系统不会自动释放它，请先处理阻塞状态。"
        return message

    @staticmethod
    def _format_plan(plan: Mapping[str, Any], proposal: Mapping[str, Any]) -> str:
        risk = dict(plan.get("risk") or {})
        normalized = dict(plan.get("normalized_input") or {})
        neutral_detail = ""
        if normalized.get("direction") == "neutral":
            legs = dict(risk.get("legs") or {})
            neutral_detail = (
                f"neutral_legs=buy→{legs.get('long', {}).get('boundary')} / "
                f"sell→{legs.get('short', {}).get('boundary')}\n"
            )
        grid_detail = ""
        if normalized.get("strategy_type") == "grid":
            geometry = dict(risk.get("grid_entry_range") or {})
            hard_stop = risk.get("hard_stop")
            grid_detail = (
                f"entry_range={geometry.get('lower')}~{geometry.get('upper')} "
                f"spacing={risk.get('grid_spacing')} rungs={risk.get('order_count')}\n"
                f"grid_hard_stop={hard_stop} tp_geometry=next_rung_then_boundary "
                f"rung_prices={risk.get('grid_rung_prices')}\n"
            )
        return "".join(
            (
                "Park proposal (Paper-only; confirm this plan in plain language or with its digest)\n",
                f"direction={normalized.get('direction')} type={normalized.get('strategy_type')}\n",
                f"range={normalized.get('upper_price_boundary')}~{normalized.get('lower_price_boundary')} current={dict(plan.get('market') or {}).get('price')}\n",
                neutral_detail,
                grid_detail,
                f"max_notional={risk.get('maximum_notional')} effective_leverage={risk.get('effective_leverage')}x\n",
                f"theoretical_max_loss={risk.get('theoretical_max_loss')} order_count={risk.get('order_count')} quantity_each={risk.get('per_order_quantity')}\n",
                f"risk_digest={proposal.get('risk_digest')}\n",
                f"plan_digest={proposal.get('plan_digest')}\n",
                f"Reply: 确认当前计划 / 拒绝当前计划；or confirm {proposal.get('plan_digest')}",
            )
        )

    def drain_outbound(self, transport: TelegramBotTransport) -> dict[str, Any]:
        delivered = 0
        failed = 0
        rows: list[dict[str, Any]] = []
        for message in self.telegram.pending_outbound():
            try:
                result = transport.send_message(str(message.get("text") or ""), chat_id=str(message.get("chat_id") or ""))
            except TelegramBotTransportError as exc:
                result = {"ok": False, "reason": exc.code}
            receipt = self.telegram.record_send_result(
                message_id=str(message.get("message_id") or ""),
                transport_result=result,
            )
            rows.append(receipt)
            if receipt.get("status") == "delivered":
                delivered += 1
            else:
                failed += 1
        return {"delivered": delivered, "failed": failed, "messages": rows}


class ParkTelegramWorker:
    """One bounded polling pass with cursor and single-process lease."""

    def __init__(self, router: ParkTelegramRouter, *, timeout_seconds: int = 20) -> None:
        self.router = router
        self.timeout_seconds = int(timeout_seconds)
        self.cursor = _Cursor(router.output_root)
        self.lock_path = router.output_root / "park_strategy" / "telegram_worker.lock"

    def run_once(self, transport: TelegramBotTransport) -> dict[str, Any]:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+", encoding="utf-8") as lock:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise ParkTelegramRuntimeError("worker_already_running", "another Park Telegram worker holds the lease") from exc
            try:
                offset = self.cursor.read()
                recovered = self.router.recover_pending_legacy_cutovers()
                updates = transport.get_updates(offset=offset, timeout_seconds=self.timeout_seconds)
                handled: list[dict[str, Any]] = list(recovered)
                for update in updates:
                    handled.append(self.router.handle_update(update))
                    if update.get("update_id") not in (None, ""):
                        self.cursor.advance(int(update["update_id"]))
                delivery = self.router.drain_outbound(transport)
                return {
                    "schema_version": PARK_TELEGRAM_RUNTIME_SCHEMA,
                    "status": "pass",
                    "updates_received": len(updates),
                    "updates_handled": handled,
                    "next_offset": self.cursor.read(),
                    "delivery": delivery,
                    "control_actions_executed": 0,
                    "orders_created": 0,
                    "positions_created": 0,
                    "paper_only": True,
                }
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _append_jsonl(path: Path, row: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = (json.dumps(dict(row), ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
    fd, temp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())
        with path.open("ab") as destination:
            destination.write(line)
            destination.flush()
            os.fsync(destination.fileno())
        os.unlink(temp)
    except Exception:
        try:
            os.unlink(temp)
        except FileNotFoundError:
            pass
        raise


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
