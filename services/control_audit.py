"""Append-only operator audit trail for strategy control actions.

Every mutating control action (start, stop, adjust_plan, cancel_all,
reset_statistics, ...) is recorded as one JSON line under
`outputs/dualtrack/strategy_control/control_events/<UTC date>.jsonl`,
whether it was accepted or rejected. Preview is read-only and is not
recorded here (the public gateway keeps its own access log for previews).

The event answers "who did what, from where, with what request, and what
happened" — the gap exposed by the 2026-07-15 stop diagnosis, where the
runtime row proved an action's time but could not attribute it to anyone.

Trust model: the actor email is asserted by the public gateway via the
`X-Goldbot-Actor-Email` header only after it has validated the Cloudflare
Access JWT; the upstream server is loopback-bound, and the gateway never
forwards client-supplied headers. A request without that header is a
local operator by definition.

Audit writes must never block a control action: stopping a robot with a
full disk is more important than logging the stop. Callers report a
failed write honestly via `audit_recorded: false` in the API response.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "strategy-control-event-v1"
_EVENTS_SUBDIR = Path("dualtrack") / "strategy_control" / "control_events"
_MAX_STRING = 512
_MAX_KEYS = 40
_MAX_DEPTH = 4
_CREDENTIAL_MARKERS = (
    "token",
    "jwt",
    "assertion",
    "authorization",
    "secret",
    "password",
    "cookie",
    "credential",
    "api_key",
    "access_key",
    "private_key",
    "passphrase",
)
_COMPACT_CREDENTIAL_KEYS = frozenset({
    "accesstoken",
    "apikey",
    "accesskey",
    "privatekey",
    "clientsecret",
    "clientcredential",
})
_INLINE_CREDENTIAL = re.compile(
    r"(?i)\b(token|jwt|assertion|authorization|secret|password|cookie|credential|"
    r"api[_-]?key|access[_-]?key|private[_-]?key|passphrase)\b\s*[:=]\s*([^\s,;]+)"
)
_BEARER_CREDENTIAL = re.compile(r"(?i)\bbearer\s+[^\s,;]+")


def build_control_event(
    *,
    cycle_id: str,
    action: str,
    actor: dict[str, Any] | None,
    payload: dict[str, Any] | None,
    result: str,
    error: str | None,
    runtime: dict[str, Any] | None,
    evidence: dict[str, Any] | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    normalized_actor = normalize_actor(actor)
    runtime = runtime or {}
    return {
        "schema_version": SCHEMA_VERSION,
        "ts": str(now) if now else datetime.now(timezone.utc).isoformat(),
        "cycle_id": str(cycle_id),
        "action": str(action or "").lower(),
        "actor": normalized_actor,
        "request": _bounded(payload or {}, depth=0),
        "result": str(result),
        "error": _bounded(str(error), depth=0) if error else None,
        "evidence": _bounded(evidence or {}, depth=0),
        "runtime_after": {
            "desired_state": runtime.get("desired_state"),
            "actual_state": runtime.get("actual_state"),
            "last_action": runtime.get("last_action"),
            "updated_at": runtime.get("updated_at"),
        },
    }


def normalize_actor(actor: dict[str, Any] | None) -> dict[str, Any]:
    actor = actor if isinstance(actor, dict) else {}
    email = str(actor.get("email") or "").strip().lower() or None
    transport = str(actor.get("transport") or "").strip() or ("public_gateway" if email else "local")
    client = actor.get("client")
    return {"email": email, "transport": transport, "client": str(client) if client else None}


def append_control_event(output_root: Path, event: dict[str, Any]) -> Path:
    """Append one event line; the file is date-partitioned and append-only."""
    day = str(event.get("ts") or "")[:10] or datetime.now(timezone.utc).date().isoformat()
    directory = Path(output_root) / _EVENTS_SUBDIR
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{day}.jsonl"
    line = json.dumps(event, ensure_ascii=False, sort_keys=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")
    return path


def read_last_control_event(output_root: Path) -> dict[str, Any] | None:
    directory = Path(output_root) / _EVENTS_SUBDIR
    if not directory.is_dir():
        return None
    for path in sorted(directory.glob("*.jsonl"), reverse=True):
        lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        if not lines:
            continue
        try:
            row = json.loads(lines[-1])
        except json.JSONDecodeError:
            return None
        return row if isinstance(row, dict) else None
    return None


def _bounded(value: Any, *, depth: int) -> Any:
    if depth >= _MAX_DEPTH:
        return "[truncated]"
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= _MAX_KEYS:
                out["[truncated]"] = f"{len(value) - _MAX_KEYS} more keys"
                break
            if _is_credential_key(key):
                out[str(key)] = "[redacted]"
                continue
            out[str(key)] = _bounded(item, depth=depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        items = [_bounded(item, depth=depth + 1) for item in list(value)[:_MAX_KEYS]]
        if len(value) > _MAX_KEYS:
            items.append("[truncated]")
        return items
    if isinstance(value, str):
        redacted = _redact_inline_credentials(value)
        if len(redacted) > _MAX_STRING:
            return redacted[:_MAX_STRING] + f"...[{len(redacted)} chars]"
        return redacted
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return str(value)[:_MAX_STRING]


def _redact_inline_credentials(value: str) -> str:
    redacted = _BEARER_CREDENTIAL.sub("Bearer [redacted]", value)
    return _INLINE_CREDENTIAL.sub(lambda match: f"{match.group(1)}=[redacted]", redacted)


def _is_credential_key(value: Any) -> bool:
    lowered = str(value).lower()
    compact = re.sub(r"[^a-z0-9]", "", lowered)
    return (
        any(marker in lowered for marker in _CREDENTIAL_MARKERS)
        or compact in _COMPACT_CREDENTIAL_KEYS
    )
