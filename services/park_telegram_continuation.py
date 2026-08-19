"""Durable, non-authorizing context for multi-message Park strategy input."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any


PARK_CONTINUATION_SCHEMA = "park-telegram-continuation-v1"


class ParkContinuationError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _digest(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _append(path: Path, row: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = (json.dumps(dict(row), ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
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
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ParkContinuationError("journal_corrupt", "continuation row is not an object")
            rows.append(value)
    return rows


class ParkTelegramContinuationLedger:
    """Persist incomplete input without granting strategy authority."""

    def __init__(self, output_root: Path, *, park_user_id: str, chat_id: str, ttl_seconds: int = 900) -> None:
        self.path = Path(output_root) / "park_strategy" / "telegram_continuations.jsonl"
        self.park_user_id = str(park_user_id or "").strip()
        self.chat_id = str(chat_id or "").strip()
        self.ttl_seconds = max(60, int(ttl_seconds))
        if not self.park_user_id or not self.chat_id:
            raise ParkContinuationError("identity_missing", "Park user and chat are required")

    def rows(self) -> list[dict[str, Any]]:
        return _rows(self.path)

    def latest_pending(self) -> dict[str, Any] | None:
        latest: dict[str, dict[str, Any]] = {}
        for row in self.rows():
            draft_id = str(row.get("draft_id") or "")
            if draft_id:
                latest[draft_id] = dict(row)
        pending = [
            row
            for row in latest.values()
            if row.get("event") == "pending"
            and str(row.get("park_user_id") or "") == self.park_user_id
            and str(row.get("chat_id") or "") == self.chat_id
            and float(row.get("expires_at") or 0) > time.time()
        ]
        if len(pending) > 1:
            raise ParkContinuationError("multiple_pending_contexts", "multiple pending strategy contexts are active")
        return dict(pending[0]) if pending else None

    def record_pending(
        self,
        *,
        update_id: int | str,
        text_digest: str,
        normalized_input: Mapping[str, Any],
        missing_fields: list[str],
        draft_id: str | None = None,
    ) -> dict[str, Any]:
        payload = {
            "park_user_id": self.park_user_id,
            "chat_id": self.chat_id,
            "normalized_input": dict(normalized_input),
            "missing_fields": sorted({str(value) for value in missing_fields if str(value)}),
            "source_text_digest": str(text_digest),
        }
        requested_draft_id = str(draft_id or "").strip()
        draft_id = requested_draft_id or "draft-" + _digest(payload).removeprefix("sha256:")[:24]
        existing = next(
            (
                dict(row)
                for row in reversed(self.rows())
                if row.get("event") == "pending" and row.get("draft_id") == draft_id
            ),
            None,
        )
        if existing and not requested_draft_id and float(existing.get("expires_at") or 0) > time.time():
            return existing
        prior_normalized = dict(existing.get("normalized_input") or {}) if existing else {}
        prior_updates = [str(value) for value in existing.get("source_update_ids") or []] if existing else []
        row = {
            "schema_version": PARK_CONTINUATION_SCHEMA,
            "event": "pending",
            "draft_id": draft_id,
            "created_at": time.time(),
            "expires_at": time.time() + self.ttl_seconds,
            "execution_authorized": False,
            **payload,
            "source_update_ids": [*prior_updates, str(update_id)],
            "normalized_input": {**prior_normalized, **dict(normalized_input)},
            "missing_fields": sorted({str(value) for value in missing_fields if str(value)}),
        }
        _append(self.path, row)
        return row

    def resolve(self, draft: Mapping[str, Any], *, update_id: int | str, plan_digest: str) -> dict[str, Any]:
        draft_id = str(draft.get("draft_id") or "").strip()
        if not draft_id:
            raise ParkContinuationError("draft_identity_missing", "continuation draft identity is required")
        row = {
            "schema_version": PARK_CONTINUATION_SCHEMA,
            "event": "resolved",
            "draft_id": draft_id,
            "resolved_at": time.time(),
            "resolution_update_id": str(update_id),
            "plan_digest": str(plan_digest),
            "execution_authorized": False,
            "next_action": "await_exact_park_confirmation",
        }
        _append(self.path, row)
        return row
