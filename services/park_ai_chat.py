"""Dashboard AI strategy copilot for the Park Paper track.

The copilot is deliberately a proposal boundary, not an execution adapter.
It accepts natural-language Park intent, asks for missing or ambiguous facts,
and creates a short-lived draft.  Only the explicitly confirmed draft is
promoted to the existing Park plan/confirmation ledgers.  Provider output is
untrusted and is always passed through the deterministic Park normalizer and
risk planner before it can become confirmable.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import threading
import time
import uuid
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from typing import Any, Callable, Mapping
from services.park_codex_intent_parser import CodexCliIntentParser
from services.park_confirmation import ParkConfirmationLedger
from services.park_strategy_plan import (
    ParkStrategyPlanError,
    build_deterministic_risk_plan,
    normalize_park_input,
)
from services.park_strategy_session import ParkStrategyIdentityJournal, recording_window
from services.park_recording_track import ParkRecordingTrack
from services.park_strategy_snapshot import (
    PARK_AI_SNAPSHOT_EVENT_SCHEMA,
    record_strategy_snapshot_terminal,
)
from services.dashboard_ai_provider import (
    DEFAULT_DEEPSEEK_MODEL,
    DEFAULT_DEEPSEEK_TIMEOUT_SECONDS,
    DEFAULT_DEEPSEEK_URL,
    DeepSeekIntentProvider,
    _safe_context,
)


PARK_AI_CHAT_SCHEMA = "park-dashboard-ai-chat-v1"
PARK_AI_SNAPSHOT_SCHEMA = "park-strategy-snapshot-v1"
DEFAULT_DRAFT_TTL_SECONDS = 30 * 60
MAX_MESSAGE_CHARS = 8_000
_AI_CHAT_LOCK = threading.RLock()


def _serialized(method):
    """Serialize Dashboard chat journal mutations within one worker process."""

    @wraps(method)
    def wrapped(self, *args, **kwargs):
        with _AI_CHAT_LOCK:
            result = method(self, *args, **kwargs)
            if method.__name__ in {"handle_message", "confirm", "reject"} and isinstance(result, Mapping):
                self._record_assistant_result(result, actor=kwargs.get("actor"))
            return result

    return wrapped


class ParkAiChatError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _text(value: Any, field: str) -> str:
    result = str(value or "").strip()
    if not result:
        raise ParkAiChatError("invalid_input", f"{field} is required")
    return result


def _digest(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _confirmation_receipt_digest(proposal_id: str, plan_digest: str, confirmed_at: float) -> str:
    return "sha256:" + hashlib.sha256(
        f"{proposal_id}|{plan_digest}|confirmed|{confirmed_at}".encode("utf-8")
    ).hexdigest()


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ParkAiChatError("journal_corrupt", f"{path.name} row is not an object")
        rows.append(value)
    return rows


def _append_jsonl(path: Path, row: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(dict(row), ensure_ascii=False, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(dict(value), ensure_ascii=False, sort_keys=True), encoding="utf-8")
    os.replace(temporary, path)


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    value = json.loads(path.read_text(encoding="utf-8"))
    return dict(value) if isinstance(value, dict) else None


class ParkAiProviderGateway:
    """DeepSeek-first provider chain with Codex CLI fallback."""

    def __init__(
        self,
        *,
        deepseek: Any | None = None,
        codex: Any | None = None,
    ) -> None:
        self.deepseek = deepseek or DeepSeekIntentProvider()
        configured_codex = (
            os.environ.get("PARK_CODEX_CLI")
            or os.environ.get("CODEX_CLI")
            or shutil.which("codex")
            or "/opt/homebrew/bin/codex"
        )
        self.codex = codex or CodexCliIntentParser(
            executable=configured_codex,
            model="gpt-5.6-sol",
            timeout_seconds=float(os.environ.get("PARK_CODEX_FALLBACK_TIMEOUT_SECONDS", "15")),
            cwd=os.environ.get("PARK_CODEX_CWD") or None,
            codex_home=os.environ.get("CODEX_HOME") or None,
        )

    def parse(self, text: str, *, context: Mapping[str, Any] | None = None) -> dict[str, Any]:
        first = self._call(self.deepseek, text, context=context)
        if first.get("status") == "ok":
            return first
        first_metadata = dict(first.get("metadata") or {})
        second = self._call(self.codex, text, context=context)
        metadata = dict(second.get("metadata") or {})
        metadata.setdefault("fallback_from", str(first_metadata.get("provider") or "deepseek"))
        metadata["fallback_status"] = first_metadata.get("status")
        metadata["fallback_elapsed_ms"] = first_metadata.get("elapsed_ms")
        second["metadata"] = metadata
        return second

    @staticmethod
    def _call(provider: Any, text: str, *, context: Mapping[str, Any] | None) -> dict[str, Any]:
        try:
            result = provider.parse(text, context=context)
        except TypeError:
            # Existing Codex parser has a text-only seam.
            try:
                result = provider.parse(text)
            except Exception as exc:  # noqa: BLE001 - fallback failure is fail-closed.
                return {
                    "status": "unavailable",
                    "metadata": {
                        "provider": provider.__class__.__name__,
                        "status": "adapter_error",
                        "error_type": type(exc).__name__,
                    },
                }
        except Exception as exc:  # noqa: BLE001 - provider failure is a safe fallback.
            return {"status": "unavailable", "metadata": {"provider": provider.__class__.__name__, "status": "adapter_error", "error_type": type(exc).__name__}}
        return dict(result or {})


def _disposition_from_text(text: str) -> dict[str, Any] | None:
    """Parse only explicit old-portfolio handling phrases.

    This is not a strategy parser.  It intentionally refuses to infer an
    action from vague text; the normalized result is shown back to Park before
    any replacement can be confirmed.
    """

    source = str(text or "").strip()
    lowered = source.lower()
    if not source:
        return None
    position_action = None
    if any(token in source for token in ("平仓", "清仓", "flatten")) or "close position" in lowered:
        position_action = "flatten"
    elif any(token in source for token in ("继续", "保留", "不动")) or "keep" in lowered:
        if any(token in source for token in ("仓", "持仓", "止盈", "止损")) or "position" in lowered:
            position_action = "keep"
    entry_action = None
    if any(token in source for token in ("撤掉", "撤销", "取消", "撤单")) or "cancel" in lowered:
        if any(token in source for token in ("挂单", "入口", "订单", "order", "entry")) or "order" in lowered:
            entry_action = "cancel"
    elif any(token in source for token in ("保留挂单", "保留订单")) or "keep order" in lowered:
        entry_action = "keep"
    exit_action = "keep" if any(token in source for token in ("止盈", "止损")) or "take profit" in lowered or "stop loss" in lowered else None
    if position_action is None and entry_action is None and exit_action is None:
        return None
    return {
        "source_text": source,
        "position_action": position_action or "unspecified",
        "entry_action": entry_action or "unspecified",
        "exit_action": exit_action or "unspecified",
    }


def _disposition_from_candidate(candidate: Mapping[str, Any], source_text: str) -> dict[str, Any] | None:
    """Accept only provider disposition fields from a strict small vocabulary."""

    def action(name: str, aliases: Mapping[str, str]) -> str:
        value = str(candidate.get(name) or "").strip().lower()
        return aliases.get(value, "unspecified")

    position = action(
        "position_action",
        {
            "keep": "keep",
            "preserve": "keep",
            "flatten": "flatten",
            "close": "flatten",
            "close_all": "flatten",
        },
    )
    entry = action(
        "entry_action",
        {
            "keep": "keep",
            "preserve": "keep",
            "cancel": "cancel",
            "cancel_entries": "cancel",
            "cancel_orders": "cancel",
        },
    )
    exit_action = action(
        "exit_action",
        {"keep": "keep", "preserve": "keep", "manage": "keep"},
    )
    if position == entry == exit_action == "unspecified":
        return None
    return {
        "source_text": str(source_text),
        "position_action": position,
        "entry_action": entry,
        "exit_action": exit_action,
    }


def _disposition_complete(disposition: Mapping[str, Any] | None) -> bool:
    """Require an explicit decision for positions, entries, and exits."""

    if not isinstance(disposition, Mapping):
        return False
    return (
        str(disposition.get("position_action") or "") in {"keep", "flatten"}
        and str(disposition.get("entry_action") or "") in {"keep", "cancel"}
        and str(disposition.get("exit_action") or "") == "keep"
    )


class ParkAiChatService:
    """Stateful Dashboard chat seam shared by API tests and the HTTP handler."""

    def __init__(
        self,
        output_root: Path,
        *,
        park_user_id: str,
        provider: Any | None = None,
        market_reader: Callable[[], Mapping[str, Any]] | None = None,
        account_reader: Callable[[Path, str], Mapping[str, Any]] | None = None,
        now: Callable[[], str] | None = None,
        draft_ttl_seconds: int = DEFAULT_DRAFT_TTL_SECONDS,
    ) -> None:
        self.output_root = Path(output_root)
        self.park_user_id = _text(park_user_id, "park_user_id")
        self.provider = provider or ParkAiProviderGateway()
        self.market_reader = market_reader or self._default_market_reader
        self.account_reader = account_reader or self._default_account_reader
        self.now = now or _utc_now
        self._time = time.time
        self.draft_ttl_seconds = max(60, int(draft_ttl_seconds))
        self.draft_path = self.output_root / "park_strategy" / "dashboard_ai_draft.json"
        self.chat_path = self.output_root / "park_strategy" / "dashboard_ai_chat.jsonl"
        self.snapshot_path = self.output_root / "park_strategy" / "strategy_snapshots.jsonl"
        self.identity = ParkStrategyIdentityJournal(self.output_root)

    @_serialized
    def handle_message(self, message: str, *, actor: str | None = None) -> dict[str, Any]:
        text = _text(message, "message")
        if len(text) > MAX_MESSAGE_CHARS:
            raise ParkAiChatError("message_too_large", f"message exceeds {MAX_MESSAGE_CHARS} characters")
        draft = self._load_draft()
        active = self.identity.active_session()
        self._record_chat_event(
            "message_received",
            {
                "message": text,
                "draft_id": draft.get("draft_id") if draft else None,
                "actor": actor,
            },
        )
        if draft and draft.get("requires_disposition"):
            disposition = _disposition_from_text(text)
            disposition_metadata: dict[str, Any] = {}
            if disposition is None:
                disposition_result = ParkAiProviderGateway._call(
                    self.provider,
                    text,
                    context=self._read_context(active),
                )
                disposition_metadata = dict(disposition_result.get("metadata") or {})
                self._record_provider(disposition_metadata, actor=actor)
                if disposition_result.get("status") == "ok" and isinstance(
                    disposition_result.get("candidate"), Mapping
                ):
                    disposition = _disposition_from_candidate(
                        disposition_result["candidate"],
                        text,
                    )
            if disposition is not None:
                if not _disposition_complete(disposition):
                    draft["disposition"] = disposition
                    draft["clarification_code"] = "portfolio_disposition_incomplete"
                    draft["confirmable"] = False
                    self._save_draft(draft)
                    return {
                        "status": "needs_disposition",
                        "code": "portfolio_disposition_incomplete",
                        "message": "旧仓处置还不完整。请同时明确：持仓保留/平仓、未成交入口保留/撤销、止盈止损保留（例如：旧仓继续止盈止损，撤掉未成交挂单）。",
                        "draft": self._public_draft(draft),
                        "confirmable": False,
                        "paper_only": True,
                    }
                if disposition_metadata:
                    draft["provider"] = disposition_metadata
                draft["disposition"] = disposition
                self._record_event_for_draft(
                    draft,
                    category="control",
                    event_type="portfolio_disposition_received",
                    payload={"draft_id": draft.get("draft_id"), "actor": actor},
                )
                result = self._complete_draft(draft, actor=actor)
                if result.get("status") != "needs_clarification":
                    self._save_draft(draft)
                return result
            context = self._read_context(active)
            return {
                "status": "needs_disposition",
                "code": "portfolio_disposition_required",
                "message": self._disposition_prompt(context),
                "draft": self._public_draft(draft),
                "confirmable": False,
                "paper_only": True,
            }
        combined = "\n".join(item for item in [str(draft.get("source_text") or "") if draft else "", text] if item).strip()
        context = self._read_context(active)
        has_existing_exposure = self._has_existing_exposure(context.get("account") or {})
        provider_result = ParkAiProviderGateway._call(self.provider, combined, context=context)
        if provider_result.get("status") != "ok" or not isinstance(provider_result.get("candidate"), Mapping):
            metadata = dict(provider_result.get("metadata") or {})
            self._record_provider(metadata, actor=actor)
            return {
                "status": "provider_unavailable",
                "code": "ai_provider_unavailable",
                "message": "DeepSeek 暂时不可用，Codex fallback 也没有返回可验证结果。你可以继续使用原来的手工 Paper 表单；不会创建策略或下单。",
                "provider": metadata,
                "manual_fallback": True,
                "confirmable": False,
            }
        metadata = dict(provider_result.get("metadata") or {})
        self._record_provider(metadata, actor=actor)
        candidate = dict(provider_result["candidate"])
        candidate.setdefault("source_text", combined)
        provider_needs_clarification = candidate.get("needs_clarification") in {
            True,
            1,
            "1",
            "true",
            "True",
            "yes",
        }
        provider_fields = candidate.get("clarification_fields")
        if isinstance(provider_fields, str):
            provider_fields = [provider_fields]
        if provider_needs_clarification or provider_fields:
            fields = ", ".join(str(item) for item in provider_fields or [] if str(item).strip())
            detail = f"AI 标记仍需明确：{fields}" if fields else "AI 无法安全确认全部参数"
            draft = draft or self._new_draft(
                combined,
                candidate,
                active=active or has_existing_exposure,
                provider=metadata,
            )
            draft.update(
                {
                    "source_text": combined,
                    "normalized": candidate,
                    "provider": metadata,
                    "requires_disposition": bool(active or has_existing_exposure),
                    "clarification_code": "provider_clarification",
                    "clarification_detail": detail,
                    "confirmable": False,
                }
            )
            self._save_draft(draft)
            return self._clarification(
                "provider_clarification",
                detail,
                source_text=combined,
                provider=metadata,
                draft=draft,
            )
        try:
            normalized = normalize_park_input(candidate)
        except ParkStrategyPlanError as exc:
            draft = draft or self._new_draft(
                combined,
                candidate,
                active=active or has_existing_exposure,
                provider=metadata,
            )
            draft["source_text"] = combined
            draft["normalized"] = candidate
            draft["provider"] = metadata
            draft["requires_disposition"] = bool(active or has_existing_exposure)
            draft["clarification_code"] = exc.code
            draft["clarification_detail"] = str(exc)
            draft["confirmable"] = False
            self._save_draft(draft)
            self._record_event_for_draft(
                draft,
                category="control",
                event_type="strategy_clarification",
                payload={"draft_id": draft.get("draft_id"), "code": exc.code},
            )
            return self._clarification(
                exc.code,
                str(exc),
                source_text=combined,
                provider=metadata,
                draft=draft,
            )
        draft = draft or self._new_draft(combined, normalized, active=active or has_existing_exposure, provider=metadata)
        draft["source_text"] = combined
        draft["normalized"] = normalized
        draft["provider"] = metadata
        draft["requires_disposition"] = bool(active or has_existing_exposure)
        if active:
            return self._complete_draft(draft, actor=actor)
        result = self._complete_draft(draft, actor=actor)
        self._record_event_for_draft(
            draft,
            category="control",
            event_type="strategy_draft_updated",
            payload={
                "draft_id": draft.get("draft_id"),
                "confirmable": bool(result.get("confirmable")),
                "status": result.get("status"),
            },
        )
        if result.get("status") != "needs_clarification":
            self._save_draft(draft)
        return result

    @_serialized
    def confirm(self, draft_id: str, plan_digest: str, *, actor: str | None = None) -> dict[str, Any]:
        draft = self._load_draft()
        if not draft or str(draft.get("draft_id")) != str(draft_id):
            existing = next(
                (
                    row
                    for row in reversed(_read_jsonl(self.snapshot_path))
                    if str(row.get("draft_id") or "") == str(draft_id)
                    and str(row.get("plan_digest") or "").lower() == str(plan_digest or "").lower()
                ),
                None,
            )
            if existing:
                return {"status": "confirmed", "snapshot": self._public_snapshot(existing), "idempotent": True, "paper_only": True}
            return self._blocked("draft_missing", "这个 Draft 已不存在，请重新描述策略。")
        if self._expired(draft):
            self._record_event_for_draft(
                draft,
                category="control",
                event_type="strategy_draft_expired",
                payload={"draft_id": draft.get("draft_id")},
            )
            self._delete_draft()
            return self._blocked("confirmation_expired", "这个 Draft 已超过 30 分钟，请重新描述策略。")
        expected = str(draft.get("plan_digest") or "").lower()
        if not expected or str(plan_digest or "").lower() != expected:
            return self._blocked("plan_digest_mismatch", "确认对象与当前策略 Draft 不一致。")
        if not draft.get("confirmable"):
            return self._blocked("draft_not_confirmable", "当前 Draft 仍缺少必要信息或旧仓处理方式。")
        active = self.identity.active_session()
        context = self._read_context(active)
        # Rebuild from the latest trusted market/account facts.  A stale UI
        # card cannot authorize a mutation.
        try:
            plan = self._build_plan(draft, context)
        except ParkStrategyPlanError as exc:
            return self._blocked(exc.code, str(exc))
        if str(plan.get("plan_digest")) != expected:
            return self._blocked("plan_changed", "最新行情或组合风险使计划 digest 发生变化，请重新确认。")
        if self._has_existing_exposure(context.get("account") or {}):
            return self._blocked(
                "portfolio_reconciliation_required",
                "已有 orders/positions 尚未完成归属处理；系统不会静默切换执行身份。请先完成处置并重新确认。",
            )
        if active:
            # The existing Paper runtime has one active execution identity.
            # A web draft may record the requested disposition, but it must
            # never close or supersede that identity implicitly.  The old
            # session remains authoritative until its own terminal boundary
            # path records closure and the next proposal starts clean.
            return self._blocked(
                "active_strategy_not_terminal",
                "当前策略仍处于 active；处置意图已记录，但必须等旧策略进入 terminal/paused 后才能确认新策略。",
            )
        session_id = str(draft.get("strategy_session_id") or f"session-{uuid.uuid4().hex}")
        revision_id = str(draft.get("strategy_revision_id") or f"revision-{uuid.uuid4().hex}")
        plan["strategy_session_id"] = session_id
        plan["strategy_revision_id"] = revision_id
        if str(plan.get("plan_digest") or "") != expected:
            return self._blocked("plan_changed", "计划身份在确认前发生变化，请重新描述策略。")
        facts = dict(context.get("account") or {})
        if facts.get("reconciliation_healthy") is not True:
            return self._blocked("reconciliation_required", "Paper 对账尚未通过；确认不会创建新策略。")
        if bool(facts.get("unresolved_runtime")) or bool(facts.get("pending_terminal_actions")):
            return self._blocked("runtime_not_terminal", "Paper runtime 或终态动作尚未收口；确认不会创建新策略。")
        session_started = False
        snapshot_persisted = False
        decision_committed = False
        try:
            self.identity.start_clean_session(
                observed_at=self.now(),
                plan_digest=expected,
                reconciliation_healthy=facts.get("reconciliation_healthy") is True,
                open_positions=int(facts.get("open_positions") or 0),
                open_or_accepted_orders=int(facts.get("open_or_accepted_orders") or 0),
                unresolved_runtime=bool(facts.get("unresolved_runtime")),
                pending_terminal_actions=bool(facts.get("pending_terminal_actions")),
                strategy_session_id=session_id,
                strategy_revision_id=revision_id,
            )
            session_started = True
            _append_jsonl(self.output_root / "park_strategy" / "plans.jsonl", {"event": "plan_proposed", **plan, "created_at": self.now(), "source": "dashboard_ai"})
            confirmation = ParkConfirmationLedger(self.output_root, park_user_id=self.park_user_id)
            proposal_id = f"park-proposal-{expected.removeprefix('sha256:')[:24]}"
            confirmation_now = self._time()
            proposal = confirmation.create_proposal(
                proposal_id=proposal_id,
                strategy_session_id=session_id,
                strategy_revision_id=revision_id,
                plan_digest=expected,
                risk_digest=_digest(plan.get("risk") or {}),
                expires_at=confirmation_now + self.draft_ttl_seconds,
            )
            expected_receipt = _confirmation_receipt_digest(proposal_id, expected, confirmation_now)
            snapshot = self._snapshot(
                plan,
                draft=draft,
                decision={"receipt_digest": expected_receipt},
                actor=actor,
            )
            _append_jsonl(self.snapshot_path, snapshot)
            snapshot_persisted = True
            self._record_event_for_draft(
                draft,
                category="control",
                event_type="strategy_confirmation_prepared",
                payload={
                    "snapshot_id": snapshot["snapshot_id"],
                    "plan_digest": snapshot.get("plan_digest"),
                    "actor": actor,
                },
            )
            self._record_event_for_draft(
                draft,
                category="plan",
                event_type="strategy_snapshot_prepared",
                payload={"snapshot_id": snapshot["snapshot_id"], "plan_digest": snapshot.get("plan_digest")},
            )
            _append_jsonl(self.chat_path, {"event": "strategy_confirmation_prepared", "snapshot_id": snapshot["snapshot_id"], "source_text": draft.get("source_text"), "recorded_at": self.now(), "actor": actor, "provider": draft.get("provider")})
            decision = confirmation.decide(
                proposal_id=proposal_id,
                park_user_id=self.park_user_id,
                command_text=f"confirm {expected}",
                current_binding={"strategy_session_id": session_id, "strategy_revision_id": revision_id},
                now=confirmation_now,
            )
            if str(decision.get("receipt_digest") or "") != expected_receipt:
                raise ParkAiChatError("confirmation_receipt_mismatch", "confirmation receipt did not match the prepared snapshot")
            decision_committed = True
            try:
                _append_jsonl(self.chat_path, {"event": "strategy_accepted", "snapshot_id": snapshot["snapshot_id"], "source_text": draft.get("source_text"), "recorded_at": self.now(), "actor": actor, "provider": draft.get("provider")})
            except OSError:
                # The confirmation ledger and immutable snapshot are already
                # durable; a chat-audit transport failure cannot authorize a
                # second attempt or mutate Paper state.
                pass
        except Exception as exc:  # noqa: BLE001 - no partial execution authority.
            if snapshot_persisted and not decision_committed:
                try:
                    record_strategy_snapshot_terminal(
                        self.output_root,
                        strategy_session_id=session_id,
                        strategy_revision_id=revision_id,
                        plan_digest=expected,
                        reason="confirmation_failed",
                        observed_at=self.now(),
                    )
                except Exception:
                    pass
            if session_started:
                try:
                    self.identity.close_session(
                        strategy_session_id=session_id,
                        strategy_revision_id=revision_id,
                        observed_at=self.now(),
                        reason="dashboard_ai_commit_failed",
                    )
                except Exception:
                    pass
            draft["confirmable"] = False
            draft["clarification_code"] = "strategy_commit_blocked"
            try:
                self._save_draft(draft)
            except OSError:
                pass
            return self._blocked("strategy_commit_blocked", f"策略确认持久化未完成（{type(exc).__name__}）；未提交新的 Paper 执行权。")
        self._delete_draft()
        return {"status": "confirmed", "snapshot": self._public_snapshot(snapshot), "proposal": proposal, "decision": decision, "paper_only": True}

    @_serialized
    def reject(self, draft_id: str, *, actor: str | None = None) -> dict[str, Any]:
        draft = self._load_draft()
        if not draft or str(draft.get("draft_id")) != str(draft_id):
            prior = next(
                (
                    row
                    for row in reversed(_read_jsonl(self.chat_path))
                    if row.get("event") == "draft_rejected"
                    and str(row.get("draft_id") or "") == str(draft_id)
                ),
                None,
            )
            if prior:
                return {
                    "status": "rejected",
                    "draft_id": str(draft_id),
                    "paper_only": True,
                    "recorded": False,
                    "idempotent": True,
                }
            return self._blocked("draft_missing", "这个 Draft 已不存在。")
        self._record_event_for_draft(
            draft,
            category="control",
            event_type="strategy_draft_rejected",
            payload={"draft_id": draft_id, "actor": actor},
        )
        self._record_chat_event(
            "draft_rejected",
            {"draft_id": draft_id, "actor": actor, "card_created": False},
        )
        self._delete_draft()
        return {"status": "rejected", "draft_id": str(draft_id), "paper_only": True, "recorded": False}

    @_serialized
    def read_model(self) -> dict[str, Any]:
        draft = self._load_draft()
        snapshots = _read_jsonl(self.snapshot_path)
        terminal_events = {
            (
                str(row.get("strategy_session_id") or ""),
                str(row.get("strategy_revision_id") or ""),
                str(row.get("plan_digest") or ""),
            ): row
            for row in _read_jsonl(self.output_root / "park_strategy" / "strategy_snapshot_events.jsonl")
            if row.get("event") == "strategy_snapshot_sealed"
        }
        public_snapshots: list[dict[str, Any]] = []
        for snapshot in snapshots[-20:]:
            public = self._public_snapshot(snapshot)
            if public.get("integrity") == "mismatch":
                public["status"] = "blocked"
                public["timeline"] = [
                    *list(public.get("timeline") or []),
                    {"event": "integrity_mismatch", "occurred_at": self.now()},
                ]
            event = terminal_events.get(
                (
                    str(snapshot.get("strategy_session_id") or ""),
                    str(snapshot.get("strategy_revision_id") or ""),
                    str(snapshot.get("plan_digest") or ""),
                )
            )
            if event:
                public["status"] = "expired"
                public["sealed"] = True
                public["timeline"] = [
                    *list(public.get("timeline") or []),
                    {
                        "event": "sealed",
                        "reason": event.get("reason"),
                        "occurred_at": event.get("observed_at"),
                    },
                ]
            public_snapshots.append(public)
        return {
            "schema_version": PARK_AI_CHAT_SCHEMA,
            "paper_only": True,
            "pending_draft": self._public_draft(draft) if draft else None,
            "snapshots": public_snapshots,
        }

    def _complete_draft(self, draft: dict[str, Any], *, actor: str | None) -> dict[str, Any]:
        provider = dict(draft.get("provider") or {})
        self._record_event_for_draft(
            draft,
            category="provider",
            event_type="ai_provider_call",
            payload={
                "provider": provider.get("provider"),
                "model": provider.get("model"),
                "status": provider.get("status"),
                "elapsed_ms": provider.get("elapsed_ms"),
                "timed_out": bool(provider.get("timed_out")),
                "fallback_from": provider.get("fallback_from"),
            },
        )
        try:
            normalized = dict(draft.get("normalized") or {})
            # Bind the candidate to its draft identity before calculating the
            # digest.  The identity is stable across the final confirmation;
            # changing it at confirmation time would make every card stale.
            normalized["strategy_session_id"] = str(draft.get("strategy_session_id") or "")
            normalized["strategy_revision_id"] = str(draft.get("strategy_revision_id") or "")
            draft["normalized"] = normalized
            context = self._read_context(self.identity.active_session())
            if draft.get("requires_disposition") and not draft.get("disposition"):
                draft["confirmable"] = False
                self._save_draft(draft)
                return {
                    "status": "needs_disposition",
                    "code": "portfolio_disposition_required",
                    "message": self._disposition_prompt(context),
                    "draft": self._public_draft(draft),
                    "confirmable": False,
                    "paper_only": True,
                }
            plan = self._build_plan(draft, context)
        except ParkStrategyPlanError as exc:
            draft["confirmable"] = False
            self._save_draft(draft)
            return self._clarification(exc.code, str(exc), source_text=str(draft.get("source_text") or ""), provider=draft.get("provider"), draft=draft)
        draft.update({
            "plan": plan,
            "plan_digest": str(plan.get("plan_digest") or ""),
            "confirmable": True,
            "updated_at": self.now(),
        })
        self._save_draft(draft)
        return {
            "status": "draft",
            "message": self._draft_message(plan, draft),
            "draft": self._public_draft(draft),
            "confirmable": True,
            "paper_only": True,
        }

    def _build_plan(self, draft: Mapping[str, Any], context: Mapping[str, Any]) -> dict[str, Any]:
        normalized = dict(draft.get("normalized") or {})
        if normalized.get("strategy_type") == "dca" and (normalized.get("stop_price") in (None, "") or normalized.get("take_profit_price") in (None, "")):
            raise ParkStrategyPlanError("dca_exit_fields_required", "DCA requires explicit stop_price and take_profit_price")
        account = dict(context.get("account") or {})
        plan = build_deterministic_risk_plan(
            normalized,
            market=dict(context.get("market") or {}),
            account_equity=account.get("equity"),
        )
        disposition = dict(draft.get("disposition") or {})
        existing_notional = self._existing_notional(account)
        risk = dict(plan.get("risk") or {})
        risk["existing_portfolio_notional"] = existing_notional
        risk["portfolio_disposition"] = disposition or None
        risk["available_notional_after_existing"] = round(max(0.0, float(risk.get("maximum_notional") or 0.0) - existing_notional), 12)
        plan["risk"] = risk
        plan["portfolio"] = {
            "disposition": disposition or None,
            "existing_notional": existing_notional,
            "open_positions": int(account.get("open_positions") or 0),
            "open_or_accepted_orders": int(account.get("open_or_accepted_orders") or 0),
        }
        plan["plan_digest"] = _digest(plan)
        return plan

    @staticmethod
    def _existing_notional(account: Mapping[str, Any]) -> float:
        snapshot = dict(account.get("snapshot") or {})
        total = 0.0
        rows = [
            row
            for row in snapshot.get("positions") or []
            if str(row.get("status") or "open").lower() == "open"
        ] + [
            row
            for row in snapshot.get("orders") or []
            if str(row.get("state") or "accepted").lower() == "accepted"
        ]
        for row in rows:
            try:
                notional = float(row.get("notional") or 0)
            except (TypeError, ValueError):
                notional = 0.0
            if notional <= 0:
                try:
                    notional = abs(float(row.get("quantity") or row.get("remaining_units") or 0)) * abs(float(row.get("price") or row.get("entry_price") or 0))
                except (TypeError, ValueError):
                    notional = 0.0
            total += notional
        return round(total, 12)

    @staticmethod
    def _has_existing_exposure(account: Mapping[str, Any]) -> bool:
        try:
            positions = int(account.get("open_positions") or 0)
            orders = int(account.get("open_or_accepted_orders") or 0)
        except (TypeError, ValueError):
            return True
        snapshot = account.get("snapshot") or {}
        active_positions = [
            row
            for row in snapshot.get("positions") or []
            if str(row.get("status") or "").lower() == "open"
        ]
        active_orders = [
            row
            for row in snapshot.get("orders") or []
            if str(row.get("state") or "").lower() == "accepted"
        ]
        return bool(positions or orders or active_positions or active_orders)

    def _read_context(self, active: Mapping[str, Any] | None) -> dict[str, Any]:
        market = dict(self.market_reader() or {})
        cycle = str(recording_window(self.now())["record_window_id"])
        account = dict(self.account_reader(self.output_root, cycle) or {})
        return {
            "market": market,
            "account": account,
            "strategy": dict(active or {}),
            "positions": list((account.get("snapshot") or {}).get("positions") or []),
            "orders": list((account.get("snapshot") or {}).get("orders") or []),
        }

    def _new_draft(self, source_text: str, normalized: Mapping[str, Any], *, active: Mapping[str, Any] | None, provider: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "schema_version": PARK_AI_CHAT_SCHEMA,
            "draft_id": f"draft-{uuid.uuid4().hex}",
            "strategy_session_id": f"session-{uuid.uuid4().hex}",
            "strategy_revision_id": f"revision-{uuid.uuid4().hex}",
            "source_text": source_text,
            "normalized": dict(normalized),
            "provider": dict(provider),
            "requires_disposition": bool(active),
            "disposition": None,
            "created_at": self.now(),
            "expires_at": self._time() + self.draft_ttl_seconds,
            "confirmable": False,
        }

    def _load_draft(self) -> dict[str, Any] | None:
        draft = _read_json(self.draft_path)
        if draft and self._expired(draft):
            self._record_event_for_draft(
                draft,
                category="control",
                event_type="strategy_draft_expired",
                payload={"draft_id": draft.get("draft_id")},
            )
            self._delete_draft()
            return None
        return draft

    def _save_draft(self, draft: Mapping[str, Any]) -> None:
        _write_json(self.draft_path, draft)

    def _delete_draft(self) -> None:
        try:
            self.draft_path.unlink()
        except FileNotFoundError:
            pass

    def _expired(self, draft: Mapping[str, Any]) -> bool:
        try:
            return float(draft.get("expires_at") or 0) <= float(self._time())
        except (TypeError, ValueError):
            return True

    def _record_provider(self, metadata: Mapping[str, Any], *, actor: str | None) -> None:
        _append_jsonl(
            self.output_root / "park_strategy" / "provider_calls.jsonl",
            {
                "schema_version": PARK_AI_CHAT_SCHEMA,
                "event": "dashboard_ai_provider_call",
                "provider": metadata.get("provider"),
                "model": metadata.get("model"),
                "status": metadata.get("status"),
                "elapsed_ms": metadata.get("elapsed_ms"),
                "timed_out": bool(metadata.get("timed_out")),
                "fallback_from": metadata.get("fallback_from"),
                "error_type": metadata.get("error_type"),
                "recorded_at": self.now(),
                "actor": actor,
            },
        )

    def _record_chat_event(self, event: str, payload: Mapping[str, Any]) -> None:
        _append_jsonl(
            self.chat_path,
            {
                "schema_version": PARK_AI_CHAT_SCHEMA,
                "event": str(event),
                "recorded_at": self.now(),
                **dict(payload),
            },
        )

    def _record_assistant_result(self, result: Mapping[str, Any], *, actor: str | None) -> None:
        """Persist a bounded assistant outcome without raw provider output."""

        draft = result.get("draft") if isinstance(result.get("draft"), Mapping) else {}
        snapshot = result.get("snapshot") if isinstance(result.get("snapshot"), Mapping) else {}
        self._record_chat_event(
            "assistant_response",
            {
                "status": result.get("status"),
                "code": result.get("code"),
                "confirmable": bool(result.get("confirmable")),
                "draft_id": draft.get("draft_id") or result.get("draft_id"),
                "plan_digest": draft.get("plan_digest") or snapshot.get("plan_digest"),
                "snapshot_id": snapshot.get("snapshot_id"),
                "actor": actor,
            },
        )

    def _record_event_for_draft(
        self,
        draft: Mapping[str, Any],
        *,
        category: str,
        event_type: str,
        payload: Mapping[str, Any] | None = None,
    ) -> None:
        """Append a bounded Recording Track fact without persisting raw drafts."""

        session_id = str(draft.get("strategy_session_id") or "").strip()
        revision_id = str(draft.get("strategy_revision_id") or "").strip()
        if not session_id or not revision_id:
            return
        try:
            window = recording_window(self.now())
            track = ParkRecordingTrack(self.output_root)
            track.start_window(
                record_window_id=str(window["record_window_id"]),
                strategy_session_id=session_id,
                strategy_revision_id=revision_id,
                starts_at=str(window["starts_at"]),
                ends_at=str(window["ends_at"]),
            )
            track.record_event(
                record_window_id=str(window["record_window_id"]),
                strategy_session_id=session_id,
                strategy_revision_id=revision_id,
                category=category,
                event_type=event_type,
                source="dashboard_ai",
                occurred_at=self.now(),
                payload=dict(payload or {}),
            )
        except Exception:
            # Recording uncertainty never turns a proposal into execution; the
            # dedicated chat/provider journals remain available for diagnosis.
            return

    def _snapshot(self, plan: Mapping[str, Any], *, draft: Mapping[str, Any], decision: Mapping[str, Any], actor: str | None) -> dict[str, Any]:
        snapshot_id = f"snapshot-{str(plan.get('plan_digest') or '').removeprefix('sha256:')[:24]}"
        snapshot = {
            "schema_version": PARK_AI_SNAPSHOT_SCHEMA,
            "snapshot_id": snapshot_id,
            "draft_id": draft.get("draft_id"),
            "status": "confirmed",
            "strategy_session_id": plan.get("strategy_session_id"),
            "strategy_revision_id": plan.get("strategy_revision_id"),
            "plan_digest": plan.get("plan_digest"),
            "source_text": draft.get("source_text"),
            "normalized_input": dict(plan.get("normalized_input") or {}),
            "market": dict(plan.get("market") or {}),
            "risk": dict(plan.get("risk") or {}),
            "portfolio": dict(plan.get("portfolio") or {}),
            "timeline": [
                {"event": "confirmed", "occurred_at": self.now(), "actor": actor, "receipt_digest": decision.get("receipt_digest")},
            ],
            "provider": dict(draft.get("provider") or {}),
            "paper_only": True,
            "sealed": False,
        }
        snapshot["snapshot_digest"] = _digest(snapshot)
        return snapshot

    @staticmethod
    def _public_snapshot(snapshot: Mapping[str, Any]) -> dict[str, Any]:
        payload = {key: snapshot.get(key) for key in ("snapshot_id", "status", "strategy_session_id", "strategy_revision_id", "plan_digest", "normalized_input", "market", "risk", "portfolio", "timeline", "paper_only", "sealed", "snapshot_digest")}
        digest = str(snapshot.get("snapshot_digest") or "")
        if digest:
            source = {key: value for key, value in snapshot.items() if key != "snapshot_digest"}
            payload["integrity"] = "verified" if _digest(source) == digest else "mismatch"
        else:
            payload["integrity"] = "unknown"
        return payload

    @staticmethod
    def _public_draft(draft: Mapping[str, Any] | None) -> dict[str, Any] | None:
        if not draft:
            return None
        return {key: draft.get(key) for key in ("draft_id", "strategy_session_id", "strategy_revision_id", "source_text", "normalized", "plan", "plan_digest", "provider", "requires_disposition", "disposition", "expires_at", "confirmable")}

    @staticmethod
    def _draft_message(plan: Mapping[str, Any], draft: Mapping[str, Any]) -> str:
        normalized = dict(plan.get("normalized_input") or {})
        risk = dict(plan.get("risk") or {})
        return (
            f"我理解的是：{normalized.get('direction')} {normalized.get('strategy_type')}，"
            f"区间 {normalized.get('lower_price_boundary')}~{normalized.get('upper_price_boundary')}。\n"
            f"当前价 {dict(plan.get('market') or {}).get('price')}；最大名义 {risk.get('maximum_notional')}；"
            f"理论最大亏损 {risk.get('theoretical_max_loss')}；有效杠杆 {risk.get('effective_leverage')}x。\n\n"
            f"请检查 snapshot，确认后点击“确认并执行”。digest：{draft.get('plan_digest')}"
        )

    @staticmethod
    def _disposition_prompt(context: Mapping[str, Any]) -> str:
        account = dict(context.get("account") or {})
        return (
            f"当前已有 {account.get('open_positions', 0)} 个持仓、{account.get('open_or_accepted_orders', 0)} 个挂单。\n"
            "请用自然语言说明旧仓怎么处理，例如：\n"
            "“旧仓继续原来的止盈止损，撤掉未成交挂单”\n"
            "“旧仓全部保留，不动旧挂单”\n"
            "系统会先把你的意思转换成标准化处置方案，再让你确认。"
        )

    @staticmethod
    def _clarification(code: str, detail: str, *, source_text: str, provider: Mapping[str, Any] | None, draft: Mapping[str, Any] | None = None) -> dict[str, Any]:
        samples = {
            "missing_direction": "例如：做空 DCA，区间 4444~4200，最大10倍杠杆，止损4444，止盈4100",
            "missing_strategy_type": "请说明 DCA 或 Grid，例如：中性网格，区间4450~4100，最大20倍杠杆",
            "missing_price_boundary": "请补充价格区间，例如：区间 4444~4200",
            "missing_risk_authority": "请补充最大杠杆或最大可接受亏损，例如：最大10倍杠杆",
            "dca_exit_fields_required": "DCA 还缺明确止损和止盈，例如：止损4444，止盈4100",
        }
        return {
            "status": "needs_clarification",
            "code": code,
            "message": f"我还需要确认：{detail}\n\n你可以这样回复：{samples.get(code, '请直接补充缺少的参数。')}",
            "provider": dict(provider or {}),
            "draft": dict(draft or {}) if draft else None,
            "confirmable": False,
            "paper_only": True,
            "source_text": source_text,
        }

    @staticmethod
    def _blocked(code: str, message: str) -> dict[str, Any]:
        return {"status": "blocked", "code": code, "message": message, "confirmable": False, "paper_only": True}

    @staticmethod
    def _default_market_reader() -> Mapping[str, Any]:
        from services.park_telegram_runtime import default_market_reader

        return default_market_reader()

    @staticmethod
    def _default_account_reader(output_root: Path, cycle_id: str) -> Mapping[str, Any]:
        from services.park_telegram_runtime import default_account_reader

        return default_account_reader(output_root, cycle_id)
