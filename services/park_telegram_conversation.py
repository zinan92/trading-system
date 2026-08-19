"""Durable, non-authorizing Telegram Trading Conversation state."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from services.park_conversation_contract import (
    MAX_HISTORY_MESSAGES,
    PARK_CONVERSATION_SCHEMA,
    ParkConversationContractError,
    extract_explicit_strategy_patch,
    normalize_conversation_result,
)


class ParkTelegramConversationError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _digest(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _append(path: Path, row: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = (json.dumps(dict(row), sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())
        with path.open("ab") as destination:
            destination.write(line)
            destination.flush()
            os.fsync(destination.fileno())
        os.unlink(tmp_name)
    except Exception:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise


def _rows(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ParkTelegramConversationError("conversation_journal_corrupt", "conversation row is not an object")
            rows.append(value)
    return rows


class ParkTelegramConversationLedger:
    """Append-only conversation facts; never an execution authority."""

    def __init__(
        self,
        output_root: Path,
        *,
        park_user_id: str,
        chat_id: str,
        ttl_seconds: int = 1800,
    ) -> None:
        self.path = Path(output_root) / "park_strategy" / "telegram_conversation.jsonl"
        self.park_user_id = str(park_user_id or "").strip()
        self.chat_id = str(chat_id or "").strip()
        self.ttl_seconds = max(60, int(ttl_seconds))
        if not self.park_user_id or not self.chat_id:
            raise ParkTelegramConversationError("conversation_identity_missing", "Park identity is required")

    def rows(self) -> list[dict[str, Any]]:
        return _rows(self.path)

    def history(self, *, now: float | None = None, limit: int = MAX_HISTORY_MESSAGES) -> list[dict[str, Any]]:
        cutoff = float(now if now is not None else time.time()) - self.ttl_seconds
        result: list[dict[str, Any]] = []
        for row in self.rows():
            if str(row.get("park_user_id") or "") != self.park_user_id or str(row.get("chat_id") or "") != self.chat_id:
                continue
            try:
                if float(row.get("recorded_at") or 0) < cutoff:
                    continue
            except (TypeError, ValueError):
                continue
            role = str(row.get("role") or "")
            content = str(row.get("content") or "")
            if role in {"user", "assistant"} and content:
                item: dict[str, Any] = {"role": role, "content": content}
                if role == "assistant":
                    item["mode"] = str(row.get("mode") or "")
                    item["strategy_patch"] = dict(row.get("strategy_patch") or {})
                    item["missing_fields"] = list(row.get("missing_fields") or [])
                result.append(item)
        return result[-max(1, int(limit)) :]

    def latest_strategy_patch(self, *, now: float | None = None) -> dict[str, Any]:
        cutoff = float(now if now is not None else time.time()) - self.ttl_seconds
        merged: dict[str, Any] = {}
        for row in self.rows():
            if row.get("event") != "assistant_message":
                continue
            if str(row.get("park_user_id") or "") != self.park_user_id or str(row.get("chat_id") or "") != self.chat_id:
                continue
            try:
                if float(row.get("recorded_at") or 0) < cutoff:
                    continue
            except (TypeError, ValueError):
                continue
            patch = row.get("strategy_patch")
            if isinstance(patch, Mapping):
                merged.update({str(key): value for key, value in patch.items() if value not in (None, "")})
        return merged

    def record_user(self, *, update_id: Any, text: str, recorded_at: float | None = None) -> dict[str, Any]:
        row = {
            "schema_version": PARK_CONVERSATION_SCHEMA,
            "event": "user_message",
            "role": "user",
            "park_user_id": self.park_user_id,
            "chat_id": self.chat_id,
            "update_id": str(update_id),
            "content": str(text or "")[:2000],
            "content_digest": _digest(str(text or "")),
            "execution_authorized": False,
            "recorded_at": float(recorded_at if recorded_at is not None else time.time()),
        }
        _append(self.path, row)
        return row

    def record_assistant(
        self,
        *,
        update_id: Any,
        conversation: Mapping[str, Any],
        provider: Mapping[str, Any] | None = None,
        recorded_at: float | None = None,
    ) -> dict[str, Any]:
        row = {
            "schema_version": PARK_CONVERSATION_SCHEMA,
            "event": "assistant_message",
            "role": "assistant",
            "park_user_id": self.park_user_id,
            "chat_id": self.chat_id,
            "update_id": str(update_id),
            "content": str(conversation.get("assistant_reply") or "")[:2000],
            "content_digest": _digest(str(conversation.get("assistant_reply") or "")),
            "mode": str(conversation.get("mode") or ""),
            "strategy_patch": dict(conversation.get("strategy_patch") or {}),
            "missing_fields": list(conversation.get("missing_fields") or []),
            "needs_confirmation": bool(conversation.get("needs_confirmation")),
            "execution_authorized": False,
            "provider": dict(provider or {}),
            "recorded_at": float(recorded_at if recorded_at is not None else time.time()),
        }
        _append(self.path, row)
        return row


class ParkTelegramConversationAgent:
    """Provider-facing conversation seam with no execution capability."""

    def __init__(self, ledger: ParkTelegramConversationLedger, provider: Any) -> None:
        self.ledger = ledger
        self.provider = provider

    def evaluate(
        self,
        text: str,
        *,
        update_id: Any,
        context: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        converse = getattr(self.provider, "converse", None)
        if not callable(converse):
            return {"status": "unavailable", "metadata": {"provider": "conversation_unavailable", "status": "not_configured"}}
        history = self.ledger.history()
        prior_patch = self.ledger.latest_strategy_patch()
        self.ledger.record_user(update_id=update_id, text=text)
        user_text = "\n".join(
            [str(item.get("content") or "") for item in history if item.get("role") == "user"] + [str(text or "")]
        )
        explicit_patch = extract_explicit_strategy_patch(user_text)
        try:
            result = dict(converse(text, context=context, history=history) or {})
        except Exception as exc:  # noqa: BLE001 - conversation failure is fail-closed.
            result = {
                "status": "unavailable",
                "metadata": {
                    "provider": self.provider.__class__.__name__,
                    "status": "adapter_error",
                    "error_type": type(exc).__name__,
                },
            }
        metadata = dict(result.get("metadata") or {})
        if result.get("status") != "ok" or not isinstance(result.get("conversation"), Mapping):
            return {"status": "unavailable", "metadata": metadata}
        try:
            conversation = normalize_conversation_result(result["conversation"], source_text=text)
        except ParkConversationContractError as exc:
            metadata = {**metadata, "status": exc.code}
            return {"status": "unavailable", "metadata": metadata}
        if prior_patch:
            conversation["strategy_patch"] = {
                **prior_patch,
                **dict(conversation.get("strategy_patch") or {}),
            }
        if explicit_patch:
            conversation["strategy_patch"] = {
                **dict(conversation.get("strategy_patch") or {}),
                **explicit_patch,
            }
        self.ledger.record_assistant(update_id=update_id, conversation=conversation, provider=metadata)
        return {"status": "ok", "conversation": conversation, "metadata": metadata}
