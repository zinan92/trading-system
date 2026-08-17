"""Pure, append-only strategy snapshot terminal events.

This module deliberately contains no provider or network imports so the Paper
runtime can seal a card without loading the Dashboard AI transport.
"""

from __future__ import annotations

import hashlib
import json
import os
import fcntl
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Mapping


PARK_AI_SNAPSHOT_EVENT_SCHEMA = "park-strategy-snapshot-event-v1"


def _digest(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        value = json.loads(line)
        if isinstance(value, dict):
            rows.append(value)
    return rows


def _append_jsonl(path: Path, row: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(dict(row), ensure_ascii=False, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


@contextmanager
def _journal_lock(path: Path):
    """Serialize terminal seal read/check/append across worker processes."""

    lock_path = path.with_name(f".{path.name}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def record_strategy_snapshot_terminal(
    output_root: Path,
    *,
    strategy_session_id: str,
    strategy_revision_id: str,
    plan_digest: str,
    reason: str,
    observed_at: str,
) -> dict[str, Any]:
    """Append an immutable terminal event, idempotently by identity and reason."""

    event_path = Path(output_root) / "park_strategy" / "strategy_snapshot_events.jsonl"
    key = _digest(
        {
            "strategy_session_id": strategy_session_id,
            "strategy_revision_id": strategy_revision_id,
            "plan_digest": plan_digest,
            "reason": reason,
        }
    )
    with _journal_lock(event_path):
        existing = next(
            (
                row
                for row in _read_jsonl(event_path)
                if str(row.get("event_key") or "") == key
            ),
            None,
        )
        if existing:
            return dict(existing)
        event = {
            "schema_version": PARK_AI_SNAPSHOT_EVENT_SCHEMA,
            "event": "strategy_snapshot_sealed",
            "event_key": key,
            "strategy_session_id": str(strategy_session_id),
            "strategy_revision_id": str(strategy_revision_id),
            "plan_digest": str(plan_digest),
            "reason": str(reason),
            "observed_at": str(observed_at),
            "paper_only": True,
        }
        _append_jsonl(event_path, event)
        return event
