"""Independent Recording Track for Park strategy sessions."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Mapping


PARK_RECORDING_SCHEMA = "park-recording-track-v1"
REQUIRED_CATEGORIES = (
    "control",
    "plan",
    "orders",
    "fills",
    "positions",
    "exits",
    "telegram",
    "provider",
    "market_tick",
    "runtime",
    "reconciliation",
    "execution_path",
)


class ParkRecordingError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _text(value: Any, field: str) -> str:
    result = str(value or "").strip()
    if not result:
        raise ParkRecordingError("invalid_input", f"{field} is required")
    return result


def _digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


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
            row = json.loads(line)
            if not isinstance(row, dict):
                raise ParkRecordingError("journal_corrupt", "recording journal row is not an object")
            result.append(row)
    return result


class ParkRecordingTrack:
    def __init__(self, output_root: Path) -> None:
        self.root = Path(output_root) / "park_strategy" / "recording"
        self.path = self.root / "events.jsonl"
        self.package_path = self.root / "packages.jsonl"

    def events(self) -> list[dict[str, Any]]:
        return _rows(self.path)

    def packages(self) -> list[dict[str, Any]]:
        return _rows(self.package_path)

    def start_window(
        self,
        *,
        record_window_id: str,
        strategy_session_id: str,
        strategy_revision_id: str,
        starts_at: str,
        ends_at: str,
    ) -> dict[str, Any]:
        window = _text(record_window_id, "record_window_id")
        session = _text(strategy_session_id, "strategy_session_id")
        revision = _text(strategy_revision_id, "strategy_revision_id")
        manifests = [
            row
            for row in self.events()
            if row.get("event") == "manifest_started"
            and row.get("record_window_id") == window
        ]
        existing = next(
            (
                row
                for row in manifests
                if row.get("strategy_session_id") == session
                and row.get("strategy_revision_id") == revision
            ),
            None,
        )
        if existing:
            return dict(existing)
        # A recording window is a reporting slice, not an execution identity.
        # Different clean-slate session/revision pairs may therefore coexist in
        # one window. Reusing either half of an existing identity is still
        # ambiguous and remains fail-closed.
        if any(
            row.get("strategy_session_id") == session
            or row.get("strategy_revision_id") == revision
            for row in manifests
        ):
            raise ParkRecordingError(
                "window_identity_conflict",
                "recording window identity half is already bound to another revision",
            )
        row = {
            "schema_version": PARK_RECORDING_SCHEMA,
            "event": "manifest_started",
            "record_window_id": window,
            "strategy_session_id": session,
            "strategy_revision_id": revision,
            "starts_at": _text(starts_at, "starts_at"),
            "ends_at": _text(ends_at, "ends_at"),
            "recording_authority": "facts_package_review_only",
            "forbidden_effects": ["strategy_switch", "strategy_replan", "cancel_orders", "flatten_positions"],
        }
        _append(self.path, row)
        return dict(row)

    def record_event(
        self,
        *,
        record_window_id: str,
        strategy_session_id: str,
        strategy_revision_id: str,
        category: str,
        event_type: str,
        source: str,
        occurred_at: str,
        payload: Mapping[str, Any] | None = None,
        late: bool = False,
    ) -> dict[str, Any]:
        category_key = _text(category, "category")
        if category_key not in REQUIRED_CATEGORIES:
            raise ParkRecordingError("unknown_category", f"unsupported recording category: {category_key}")
        row = {
            "schema_version": PARK_RECORDING_SCHEMA,
            "event": "late_amendment" if late else "fact",
            "record_window_id": _text(record_window_id, "record_window_id"),
            "strategy_session_id": _text(strategy_session_id, "strategy_session_id"),
            "strategy_revision_id": _text(strategy_revision_id, "strategy_revision_id"),
            "category": category_key,
            "event_type": _text(event_type, "event_type"),
            "source": _text(source, "source"),
            "occurred_at": _text(occurred_at, "occurred_at"),
            "recorded_at": time.time(),
            "late": bool(late),
            "payload": dict(payload or {}),
            "payload_digest": _digest(payload or {}),
        }
        _append(self.path, row)
        return dict(row)

    def close_package(
        self,
        *,
        record_window_id: str,
        strategy_session_id: str,
        strategy_revision_id: str,
        strategy_open: bool,
        positions_open: int,
    ) -> dict[str, Any]:
        window = _text(record_window_id, "record_window_id")
        session = _text(strategy_session_id, "strategy_session_id")
        revision = _text(strategy_revision_id, "strategy_revision_id")
        window_events = [row for row in self.events() if row.get("record_window_id") == window]
        identities = sorted(
            {
                (
                    str(row.get("strategy_session_id") or ""),
                    str(row.get("strategy_revision_id") or ""),
                )
                for row in window_events
                if str(row.get("strategy_session_id") or "")
                and str(row.get("strategy_revision_id") or "")
            }
        )
        if (session, revision) not in identities:
            identities.append((session, revision))
            identities.sort()
        session_ids = [item[0] for item in identities]
        revision_ids = [item[1] for item in identities]
        existing = next((row for row in self.packages() if row.get("record_window_id") == window), None)
        if existing and existing.get("status") == "complete":
            existing_pairs = set(zip(existing.get("strategy_session_ids") or [], existing.get("strategy_revision_ids") or []))
            if set(identities).issubset(existing_pairs):
                return dict(existing)
        categories = {str(row.get("category")) for row in window_events if row.get("event") in {"fact", "late_amendment"}}
        missing = sorted(set(REQUIRED_CATEGORIES) - categories)
        if existing and existing.get("status") == "blocked_incomplete":
            if (
                int(existing.get("evidence_count") or 0) == len(window_events)
                and list(existing.get("missing_categories") or []) == missing
            ):
                return dict(existing)
        package = {
            "schema_version": PARK_RECORDING_SCHEMA,
            "event": "package_amended" if existing else "package_closed",
            "record_window_id": window,
            # Keep the original scalar fields for single-session consumers;
            # multi-session windows use the explicit identity lists below.
            "strategy_session_id": session_ids[0] if len(session_ids) == 1 else "",
            "strategy_revision_id": revision_ids[0] if len(revision_ids) == 1 else "",
            "strategy_session_ids": session_ids,
            "strategy_revision_ids": revision_ids,
            "status": "complete" if not missing else "blocked_incomplete",
            "missing_categories": missing,
            "strategy_open": bool(strategy_open),
            "positions_open": int(positions_open),
            "evidence_count": len(window_events),
            "watermark": max((float(row.get("recorded_at") or 0) for row in window_events), default=time.time()),
            "revision": int(existing.get("revision") or 0) + 1 if existing else 0,
            "execution_mutations": [],
            "next_action": "review_recorded_evidence" if not missing else "collect_missing_evidence",
        }
        _append(self.package_path, package)
        return dict(package)

    def amend_late_event(self, **kwargs: Any) -> dict[str, Any]:
        kwargs["late"] = True
        row = self.record_event(**kwargs)
        packages = [row for row in self.packages() if row.get("record_window_id") == kwargs.get("record_window_id")]
        if packages:
            package = packages[-1]
            window_events = [
                event
                for event in self.events()
                if event.get("record_window_id") == kwargs.get("record_window_id")
            ]
            identities = sorted(
                {
                    (
                        str(event.get("strategy_session_id") or ""),
                        str(event.get("strategy_revision_id") or ""),
                    )
                    for event in window_events
                    if str(event.get("strategy_session_id") or "")
                    and str(event.get("strategy_revision_id") or "")
                }
            )
            session_ids = [item[0] for item in identities]
            revision_ids = [item[1] for item in identities]
            categories = {str(event.get("category")) for event in window_events if event.get("event") in {"fact", "late_amendment"}}
            missing = sorted(set(REQUIRED_CATEGORIES) - categories)
            package_revision = int(package.get("revision") or 0) + 1
            amended = {
                **package,
                "event": "package_amended",
                "strategy_session_id": session_ids[0] if len(session_ids) == 1 else "",
                "strategy_revision_id": revision_ids[0] if len(revision_ids) == 1 else "",
                "strategy_session_ids": session_ids,
                "strategy_revision_ids": revision_ids,
                "revision": package_revision,
                "evidence_count": len(window_events),
                "watermark": max(float(package.get("watermark") or 0), float(row["recorded_at"])),
                "amendment_event_digest": row["payload_digest"],
                "status": "complete" if not missing else "blocked_incomplete",
                "missing_categories": missing,
                "next_action": "review_recorded_evidence" if not missing else "collect_missing_evidence",
            }
            _append(self.package_path, amended)
            return amended
        return row

    def review(self, *, record_window_id: str) -> dict[str, Any]:
        package = next((row for row in reversed(self.packages()) if row.get("record_window_id") == record_window_id), None)
        if not package:
            raise ParkRecordingError("package_missing", "recording package does not exist")
        events = [row for row in self.events() if row.get("record_window_id") == record_window_id]
        categories = {str(row.get("category")) for row in events}
        good = ["facts were recorded across the available categories"] if not package["missing_categories"] else []
        bad = [f"missing evidence: {', '.join(package['missing_categories'])}"] if package["missing_categories"] else []
        next_window = ["continue the same strategy session while preserving the exact revision"]
        if package["missing_categories"]:
            next_window.insert(0, "collect or repair the missing evidence before treating the package as complete")
        return {
            "record_window_id": record_window_id,
            "strategy_session_id": package["strategy_session_id"],
            "strategy_revision_id": package["strategy_revision_id"],
            "strategy_session_ids": list(
                package.get("strategy_session_ids")
                or ([package["strategy_session_id"]] if package.get("strategy_session_id") else [])
            ),
            "strategy_revision_ids": list(
                package.get("strategy_revision_ids")
                or ([package["strategy_revision_id"]] if package.get("strategy_revision_id") else [])
            ),
            "status": package["status"],
            "evidence_categories": sorted(categories),
            "did_well": good,
            "did_not": bad,
            "next_window": next_window,
            "pnl_claim": None,
        }
