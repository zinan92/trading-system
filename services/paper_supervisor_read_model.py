"""Read-only Paper Supervisor history and conservative utilization projection."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
from typing import Any

from services.dualtrack_clock import cycle_window, cycle_window_from_id
from services.paper_supervisor_classifier import CLASSIFIER_VERSION
from services.paper_supervisor_evidence import same_running_identity
from services.paper_supervisor_store import (
    PaperSupervisorStore,
    SupervisorStoreError,
)


SUPERVISOR_READ_MODEL_SCHEMA_VERSION = "paper-supervisor-read-model-v1"
RUNTIME_UTILIZATION_SCHEMA_VERSION = "strategy-runtime-utilization-v2"
MAX_OBSERVATION_INTERVAL_SECONDS = 120
SUPERVISOR_POLLING_SUMMARY_SCHEMA_VERSION = (
    "paper-supervisor-polling-summary-v1"
)
SUPERVISOR_HISTORY_RESPONSE_SCHEMA_VERSION = (
    "paper-supervisor-history-response-v2"
)
SUPERVISOR_CURRENT_CYCLE_AUDIT_SCHEMA_VERSION = (
    "paper-supervisor-current-cycle-audit-v1"
)
_MAX_POLLING_EPISODE_BYTES = 2 * 1024 * 1024
_MAX_POLLING_OBSERVATION_BYTES = 2 * 1024 * 1024


def build_paper_supervisor_polling_summary(
    output_root: Path,
    *,
    cycle_id: str,
    as_of: str | datetime | None = None,
    utilization: Mapping[str, Any] | None = None,
    count_summary: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Read a bounded, checkpoint-anchored Supervisor tail for polling.

    This projection is observational only.  It deliberately does not walk the
    append-only event or observation histories; those remain available from
    ``build_paper_supervisor_history_response``.
    """

    observed = _utc(as_of)
    utilization_summary = (
        dict(utilization) if isinstance(utilization, Mapping) else {}
    )
    root = (
        Path(output_root)
        / "dualtrack"
        / "supervisor"
        / "convergence"
    )
    episode_path = root / "episodes" / f"{cycle_id}.json"
    observation_path = root / "observations" / f"{cycle_id}.jsonl"
    if not episode_path.exists() and not observation_path.exists():
        current = _polling_unavailable_cycle(cycle_id)
        current["status"] = "not_started"
        return {
            "schema_version": SUPERVISOR_POLLING_SUMMARY_SCHEMA_VERSION,
            "as_of": observed.isoformat(),
            "source": "paper_supervisor_checkpointed_tail",
            "classifier_version": CLASSIFIER_VERSION,
            "status": "not_started",
            "current_cycle": current,
            "utilization": utilization_summary,
            "source_errors": [],
            "history_query": _history_query(cycle_id),
            "read_only": True,
            "command_authority": False,
        }
    try:
        episode = _bounded_json_object(
            episode_path,
            max_bytes=_MAX_POLLING_EPISODE_BYTES,
        )
        tail = _bounded_jsonl_tail(
            observation_path,
            max_bytes=_MAX_POLLING_OBSERVATION_BYTES,
        )
        if str(episode.get("cycle_id") or "") != cycle_id:
            raise ValueError("attempt_store_corrupt")
        _validate_polling_observation_tail(tail, cycle_id=cycle_id)
        if _timestamp(tail.get("recorded_at")) > observed:
            raise ValueError("supervisor_polling_as_of_precedes_tail")
        tail_hash = str(tail.get("observation_sha256") or "")
        if str(episode.get("last_observation_sha256") or "") != tail_hash:
            raise ValueError("attempt_store_corrupt")
        payload = _polling_mapping(tail.get("payload"), required=True)
        snapshot = _polling_mapping(payload.get("episode_snapshot"))
        wal_anchor = _polling_mapping(payload.get("wal_anchor"))
        episode_identity = _polling_mapping(episode.get("episode"))
        episode_blocker = _polling_mapping(episode.get("blocker"))
        snapshot_blocker = _polling_mapping(snapshot.get("blocker"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return {
            "schema_version": SUPERVISOR_POLLING_SUMMARY_SCHEMA_VERSION,
            "as_of": observed.isoformat(),
            "source": "paper_supervisor_checkpointed_tail",
            "classifier_version": CLASSIFIER_VERSION,
            "status": "unavailable",
            "current_cycle": _polling_unavailable_cycle(cycle_id),
            "utilization": utilization_summary,
            "source_errors": [
                {
                    "cycle_id": cycle_id,
                    "machine_code": _safe_machine_code(exc),
                }
            ],
            "history_query": _history_query(cycle_id),
            "read_only": True,
            "command_authority": False,
        }

    blocker = episode_blocker or snapshot_blocker
    counts = dict(count_summary) if isinstance(count_summary, Mapping) else {}
    event_sequence = wal_anchor.get("event_sequence")
    current = {
        "cycle_id": cycle_id,
        "status": "available",
        "attempt_count": _nonnegative_int_or_none(counts.get("attempt_count")),
        "start_intent_count": _nonnegative_int_or_none(
            counts.get("start_intent_count")
        ),
        "last_observed_at": tail.get("recorded_at"),
        "last_result": {
            "status": payload.get("status"),
            "terminal_status": payload.get("terminal_status"),
            "machine_code": payload.get("machine_code"),
            "classification": payload.get("classification"),
            "observed_at": payload.get("observed_at"),
            "preview_id": payload.get("preview_id"),
            "prepared_start_id": payload.get("prepared_start_id"),
        },
        "last_attempt": counts.get("last_attempt"),
        "episode": {
            "mode": episode.get("mode") or snapshot.get("mode"),
            "episode_id": episode_identity.get("episode_id"),
            "next_attempt_at": episode_identity.get("next_attempt_at"),
            "budgets": episode.get("budgets") or snapshot.get("budgets"),
            "blocker": blocker or None,
            "alert_required": episode.get("alert_required"),
        },
        "history": {
            "status": "available_on_demand",
            "complete": False,
            "endpoint": "/api/trading-system/supervisor-history",
            "cycle_id": cycle_id,
            "event_count": _nonnegative_int_or_none(event_sequence),
            "observation_count": _nonnegative_int_or_none(
                tail.get("sequence")
            ),
            "tail_observation_sha256": tail.get("observation_sha256"),
        },
    }
    return {
        "schema_version": SUPERVISOR_POLLING_SUMMARY_SCHEMA_VERSION,
        "as_of": observed.isoformat(),
        "source": "paper_supervisor_checkpointed_tail",
        "classifier_version": CLASSIFIER_VERSION,
        "status": "available",
        "current_cycle": current,
        "utilization": utilization_summary,
        "source_errors": [],
        "history_query": _history_query(cycle_id),
        "read_only": True,
        "command_authority": False,
    }


def build_paper_supervisor_history_response(
    output_root: Path,
    *,
    cycle_id: str,
    as_of: str | datetime | None = None,
) -> dict[str, Any]:
    """Return the complete immutable audit projection on explicit request."""

    end = _utc(as_of)
    current, source_errors, _observation_cache = _read_current_cycle(
        PaperSupervisorStore(Path(output_root)),
        cycle_id=cycle_id,
        as_of=end,
    )
    model = {
        "schema_version": SUPERVISOR_CURRENT_CYCLE_AUDIT_SCHEMA_VERSION,
        "as_of": end.isoformat(),
        "source": "paper_supervisor_observations",
        "classifier_version": CLASSIFIER_VERSION,
        "status": "unavailable" if source_errors else current["status"],
        "current_cycle": current,
        "source_errors": source_errors,
        "read_only": True,
        "command_authority": False,
    }
    history = dict(current.get("history") or {})
    observations = list(history.get("observations") or [])
    events = list(history.get("events") or [])
    return {
        "schema_version": SUPERVISOR_HISTORY_RESPONSE_SCHEMA_VERSION,
        "cycle_id": cycle_id,
        "as_of": model.get("as_of"),
        "completeness": {
            "status": (
                "complete" if model.get("status") != "unavailable" else "unavailable"
            ),
            "deterministic_order": True,
            "event_count": history.get("event_count"),
            "observation_count": history.get("observation_count"),
            "attempt_count": current.get("attempt_count"),
            "start_intent_count": current.get("start_intent_count"),
            "event_tail_sha256": (
                events[-1].get("event_sha256") if events else None
            ),
            "observation_tail_sha256": (
                observations[-1].get("observation_sha256")
                if observations
                else None
            ),
        },
        "supervisor": model,
        "read_only": True,
        "command_authority": False,
    }

def build_paper_supervisor_read_model(
    output_root: Path,
    *,
    cycle_id: str,
    as_of: str | datetime | None = None,
) -> dict[str, Any]:
    """Project immutable Supervisor facts without invoking control behavior."""

    end = _utc(as_of)
    store = PaperSupervisorStore(Path(output_root))
    current, source_errors, observation_cache = _read_current_cycle(
        store,
        cycle_id=cycle_id,
        as_of=end,
    )

    utilization = _build_utilization(
        store,
        end=end,
        source_errors=source_errors,
        observation_cache=observation_cache,
    )
    return {
        "schema_version": SUPERVISOR_READ_MODEL_SCHEMA_VERSION,
        "as_of": end.isoformat(),
        "source": "paper_supervisor_observations",
        "classifier_version": CLASSIFIER_VERSION,
        "status": (
            "unavailable"
            if source_errors
            else current["status"]
        ),
        "current_cycle": current,
        "utilization": utilization,
        "source_errors": source_errors,
        "read_only": True,
        "command_authority": False,
    }


def _read_current_cycle(
    store: PaperSupervisorStore,
    *,
    cycle_id: str,
    as_of: datetime,
) -> tuple[
    dict[str, Any],
    list[dict[str, str]],
    dict[str, list[dict[str, Any]]],
]:
    """Read and validate one cycle without rebuilding rolling utilization."""

    source_errors: list[dict[str, str]] = []
    observation_cache: dict[str, list[dict[str, Any]]] = {}
    try:
        snapshot = store.read_cycle_snapshot(cycle_id)
        events = list(snapshot["events"])
        observations = list(snapshot["observations"])
        observation_cache[cycle_id] = observations
        current = _current_cycle_projection(
            store=store,
            cycle_id=cycle_id,
            events=events,
            observations=observations,
            as_of=as_of,
        )
    except (OSError, SupervisorStoreError, ValueError) as exc:
        source_errors.append(
            {
                "cycle_id": cycle_id,
                "machine_code": _safe_machine_code(exc),
            }
        )
        current = _unavailable_current_cycle(cycle_id)
    return current, source_errors, observation_cache

def build_paper_supervisor_utilization(
    output_root: Path,
    *,
    as_of: str | datetime | None = None,
) -> dict[str, Any]:
    """Compatibility entrypoint for callers that only need utilization."""

    end = _utc(as_of)
    return _build_utilization(
        PaperSupervisorStore(Path(output_root)),
        end=end,
        source_errors=[],
    )


def _bounded_json_object(path: Path, *, max_bytes: int) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ValueError("attempt_store_corrupt")
    stat_before = path.stat()
    if stat_before.st_size <= 0 or stat_before.st_size > max_bytes:
        raise ValueError("attempt_store_corrupt")
    raw = path.read_bytes()
    stat_after = path.stat()
    if (
        stat_before.st_ino != stat_after.st_ino
        or stat_before.st_size != stat_after.st_size
        or stat_before.st_mtime_ns != stat_after.st_mtime_ns
    ):
        raise ValueError("attempt_store_corrupt")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("attempt_store_corrupt")
    return value


def _polling_mapping(
    value: Any,
    *,
    required: bool = False,
) -> dict[str, Any]:
    if value is None and not required:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError("attempt_store_corrupt")
    return dict(value)


def _bounded_jsonl_tail(path: Path, *, max_bytes: int) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ValueError("attempt_store_corrupt")
    stat_before = path.stat()
    if stat_before.st_size <= 0:
        raise ValueError("attempt_store_corrupt")
    with path.open("rb") as handle:
        start = max(0, stat_before.st_size - max_bytes - 1)
        handle.seek(start)
        raw = handle.read(max_bytes + 1)
    stat_after = path.stat()
    if (
        stat_before.st_ino != stat_after.st_ino
        or stat_before.st_size != stat_after.st_size
        or stat_before.st_mtime_ns != stat_after.st_mtime_ns
    ):
        raise ValueError("attempt_store_corrupt")
    if start:
        boundary = raw.find(b"\n")
        if boundary < 0:
            raise ValueError("attempt_store_corrupt")
        raw = raw[boundary + 1 :]
    lines = raw.splitlines()
    if not lines:
        raise ValueError("attempt_store_corrupt")
    value = json.loads(lines[-1])
    if not isinstance(value, dict):
        raise ValueError("attempt_store_corrupt")
    return value


def _validate_polling_observation_tail(
    row: Mapping[str, Any],
    *,
    cycle_id: str,
) -> None:
    supplied = str(row.get("observation_sha256") or "")
    unsigned = {
        key: value
        for key, value in row.items()
        if key != "observation_sha256"
    }
    expected = hashlib.sha256(
        json.dumps(
            unsigned,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    if (
        row.get("schema_version") != "paper-supervisor-observation-v1"
        or str(row.get("cycle_id") or "") != cycle_id
        or not isinstance(row.get("sequence"), int)
        or int(row.get("sequence") or 0) < 1
        or not isinstance(row.get("payload"), dict)
        or supplied != expected
    ):
        raise ValueError("attempt_store_corrupt")


def _polling_unavailable_cycle(cycle_id: str) -> dict[str, Any]:
    current = _unavailable_current_cycle(cycle_id)
    current["history"] = {
        **_history_query(cycle_id),
        "status": "available_on_demand",
        "complete": False,
    }
    return current


def _history_query(cycle_id: str) -> dict[str, Any]:
    return {
        "endpoint": "/api/trading-system/supervisor-history",
        "cycle_id": cycle_id,
        "method": "GET",
    }


def _nonnegative_int_or_none(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _current_cycle_projection(
    *,
    store: PaperSupervisorStore,
    cycle_id: str,
    events: list[dict[str, Any]],
    observations: list[dict[str, Any]],
    as_of: datetime,
) -> dict[str, Any]:
    visible_events = [
        dict(row)
        for row in events
        if _timestamp(row.get("recorded_at")) <= as_of
    ]
    visible_observations = [
        dict(row)
        for row in observations
        if _timestamp(row.get("recorded_at")) <= as_of
    ]
    visible_state = store.project_event_prefix(
        cycle_id,
        visible_events,
    )
    tail = visible_observations[-1] if visible_observations else None
    payload = dict((tail or {}).get("payload") or {})
    episode = dict(payload.get("episode") or {})
    snapshot = dict(payload.get("episode_snapshot") or {})
    blocker = dict(snapshot.get("blocker") or {})
    if not blocker:
        blocker = dict(payload.get("blocker") or {})
    visible_event_tail = (
        int(visible_events[-1]["sequence"]) if visible_events else 0
    )
    history = {
        "event_count": len(visible_events),
        "events": visible_events,
        "observation_count": len(visible_observations),
        "observations": visible_observations,
        "typed_heartbeats": [
            dict(row)
            for row in visible_state.get("typed_heartbeats") or []
            if isinstance(row, Mapping)
            and int(row.get("sequence") or 0) <= visible_event_tail
        ],
        "pre_intent_attempts": [
            dict(row)
            for row in visible_state.get("pre_intent_attempts") or []
            if isinstance(row, Mapping)
            and int(row.get("started_sequence") or 0)
            <= visible_event_tail
        ],
        "start_attempts": [
            dict(row)
            for row in visible_state.get("attempts") or []
            if isinstance(row, Mapping)
            and int(row.get("intent_sequence") or 0)
            <= visible_event_tail
        ],
    }
    latest_attempt = _latest_attempt(history)
    return {
        "cycle_id": cycle_id,
        "status": "available" if tail else "not_started",
        "attempt_count": len(history["pre_intent_attempts"]),
        "start_intent_count": len(history["start_attempts"]),
        "last_observed_at": (
            tail.get("recorded_at") if tail else None
        ),
        "last_result": (
            {
                "status": payload.get("status"),
                "terminal_status": payload.get("terminal_status"),
                "machine_code": payload.get("machine_code"),
                "classification": payload.get("classification"),
                "observed_at": payload.get("observed_at"),
                "preview_id": payload.get("preview_id"),
                "prepared_start_id": payload.get(
                    "prepared_start_id"
                ),
            }
            if tail
            else None
        ),
        "last_attempt": latest_attempt,
        "episode": {
            "mode": (
                snapshot.get("mode")
                or episode.get("mode")
            ),
            "episode_id": (
                dict(snapshot.get("episode") or {}).get(
                    "episode_id"
                )
                or episode.get("episode_id")
            ),
            "next_attempt_at": (
                dict(snapshot.get("episode") or {}).get(
                    "next_attempt_at"
                )
                or episode.get("next_attempt_at")
            ),
            "budgets": snapshot.get("budgets") or episode.get("budgets"),
            "blocker": blocker or None,
            "alert_required": (
                snapshot.get("alert_required")
                if "alert_required" in snapshot
                else episode.get("alert_required")
            ),
        },
        "history": history,
    }


def _unavailable_current_cycle(cycle_id: str) -> dict[str, Any]:
    return {
        "cycle_id": cycle_id,
        "status": "unavailable",
        "attempt_count": None,
        "start_intent_count": None,
        "last_observed_at": None,
        "last_result": None,
        "last_attempt": None,
        "episode": {
            "mode": None,
            "episode_id": None,
            "next_attempt_at": None,
            "budgets": None,
            "blocker": None,
            "alert_required": None,
        },
        "history": {
            "event_count": None,
            "events": [],
            "observation_count": None,
            "observations": [],
            "typed_heartbeats": [],
            "pre_intent_attempts": [],
            "start_attempts": [],
        },
    }


def _build_utilization(
    store: PaperSupervisorStore,
    *,
    end: datetime,
    source_errors: list[dict[str, str]],
    observation_cache: dict[str, list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    observation_cache = observation_cache or {}
    windows: dict[str, Any] = {}
    for label, hours in (("24h", 24), ("7d", 24 * 7)):
        start = end - timedelta(hours=hours)
        cycle_ids = _cycle_ids_for_window(start, end)
        observations: list[dict[str, Any]] = []
        window_errors: list[dict[str, str]] = []
        for expected_cycle_id in cycle_ids:
            try:
                if expected_cycle_id in observation_cache:
                    rows = observation_cache[expected_cycle_id]
                else:
                    rows = store.read_cycle_observation_snapshot(
                        expected_cycle_id
                    )
                    observation_cache[expected_cycle_id] = rows
                observations.extend(rows)
            except (OSError, SupervisorStoreError, ValueError) as exc:
                window_errors.append(
                    {
                        "cycle_id": expected_cycle_id,
                        "machine_code": _safe_machine_code(exc),
                    }
                )
        source_errors.extend(
            row for row in window_errors if row not in source_errors
        )
        windows[label] = _utilization_window(
            observations,
            start=start,
            end=end,
            source_errors=window_errors,
        )
    return {
        "schema_version": RUNTIME_UTILIZATION_SCHEMA_VERSION,
        "as_of": end.isoformat(),
        "source": "paper_supervisor_running_evidence",
        "interval_policy": {
            "max_adjacent_seconds": (
                MAX_OBSERVATION_INTERVAL_SECONDS
            ),
            "missing_or_unknown_counts_as_running": False,
            "head_or_tail_extrapolation": False,
            "cross_cycle_bridge": False,
            "control_event_extrapolation": False,
        },
        "windows": windows,
    }


def _utilization_window(
    observations: list[dict[str, Any]],
    *,
    start: datetime,
    end: datetime,
    source_errors: list[dict[str, str]],
) -> dict[str, Any]:
    evidence_rows: list[tuple[datetime, dict[str, Any]]] = []
    time_regression = False
    previous_time: datetime | None = None
    ordered = sorted(
        observations,
        key=lambda row: (
            cycle_window_from_id(str(row["cycle_id"])).start,
            int(row.get("sequence") or 0),
        ),
    )
    for row in ordered:
        evidence = dict(
            dict(row.get("payload") or {}).get(
                "running_evidence"
            )
            or {}
        )
        if not evidence:
            continue
        evidence_at = _timestamp(evidence.get("evidence_at"))
        persisted_at = _timestamp(evidence.get("persisted_at"))
        if evidence_at > end or persisted_at > end:
            continue
        if previous_time is not None and evidence_at <= previous_time:
            time_regression = True
        previous_time = evidence_at
        evidence_rows.append((evidence_at, evidence))

    anchor_index = next(
        (
            index
            for index in range(len(evidence_rows) - 1, -1, -1)
            if evidence_rows[index][0] <= start
        ),
        None,
    )
    tail = evidence_rows[-1] if evidence_rows else None
    tail_age = (
        (end - tail[0]).total_seconds()
        if tail is not None
        else None
    )
    tail_fresh = (
        tail_age is not None
        and 0 <= tail_age <= MAX_OBSERVATION_INTERVAL_SECONDS
    )
    running_seconds = 0.0
    observed_seconds = 0.0
    missing_seconds = 0.0
    interior_missing_seconds = 0.0
    proven_interval_count = 0
    zero_interval_count = 0
    for (left_at, left), (right_at, right) in zip(
        evidence_rows,
        evidence_rows[1:],
    ):
        clipped_start = max(left_at, start)
        clipped_end = min(right_at, end)
        if clipped_end <= clipped_start:
            continue
        clipped_seconds = (clipped_end - clipped_start).total_seconds()
        delta = (right_at - left_at).total_seconds()
        if 0 < delta <= MAX_OBSERVATION_INTERVAL_SECONDS:
            observed_seconds += clipped_seconds
            if (
                left.get("running_proven") is True
                and right.get("running_proven") is True
                and same_running_identity(left, right)
                and left.get("cycle_id") == right.get("cycle_id")
            ):
                running_seconds += clipped_seconds
                proven_interval_count += 1
            else:
                zero_interval_count += 1
        else:
            missing_seconds += clipped_seconds
            interior_missing_seconds += clipped_seconds
            zero_interval_count += 1
    window_seconds = (end - start).total_seconds()
    unaccounted = max(
        0.0,
        window_seconds - observed_seconds - missing_seconds,
    )
    missing_seconds += unaccounted
    complete = (
        anchor_index is not None
        and tail_fresh
        and not time_regression
        and not source_errors
        and interior_missing_seconds == 0
    )
    conservative_percentage = round(
        100.0 * running_seconds / window_seconds,
        2,
    )
    return {
        "evidence_status": "complete" if complete else "insufficient",
        "running_seconds": (
            round(running_seconds, 3) if complete else None
        ),
        "conservative_running_seconds": round(running_seconds, 3),
        "window_seconds": window_seconds,
        "percentage": conservative_percentage if complete else None,
        "conservative_percentage": conservative_percentage,
        "window_start": start.isoformat(),
        "window_end": end.isoformat(),
        "anchor_observed_at": (
            evidence_rows[anchor_index][0].isoformat()
            if anchor_index is not None
            else None
        ),
        "tail_observed_at": tail[0].isoformat() if tail else None,
        "tail_age_seconds": (
            round(tail_age, 3) if tail_age is not None else None
        ),
        "observation_count": len(evidence_rows),
        "proven_interval_count": proven_interval_count,
        "zero_interval_count": zero_interval_count,
        "observed_seconds": round(observed_seconds, 3),
        "missing_seconds": round(missing_seconds, 3),
        "interior_missing_seconds": round(
            interior_missing_seconds,
            3,
        ),
        "ending_proof_status": (
            tail[1].get("proof_status") if tail else None
        ),
        "insufficient_reasons": [
            reason
            for reason, active in (
                ("window_head_missing", anchor_index is None),
                ("window_tail_stale", not tail_fresh),
                ("evidence_time_regression", time_regression),
                ("source_corrupt", bool(source_errors)),
                (
                    "interior_evidence_gap",
                    interior_missing_seconds > 0,
                ),
            )
            if active
        ],
        "source_errors": [dict(row) for row in source_errors],
    }


def _cycle_ids_for_window(
    start: datetime,
    end: datetime,
) -> list[str]:
    first = cycle_window(start)
    predecessor = cycle_window(first.start - timedelta(seconds=1))
    result = [predecessor.cycle_id]
    cursor = first.start
    while cursor <= end:
        window = cycle_window(cursor)
        if window.cycle_id not in result:
            result.append(window.cycle_id)
        if window.end <= cursor:
            raise ValueError("cycle_window_invalid")
        cursor = window.end
    return result


def _latest_attempt(state: Mapping[str, Any]) -> dict[str, Any] | None:
    rows = [
        dict(row)
        for row in state.get("pre_intent_attempts") or []
        if isinstance(row, Mapping)
    ]
    if not rows:
        return None
    row = rows[-1]
    return {
        "attempt_id": row.get("attempt_id"),
        "observed_at": row.get("observed_at"),
        "result": row.get("terminal_result"),
        "machine_code": row.get("terminal_machine_code"),
        "classification": row.get("terminal_classification"),
        "source_tick_key": row.get("source_tick_key"),
    }


def _safe_machine_code(exc: Exception) -> str:
    code = str(exc).strip()
    return (
        code
        if code
        in {
            "attempt_store_corrupt",
            "attempt_store_capacity_exceeded",
            "attempt_store_busy",
        }
        else "attempt_store_corrupt"
    )


def _utc(value: str | datetime | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(
            str(value).replace("Z", "+00:00")
        )
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _timestamp(value: Any) -> datetime:
    parsed = datetime.fromisoformat(
        str(value or "").replace("Z", "+00:00")
    )
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
