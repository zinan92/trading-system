"""Durable Telegram control-plane ledger for the Park Strategy Track.

This module is a transport-neutral inbox/outbox.  A polling or webhook adapter
may feed it updates and consume pending messages, but the ledger itself has no
network client and never grants execution authority.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Mapping


PARK_TELEGRAM_SCHEMA = "park-telegram-control-v1"


class ParkTelegramControlError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _text(value: Any, field: str) -> str:
    result = str(value or "").strip()
    if not result:
        raise ParkTelegramControlError("invalid_input", f"{field} is required")
    return result


def _digest(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _append(path: Path, row: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = (json.dumps(dict(row), sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())
        with path.open("ab") as handle:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())
        os.unlink(temp_name)
    except Exception:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise


def _rows(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    result: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ParkTelegramControlError("journal_corrupt", "Telegram journal row is not an object")
            result.append(value)
    return result


def strategy_binding(binding: Mapping[str, Any] | None) -> dict[str, str] | None:
    """Normalize an exact strategy binding; a window alone is never enough."""

    if binding is None:
        return None
    session_id = str(binding.get("strategy_session_id") or "").strip()
    revision_id = str(binding.get("strategy_revision_id") or "").strip()
    if not session_id or not revision_id:
        raise ParkTelegramControlError(
            "binding_incomplete",
            "Telegram command binding requires strategy_session_id and strategy_revision_id",
        )
    return {
        "strategy_session_id": session_id,
        "strategy_revision_id": revision_id,
    }


class ParkTelegramLedger:
    def __init__(
        self,
        output_root: Path,
        *,
        park_user_id: str,
        chat_id: str,
        max_send_attempts: int = 3,
    ) -> None:
        self.output_root = Path(output_root)
        self.inbox_path = self.output_root / "park_strategy" / "telegram_inbox.jsonl"
        self.outbox_path = self.output_root / "park_strategy" / "telegram_outbox.jsonl"
        self.park_user_id = _text(park_user_id, "park_user_id")
        self.chat_id = _text(chat_id, "chat_id")
        self.max_send_attempts = max(1, int(max_send_attempts))

    def inbox_rows(self) -> list[dict[str, Any]]:
        return _rows(self.inbox_path)

    def outbox_rows(self) -> list[dict[str, Any]]:
        return _rows(self.outbox_path)

    def _effective_outbox(self) -> dict[str, dict[str, Any]]:
        effective: dict[str, dict[str, Any]] = {}
        for row in self.outbox_rows():
            message_id = str(row.get("message_id") or "")
            if message_id:
                effective[message_id] = dict(row)
        return effective

    def ingest_update(
        self,
        update: Mapping[str, Any],
        *,
        binding: Mapping[str, Any] | None = None,
        received_at: float | None = None,
    ) -> dict[str, Any]:
        try:
            update_id = int(update["update_id"])
            message = update["message"]
            message_id = int(message["message_id"])
            sender_id = str(message["from"]["id"])
            incoming_chat_id = str(message["chat"]["id"])
            text = str(message.get("text") or "")
        except (KeyError, TypeError, ValueError) as exc:
            raise ParkTelegramControlError("malformed_update", "Telegram update is incomplete") from exc
        existing = next((row for row in self.inbox_rows() if row.get("update_id") == update_id), None)
        if existing:
            return dict(existing)
        if sender_id != self.park_user_id:
            raise ParkTelegramControlError("unauthorized_user", "Telegram sender is not Park")
        if incoming_chat_id != self.chat_id:
            raise ParkTelegramControlError("unauthorized_chat", "Telegram chat is not the configured Park chat")
        normalized_binding = strategy_binding(binding)
        row = {
            "schema_version": PARK_TELEGRAM_SCHEMA,
            "event": "inbound_received",
            "update_id": update_id,
            "message_id": message_id,
            "sender_id": sender_id,
            "chat_id": incoming_chat_id,
            "text": text,
            "text_digest": _digest(text),
            "received_at": float(received_at if received_at is not None else time.time()),
            "binding": normalized_binding,
            "execution_authorized": False,
            "next_action": "parse_and_require_exact_plan_confirmation",
        }
        _append(self.inbox_path, row)
        return dict(row)

    def assert_current_binding(
        self,
        binding: Mapping[str, Any],
        current_binding: Mapping[str, Any],
    ) -> dict[str, str]:
        incoming = strategy_binding(binding)
        current = strategy_binding(current_binding)
        if incoming != current:
            raise ParkTelegramControlError("stale_binding", "Telegram command is bound to a stale strategy revision")
        return incoming or {}

    def queue_outbound(
        self,
        *,
        idempotency_key: str,
        message_type: str,
        text: str,
        binding: Mapping[str, Any] | None = None,
        queued_at: float | None = None,
    ) -> dict[str, Any]:
        key = _text(idempotency_key, "idempotency_key")
        existing = next((row for row in self.outbox_rows() if row.get("idempotency_key") == key), None)
        if existing:
            return dict(existing)
        normalized_binding = strategy_binding(binding)
        message_id = "outbound-" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]
        row = {
            "schema_version": PARK_TELEGRAM_SCHEMA,
            "event": "outbound_queued",
            "message_id": message_id,
            "idempotency_key": key,
            "message_type": _text(message_type, "message_type"),
            "chat_id": self.chat_id,
            "text": str(text),
            "text_digest": _digest(str(text)),
            "binding": normalized_binding,
            "status": "queued",
            "attempts": 0,
            "queued_at": float(queued_at if queued_at is not None else time.time()),
            "next_action": "send_with_transport_and_record_receipt",
        }
        _append(self.outbox_path, row)
        return dict(row)

    def pending_outbound(self) -> list[dict[str, Any]]:
        return [
            row for row in self._effective_outbox().values()
            if row.get("status") in {"queued", "failed"}
            and int(row.get("attempts") or 0) < self.max_send_attempts
        ]

    def record_send_result(
        self,
        *,
        message_id: str,
        transport_result: Mapping[str, Any] | None,
        recorded_at: float | None = None,
    ) -> dict[str, Any]:
        message_key = _text(message_id, "message_id")
        current = self._effective_outbox().get(message_key)
        if not current:
            raise ParkTelegramControlError("unknown_message", "outbound message does not exist")
        if current.get("status") in {"delivered", "dead_letter"}:
            return dict(current)
        result = dict(transport_result or {})
        attempts = int(current.get("attempts") or 0) + 1
        explicit_receipt = bool(result.get("ok")) and str(result.get("message_id") or "").strip()
        if explicit_receipt:
            row = {
                **current,
                "event": "outbound_delivered",
                "status": "delivered",
                "attempts": attempts,
                "transport_message_id": str(result["message_id"]),
                "transport_result_digest": _digest(json.dumps(result, sort_keys=True)),
                "recorded_at": float(recorded_at if recorded_at is not None else time.time()),
                "next_action": "none",
            }
        else:
            dead_letter = attempts >= self.max_send_attempts
            row = {
                **current,
                "event": "outbound_dead_letter" if dead_letter else "outbound_failed",
                "status": "dead_letter" if dead_letter else "failed",
                "attempts": attempts,
                "failure_code": str(result.get("reason") or "missing_explicit_transport_receipt"),
                "transport_result_digest": _digest(json.dumps(result, sort_keys=True)),
                "recorded_at": float(recorded_at if recorded_at is not None else time.time()),
                "next_action": "notify_park_and_wait" if dead_letter else "retry_send",
            }
        _append(self.outbox_path, row)
        return row
