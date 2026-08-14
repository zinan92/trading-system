"""Paper-only identity and recording-window primitives for Park Strategy Track.

This module deliberately does not import the existing cycle runner or any
execution adapter.  A strategy session/revision is an execution identity; a
recording window is only a reporting slice.  Keeping the two objects separate
is the first step toward a safe cutover without changing the current runtime.
"""

from __future__ import annotations

import json
import os
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo


PARK_STRATEGY_SESSION_SCHEMA = "park-strategy-session-v1"
PARK_STRATEGY_WINDOW_SCHEMA = "park-record-window-v1"
PARK_STRATEGY_TRACK_ENABLED_BY_DEFAULT = False
BEIJING = ZoneInfo("Asia/Shanghai")
_UTC = timezone.utc
_FORBIDDEN_WINDOW_EFFECTS = frozenset(
    {"strategy_switch", "strategy_replan", "cancel_orders", "flatten_positions"}
)


class ParkStrategyIdentityError(ValueError):
    """Raised when an identity or recording-window invariant is violated."""


def _required_text(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ParkStrategyIdentityError(f"{field} is required")
    return text


def _as_beijing(value: datetime | str) -> datetime:
    if isinstance(value, str):
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            value = datetime.fromisoformat(text)
        except ValueError as exc:
            raise ParkStrategyIdentityError("observed_at must be ISO-8601") from exc
    if not isinstance(value, datetime):
        raise ParkStrategyIdentityError("observed_at must be datetime or ISO-8601")
    if value.tzinfo is None:
        value = value.replace(tzinfo=BEIJING)
    return value.astimezone(BEIJING)


def _canonical_timestamp(value: datetime | str) -> str:
    return _as_beijing(value).astimezone(_UTC).replace(microsecond=0).isoformat()


def recording_window(value: datetime | str) -> dict[str, Any]:
    """Return the Beijing 09:00/21:00 reporting slice for ``value``."""

    local = _as_beijing(value)
    if local.hour < 9:
        start = local.replace(hour=21, minute=0, second=0, microsecond=0) - timedelta(days=1)
        phase = "NIGHT"
    elif local.hour < 21:
        start = local.replace(hour=9, minute=0, second=0, microsecond=0)
        phase = "DAY"
    else:
        start = local.replace(hour=21, minute=0, second=0, microsecond=0)
        phase = "NIGHT"
    end = start + timedelta(hours=12)
    return {
        "schema_version": PARK_STRATEGY_WINDOW_SCHEMA,
        "record_window_id": f"{start.date().isoformat()}_{phase}",
        "timezone": "Asia/Shanghai",
        "phase": phase,
        "starts_at": start.astimezone(_UTC).replace(microsecond=0).isoformat(),
        "ends_at": end.astimezone(_UTC).replace(microsecond=0).isoformat(),
    }


def recording_metadata(
    *,
    strategy_session_id: str,
    strategy_revision_id: str,
    observed_at: datetime | str,
) -> dict[str, str]:
    """Build a window reference that has no execution-control fields."""

    window = recording_window(observed_at)
    return {
        "strategy_session_id": _required_text(strategy_session_id, "strategy_session_id"),
        "strategy_revision_id": _required_text(strategy_revision_id, "strategy_revision_id"),
        "record_window_id": window["record_window_id"],
        "observed_at": _canonical_timestamp(observed_at),
    }


def _atomic_append(path: Path, row: Mapping[str, Any]) -> None:
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


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ParkStrategyIdentityError("identity journal is corrupt") from exc
        if not isinstance(row, dict):
            raise ParkStrategyIdentityError("identity journal row must be an object")
        rows.append(row)
    return rows


class ParkStrategyIdentityJournal:
    """Append-only session/revision identity journal.

    The journal only records identity and reporting facts.  It intentionally
    has no methods that submit/cancel orders, flatten positions, or mutate a
    cycle runtime.
    """

    def __init__(self, output_root: Path) -> None:
        self.output_root = Path(output_root)
        self.path = self.output_root / "park_strategy" / "identity.jsonl"

    def rows(self) -> list[dict[str, Any]]:
        return _read_jsonl(self.path)

    def active_session(self) -> dict[str, Any] | None:
        sessions: dict[str, dict[str, Any]] = {}
        for row in self.rows():
            session_id = str(row.get("strategy_session_id") or "")
            if not session_id:
                continue
            if row.get("event") == "session_started":
                sessions[session_id] = dict(row)
            elif row.get("event") == "session_closed":
                sessions.pop(session_id, None)
        if not sessions:
            return None
        return dict(next(reversed(list(sessions.values()))))

    def start_clean_session(
        self,
        *,
        observed_at: datetime | str,
        plan_digest: str,
        reconciliation_healthy: bool,
        open_positions: int = 0,
        open_or_accepted_orders: int = 0,
        unresolved_runtime: bool = False,
        pending_terminal_actions: bool = False,
        strategy_session_id: str | None = None,
        strategy_revision_id: str | None = None,
    ) -> dict[str, Any]:
        if self.active_session() is not None:
            raise ParkStrategyIdentityError("an active Park strategy session already exists")
        if not reconciliation_healthy:
            raise ParkStrategyIdentityError("clean slate requires healthy reconciliation")
        if any(int(value) != 0 for value in (open_positions, open_or_accepted_orders)):
            raise ParkStrategyIdentityError("clean slate requires no open positions or orders")
        if unresolved_runtime or pending_terminal_actions:
            raise ParkStrategyIdentityError("clean slate has unresolved runtime or terminal action")
        digest = _required_text(plan_digest, "plan_digest")
        session_id = _required_text(strategy_session_id or f"session-{uuid.uuid4().hex}", "strategy_session_id")
        revision_id = _required_text(strategy_revision_id or f"revision-{uuid.uuid4().hex}", "strategy_revision_id")
        metadata = recording_metadata(
            strategy_session_id=session_id,
            strategy_revision_id=revision_id,
            observed_at=observed_at,
        )
        row = {
            "schema_version": PARK_STRATEGY_SESSION_SCHEMA,
            "event": "session_started",
            "strategy_session_id": session_id,
            "strategy_revision_id": revision_id,
            "plan_digest": digest,
            "record_window_id": metadata["record_window_id"],
            "observed_at": metadata["observed_at"],
            "execution_authority": "strategy_session_revision",
            "recording_authority": "facts_package_review_only",
        }
        _atomic_append(self.path, row)
        return dict(row)

    def append_window_observation(
        self,
        *,
        strategy_session_id: str,
        strategy_revision_id: str,
        observed_at: datetime | str,
        late_amendment: bool = False,
        effects: list[str] | None = None,
    ) -> dict[str, Any]:
        session_id = _required_text(strategy_session_id, "strategy_session_id")
        revision_id = _required_text(strategy_revision_id, "strategy_revision_id")
        active = self.active_session()
        if not active or active.get("strategy_session_id") != session_id:
            raise ParkStrategyIdentityError("strategy session is not active")
        if active.get("strategy_revision_id") != revision_id:
            raise ParkStrategyIdentityError("strategy revision identity is immutable")
        forbidden = _FORBIDDEN_WINDOW_EFFECTS.intersection(str(item) for item in (effects or []))
        if forbidden:
            raise ParkStrategyIdentityError(
                f"recording window cannot authorize {sorted(forbidden)[0]}"
            )
        metadata = recording_metadata(
            strategy_session_id=session_id,
            strategy_revision_id=revision_id,
            observed_at=observed_at,
        )
        row = {
            "schema_version": PARK_STRATEGY_WINDOW_SCHEMA,
            "event": "record_window_amendment" if late_amendment else "record_window_observed",
            **metadata,
            "late_amendment": bool(late_amendment),
            "authority": "facts_package_review_only",
        }
        _atomic_append(self.path, row)
        return dict(row)

    def close_session(
        self,
        *,
        strategy_session_id: str,
        strategy_revision_id: str,
        observed_at: datetime | str,
        reason: str,
    ) -> dict[str, Any]:
        """Close one terminal Park session without changing any position."""

        session_id = _required_text(strategy_session_id, "strategy_session_id")
        revision_id = _required_text(strategy_revision_id, "strategy_revision_id")
        active = self.active_session()
        if not active:
            raise ParkStrategyIdentityError("strategy session is not active")
        if (
            active.get("strategy_session_id") != session_id
            or active.get("strategy_revision_id") != revision_id
        ):
            raise ParkStrategyIdentityError("strategy session identity is stale")
        reason_text = _required_text(reason, "reason")
        metadata = recording_metadata(
            strategy_session_id=session_id,
            strategy_revision_id=revision_id,
            observed_at=observed_at,
        )
        existing = next(
            (
                row
                for row in reversed(self.rows())
                if row.get("event") == "session_closed"
                and row.get("strategy_session_id") == session_id
                and row.get("strategy_revision_id") == revision_id
            ),
            None,
        )
        if existing:
            return dict(existing)
        row = {
            "schema_version": PARK_STRATEGY_SESSION_SCHEMA,
            "event": "session_closed",
            **metadata,
            "reason": reason_text,
            "execution_authority": "strategy_session_revision",
            "recording_authority": "facts_package_review_only",
        }
        _atomic_append(self.path, row)
        return dict(row)
