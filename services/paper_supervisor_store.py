"""Crash-safe, Paper-only persistence for Supervisor start attempts.

This module deliberately has no control-plane or execution-adapter import.
Callers may supply one public Paper start operation, but the store first makes
the intent durable and permanently spends its ``prepared_start_id``.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import socket
import tempfile
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Sequence

from services.paper_supervisor_evidence import (
    RunningEvidenceError,
    finalize_running_evidence,
    validate_running_evidence,
)


EVENT_SCHEMA_VERSION = "paper-supervisor-convergence-event-v1"
STATE_SCHEMA_VERSION = "paper-supervisor-cycle-state-v1"
LEASE_SCHEMA_VERSION = "paper-supervisor-lease-v1"
AUTHORITY_SCHEMA_VERSION = "paper-supervisor-start-authority-v1"
OBSERVATION_SCHEMA_VERSION = "paper-supervisor-observation-v1"
_EVENT_TYPES = frozenset(
    {
        "tick_claimed",
        "typed_heartbeat_observed",
        "pre_intent_attempt_started",
        "pre_intent_prepare_succeeded",
        "pre_intent_attempt_finished",
        "pre_intent_attempt_abandoned",
        "start_intent",
        "start_result",
        "recovery_result",
    }
)
_TICK_TRUST = frozenset({"fresh", "ambiguous"})
_RESULTS = frozenset({"accepted", "rejected", "unknown"})
_RECOVERY_RESULTS = frozenset(
    {"executed", "clean_rejection", "control_outcome_unknown"}
)
_PRE_INTENT_RESULTS = frozenset(
    {"prepare_succeeded", "no_action", "transient", "structural"}
)
_CYCLE_SUFFIXES = ("_DAY", "_NIGHT")
_UNSET = object()
MAX_OBSERVATION_LINE_BYTES = 1_048_576
MAX_OBSERVATIONS_PER_CYCLE = 10_000
MAX_OBSERVATION_CYCLE_BYTES = 256 * 1024 * 1024
STABLE_READ_ATTEMPTS = 3


class SupervisorStoreError(RuntimeError):
    """Stable fail-closed store error."""

    def __init__(self, code: str) -> None:
        self.code = str(code)
        super().__init__(self.code)


@dataclass(frozen=True)
class StartAuthoritySnapshot:
    """Exact read-only authority used to resolve one unfinished start."""

    cycle_id: str
    active_plan: Mapping[str, Any]
    runtime: Mapping[str, Any]
    accepted_order_fingerprints: Sequence[str]
    open_position_count: int
    reconciliation: Mapping[str, Any]
    control_events: Sequence[Mapping[str, Any]]
    accepted_order_identities: Sequence[Mapping[str, str]] = ()
    authorized_order_identities: Sequence[Mapping[str, str]] = ()
    open_position_identities: Sequence[Mapping[str, Any]] = ()
    running_evidence: Mapping[str, Any] | None = None
    schema_version: str = AUTHORITY_SCHEMA_VERSION

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "cycle_id": self.cycle_id,
            "active_plan": dict(self.active_plan),
            "runtime": dict(self.runtime),
            "accepted_order_fingerprints": list(
                self.accepted_order_fingerprints
            ),
            "accepted_order_identities": [
                dict(row) for row in self.accepted_order_identities
            ],
            "authorized_order_identities": [
                dict(row) for row in self.authorized_order_identities
            ],
            "open_position_identities": [
                dict(row) for row in self.open_position_identities
            ],
            "open_position_count": self.open_position_count,
            "reconciliation": dict(self.reconciliation),
            "control_events": [dict(row) for row in self.control_events],
            "running_evidence": (
                dict(self.running_evidence)
                if isinstance(self.running_evidence, Mapping)
                else None
            ),
        }


class PaperSupervisorStore:
    """Append-only event authority plus atomically projected cycle state."""

    def __init__(
        self,
        output_root: Path,
        *,
        now: Callable[[], datetime] | None = None,
        hostname: Callable[[], str] | None = None,
        pid: Callable[[], int] | None = None,
    ) -> None:
        self.output_root = Path(output_root)
        self.root = (
            self.output_root
            / "dualtrack"
            / "supervisor"
            / "convergence"
        )
        self.lock_path = self.root / ".lease.lock"
        self.lease_path = self.root / "lease.json"
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.hostname = hostname or socket.gethostname
        self.pid = pid or os.getpid
        self._lease_cache: dict[str, Any] | None = None

    @contextmanager
    def try_lease(
        self,
        cycle_id: str,
        *,
        holder_id: str,
    ) -> Iterator[SupervisorLease | None]:
        """Yield one non-blocking lease or ``None`` to a contender."""

        cycle = _cycle_id(cycle_id)
        holder = _identity(holder_id, "supervisor_lease_holder_invalid")
        self.root.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(
            self.lock_path,
            os.O_CREAT | os.O_RDWR,
            0o600,
        )
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                os.close(descriptor)
                descriptor = -1
                yield None
                return
            lease = SupervisorLease(
                store=self,
                descriptor=descriptor,
                lease_id=f"supervisor-lease-{uuid.uuid4().hex}",
                cycle_id=cycle,
                holder_id=holder,
                acquired_at=_utc(self.now()).isoformat(),
                lock_inode=int(os.fstat(descriptor).st_ino),
                source_tick_key=None,
                source_tick_claim_sequence=None,
                source_tick_claim_sha256=None,
            )
            self._write_lease_projection(lease, status="held")
            try:
                self._lease_cache = {
                    "lease_id": lease.lease_id,
                    "cycle_id": cycle,
                    "initialized": False,
                    "initializing": False,
                    "episode_loaded": False,
                    "episode": None,
                }
                yield lease
            finally:
                self._lease_cache = None
                lease._active = False
                self._write_lease_projection(lease, status="released")
                fcntl.flock(descriptor, fcntl.LOCK_UN)
                os.close(descriptor)
                descriptor = -1
        finally:
            if descriptor >= 0:
                os.close(descriptor)

    def events(self, cycle_id: str) -> list[dict[str, Any]]:
        """Read and validate the complete event chain."""

        cycle = _cycle_id(cycle_id)
        cache = self._cache_for_cycle(cycle)
        if cache is not None:
            return list(cache["events"])
        path = self._events_path(cycle)
        if not path.exists():
            return []
        if path.is_symlink() or not path.is_file():
            raise SupervisorStoreError("attempt_store_corrupt")
        lines = _stable_jsonl_lines(path)
        events: list[dict[str, Any]] = []
        previous_hash: str | None = None
        for expected_sequence, line in enumerate(lines, start=1):
            if not line.strip():
                raise SupervisorStoreError("attempt_store_corrupt")
            try:
                event = json.loads(line)
            except (json.JSONDecodeError, TypeError) as exc:
                raise SupervisorStoreError("attempt_store_corrupt") from exc
            _validate_event(
                event,
                cycle_id=cycle,
                expected_sequence=expected_sequence,
                previous_hash=previous_hash,
            )
            events.append(event)
            previous_hash = str(event["event_sha256"])
        _project(events, cycle_id=cycle)
        return events

    def read_cycle_snapshot(
        self,
        cycle_id: str,
    ) -> dict[str, Any]:
        """Return one mutually consistent event/observation/state prefix."""

        cycle = _cycle_id(cycle_id)
        for _attempt in range(STABLE_READ_ATTEMPTS):
            observations: list[dict[str, Any]] | None = None
            events = self.events(cycle)
            event_tail = _event_tail_identity(events)
            try:
                observations = self._observations(
                    cycle,
                    events=events,
                )
                observation_tail = _observation_tail_identity(
                    observations
                )
                self._validate_state_projection(
                    cycle,
                    events=events,
                )
                self._validate_episode_checkpoint(
                    cycle,
                    observations=observations,
                )
            except SupervisorStoreError:
                if self._snapshot_tails_changed(
                    cycle,
                    event_tail=event_tail,
                    observation_tail=(
                        _observation_tail_identity(observations)
                        if observations is not None
                        else None
                    ),
                ):
                    time.sleep(0.005)
                    continue
                raise
            if self._snapshot_tails_changed(
                cycle,
                event_tail=event_tail,
                observation_tail=observation_tail,
            ):
                time.sleep(0.005)
                continue
            return {
                "cycle_id": cycle,
                "events": events,
                "state": _project(events, cycle_id=cycle),
                "observations": observations,
            }
        raise SupervisorStoreError("attempt_store_busy")

    def _read_lease_snapshot(self, cycle_id: str) -> dict[str, Any]:
        """Validate each immutable chain once under the exclusive writer lease."""

        cycle = _cycle_id(cycle_id)
        event_path = self._events_path(cycle)
        observation_path = self.root / "observations" / f"{cycle}.jsonl"
        for _attempt in range(STABLE_READ_ATTEMPTS):
            before = (
                _file_identity(event_path),
                _file_identity(observation_path),
            )
            events = self.events(cycle)
            observations = self._observations(cycle, events=events)
            self._validate_state_projection(cycle, events=events)
            self._validate_episode_checkpoint(
                cycle,
                observations=observations,
            )
            after = (
                _file_identity(event_path),
                _file_identity(observation_path),
            )
            if before == after:
                return {
                    "cycle_id": cycle,
                    "events": events,
                    "state": _project(events, cycle_id=cycle),
                    "observations": observations,
                }
            time.sleep(0.005)
        raise SupervisorStoreError("attempt_store_busy")

    def _snapshot_tails_changed(
        self,
        cycle_id: str,
        *,
        event_tail: tuple[int, str | None],
        observation_tail: tuple[int, str | None] | None,
    ) -> bool:
        """Detect any append while a whole snapshot was being read."""

        events = self.events(cycle_id)
        if _event_tail_identity(events) != event_tail:
            return True
        try:
            observations = self._observations(
                cycle_id,
                events=events,
            )
        except SupervisorStoreError:
            # A writer may have appended an event and its anchored
            # observation between these two reads.  Re-check the event tail
            # before treating the cross-prefix validation error as corruption.
            if _event_tail_identity(self.events(cycle_id)) != event_tail:
                return True
            raise
        return (
            observation_tail is None
            or _observation_tail_identity(observations)
            != observation_tail
        )

    def _validate_state_projection(
        self,
        cycle_id: str,
        *,
        events: Sequence[Mapping[str, Any]],
    ) -> None:
        """Keep a truncated WAL from masquerading as an empty history."""

        path = self._state_path(cycle_id)
        if not path.exists():
            return
        if path.is_symlink() or not path.is_file():
            raise SupervisorStoreError("attempt_store_corrupt")
        try:
            stored = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError) as exc:
            raise SupervisorStoreError("attempt_store_corrupt") from exc
        _validate_stored_state(
            stored,
            events=events,
            cycle_id=cycle_id,
        )

    def _validate_episode_checkpoint(
        self,
        cycle_id: str,
        *,
        observations: Sequence[Mapping[str, Any]],
    ) -> None:
        path = self._episode_path(cycle_id)
        if not path.exists():
            return
        if path.is_symlink() or not path.is_file():
            raise SupervisorStoreError("attempt_store_corrupt")
        try:
            stored = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError) as exc:
            raise SupervisorStoreError("attempt_store_corrupt") from exc
        if (
            not isinstance(stored, dict)
            or str(stored.get("cycle_id") or "") != cycle_id
        ):
            raise SupervisorStoreError("attempt_store_corrupt")
        anchor = str(
            stored.get("last_observation_sha256") or ""
        )
        hashes = {
            str(row.get("observation_sha256") or "")
            for row in observations
        }
        if anchor and anchor not in hashes:
            raise SupervisorStoreError("attempt_store_corrupt")

    @staticmethod
    def project_event_prefix(
        cycle_id: str,
        events: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        """Validate and project a caller-selected immutable event prefix."""

        cycle = _cycle_id(cycle_id)
        previous_hash: str | None = None
        rows: list[dict[str, Any]] = []
        for expected_sequence, raw in enumerate(events, start=1):
            row = _json_object(raw, "attempt_store_corrupt")
            _validate_event(
                row,
                cycle_id=cycle,
                expected_sequence=expected_sequence,
                previous_hash=previous_hash,
            )
            rows.append(row)
            previous_hash = str(row["event_sha256"])
        return _project(rows, cycle_id=cycle)

    def current_state(self, cycle_id: str) -> dict[str, Any]:
        """Return deterministic state; reject a divergent projection."""

        cycle = _cycle_id(cycle_id)
        cache = self._cache_for_cycle(cycle)
        if cache is not None:
            return dict(cache["state"])
        events = self.events(cycle)
        projected = _project(events, cycle_id=cycle)
        self._validate_state_projection(
            cycle,
            events=events,
        )
        return projected

    def observations(
        self,
        cycle_id: str,
    ) -> list[dict[str, Any]]:
        """Return one complete, strictly validated observation chain."""

        return self._observations(cycle_id)

    def read_cycle_observation_snapshot(
        self,
        cycle_id: str,
    ) -> list[dict[str, Any]]:
        """Read one stable, validated observation prefix for utilization.

        Utilization only needs the immutable event/observation chains.  The
        full ``read_cycle_snapshot`` additionally re-reads and validates the
        projected state and episode checkpoint, which is required for current
        cycle control health but needlessly repeats the expensive history
        validation for every historical cycle in a utilization window.

        Event and observation files are each read as stable JSONL prefixes,
        and their sizes are checked again before returning.  If either file
        grows during the cross-file read, retry so an observation can never be
        validated against a partial event prefix.  The event chain and every
        observation payload remain fully validated by ``events`` and
        ``_observations``; this method only omits state/episode projection
        checks that are outside utilization's evidence boundary.
        """

        cycle = _cycle_id(cycle_id)
        events_path = self._events_path(cycle)
        observations_path = (
            self.root / "observations" / f"{cycle}.jsonl"
        )
        for _attempt in range(STABLE_READ_ATTEMPTS):
            event_size_before = _file_size(events_path)
            observation_size_before = _file_size(observations_path)
            events = self.events(cycle)
            observations = self._observations(
                cycle,
                events=events,
            )
            if (
                _file_size(events_path) != event_size_before
                or _file_size(observations_path)
                != observation_size_before
            ):
                time.sleep(0.005)
                continue
            return observations
        raise SupervisorStoreError("attempt_store_busy")

    def observation_cycle_ids(self) -> list[str]:
        """Return stable observation filenames; reject aliases and symlinks."""

        directory = self.root / "observations"
        if not directory.exists():
            return []
        if not directory.is_dir() or directory.is_symlink():
            raise SupervisorStoreError("attempt_store_corrupt")
        previous: tuple[str, ...] | None = None
        for _attempt in range(STABLE_READ_ATTEMPTS):
            try:
                entries = tuple(
                    sorted(
                        path.name
                        for path in directory.iterdir()
                    )
                )
            except OSError as exc:
                raise SupervisorStoreError(
                    "attempt_store_corrupt"
                ) from exc
            if previous is not None and entries == previous:
                break
            previous = entries
            time.sleep(0.005)
        else:
            raise SupervisorStoreError("attempt_store_busy")
        cycle_ids: list[str] = []
        for name in previous or ():
            path = directory / name
            if (
                path.is_symlink()
                or not path.is_file()
                or not name.endswith(".jsonl")
            ):
                raise SupervisorStoreError("attempt_store_corrupt")
            cycle = _cycle_id(name[:-6])
            if name != f"{cycle}.jsonl":
                raise SupervisorStoreError("attempt_store_corrupt")
            cycle_ids.append(cycle)
        return cycle_ids

    def observation_for_tick(
        self,
        cycle_id: str,
        source_tick_key: str,
    ) -> dict[str, Any] | None:
        key = _identity(
            source_tick_key,
            "supervisor_tick_key_invalid",
        )
        matches = [
            row
            for row in self._observations(cycle_id)
            if str(
                ((row.get("payload") or {}).get("tick_claim") or {}).get(
                    "source_tick_key"
                )
                or ""
            )
            == key
        ]
        if len(matches) > 1:
            raise SupervisorStoreError("attempt_store_corrupt")
        return dict(matches[0]) if matches else None

    def unfinished_tick(
        self,
        cycle_id: str,
    ) -> dict[str, Any] | None:
        """Join WAL claims to terminal observations without mutating either."""

        state = self.current_state(cycle_id)
        observations = self._observations(cycle_id)
        terminal_claims = {
            int(claim["claim_sequence"])
            for row in observations
            for claim in [
                dict((row.get("payload") or {}).get("tick_claim") or {})
            ]
            if isinstance(claim.get("claim_sequence"), int)
        }
        unfinished = [
            dict(row)
            for row in state.get("tick_claims") or []
            if int(row.get("claim_sequence") or 0)
            not in terminal_claims
        ]
        if len(unfinished) > 1:
            raise SupervisorStoreError("attempt_store_corrupt")
        return unfinished[0] if unfinished else None

    def unfinished_intent(self, cycle_id: str) -> dict[str, Any] | None:
        state = self.current_state(cycle_id)
        pending = state.get("unfinished_intent")
        return dict(pending) if isinstance(pending, dict) else None

    def unfinished_pre_intent(
        self,
        cycle_id: str,
    ) -> dict[str, Any] | None:
        state = self.current_state(cycle_id)
        pending = state.get("unfinished_pre_intent")
        return dict(pending) if isinstance(pending, dict) else None

    def episode_state(self, cycle_id: str) -> dict[str, Any] | None:
        """Read or recover the episode anchored to the observation chain."""

        cycle = _cycle_id(cycle_id)
        cache = self._cache_for_cycle(cycle)
        if cache is not None and cache.get("episode_loaded"):
            episode = cache.get("episode")
            return dict(episode) if isinstance(episode, Mapping) else None
        path = self._episode_path(cycle)
        stored: dict[str, Any] | None = None
        if path.exists():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError, TypeError) as exc:
                raise SupervisorStoreError("attempt_store_corrupt") from exc
            if (
                not isinstance(raw, dict)
                or str(raw.get("cycle_id") or "") != cycle
            ):
                raise SupervisorStoreError("attempt_store_corrupt")
            stored = raw
        observations = self._observations(cycle)
        if not observations:
            if stored is not None and stored.get(
                "last_observation_sha256"
            ):
                raise SupervisorStoreError("attempt_store_corrupt")
            if cache is not None:
                cache["episode"] = stored
                cache["episode_loaded"] = True
            return stored
        stored_anchor = (
            str(stored.get("last_observation_sha256") or "")
            if stored is not None
            else ""
        )
        chain_hashes = {
            str(row["observation_sha256"]) for row in observations
        }
        if stored_anchor and stored_anchor not in chain_hashes:
            raise SupervisorStoreError("attempt_store_corrupt")
        result = _reconstruct_episode_state(
            cycle_id=cycle,
            stored=stored,
            observations=observations,
        )
        if cache is not None:
            cache["episode"] = result
            cache["episode_loaded"] = True
        return result

    def write_episode_state(
        self,
        lease: SupervisorLease,
        state: Mapping[str, Any],
    ) -> None:
        """Atomically persist the exact episode state owned by this lease."""

        lease._require_active(self)
        payload = _json_object(
            state,
            "attempt_store_corrupt",
        )
        if str(payload.get("cycle_id") or "") != lease.cycle_id:
            raise SupervisorStoreError(
                "supervisor_authority_cycle_mismatch"
            )
        _atomic_write_json(
            self._episode_path(lease.cycle_id),
            payload,
        )
        cache = self._cache_for_lease(lease)
        if cache is not None:
            cache["episode"] = dict(payload)
            cache["episode_loaded"] = True

    def commit_episode_observation(
        self,
        lease: SupervisorLease,
        *,
        state: Mapping[str, Any],
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Append the authority first, then checkpoint its exact tail.

        If the process dies between these writes, ``episode_state`` recovers
        the compact snapshot plus event delta embedded in the append-only
        observation.  A truncated tail can no longer look valid because the
        checkpoint names its exact hash.
        """

        lease._require_active(self)
        episode = _json_object(state, "attempt_store_corrupt")
        if str(episode.get("cycle_id") or "") != lease.cycle_id:
            raise SupervisorStoreError(
                "supervisor_authority_cycle_mismatch"
            )
        observations = self._observations(lease.cycle_id)
        current_tail = (
            str(observations[-1]["observation_sha256"])
            if observations
            else ""
        )
        if (
            str(episode.get("last_observation_sha256") or "")
            != current_tail
        ):
            raise SupervisorStoreError("attempt_store_corrupt")
        events = self.events(lease.cycle_id)
        wal_anchor = {
            "event_sequence": len(events),
            "event_sha256": (
                str(events[-1]["event_sha256"])
                if events
                else None
            ),
        }
        claim = None
        if lease.source_tick_key is not None:
            claim_sequence = lease.source_tick_claim_sequence
            if (
                not isinstance(claim_sequence, int)
                or claim_sequence < 1
                or claim_sequence > len(events)
            ):
                raise SupervisorStoreError("attempt_store_corrupt")
            claim_event = events[claim_sequence - 1]
            if (
                claim_event.get("event_type") != "tick_claimed"
                or str(
                    (claim_event.get("payload") or {}).get(
                        "source_tick_key"
                    )
                    or ""
                )
                != lease.source_tick_key
                or str(claim_event.get("event_sha256") or "")
                != lease.source_tick_claim_sha256
            ):
                raise SupervisorStoreError("attempt_store_corrupt")
            claim = {
                "source_tick_key": lease.source_tick_key,
                "claim_sequence": claim_sequence,
                "claim_event_sha256": (
                    lease.source_tick_claim_sha256
                ),
                "heartbeat_digest": (
                    claim_event.get("payload") or {}
                ).get("heartbeat_digest"),
                "heartbeat_recorded_at": (
                    claim_event.get("payload") or {}
                ).get("heartbeat_recorded_at"),
                "trust": (
                    claim_event.get("payload") or {}
                ).get("trust"),
            }
        prior_event_count = _observation_episode_event_count(
            observations[-1] if observations else None
        )
        episode_events = episode.get("events", [])
        if (
            not isinstance(episode_events, list)
            or prior_event_count < 0
            or prior_event_count > len(episode_events)
        ):
            raise SupervisorStoreError("attempt_store_corrupt")
        episode_snapshot = {
            key: value
            for key, value in episode.items()
            if key not in {"events", "last_observation_sha256"}
        }
        episode_snapshot["event_count"] = len(episode_events)
        episode_snapshot["event_tail_digest"] = (
            str(episode_events[-1].get("event_digest") or "")
            if episode_events
            else None
        )
        episode_events_delta = episode_events[prior_event_count:]
        observation = self.append_observation(
            lease,
            {
                **_json_object(
                    payload,
                    "supervisor_event_payload_invalid",
                ),
                "wal_anchor": wal_anchor,
                "tick_claim": claim,
                "episode_snapshot": episode_snapshot,
                "episode_events_delta": episode_events_delta,
            },
        )
        committed = {
            **episode,
            "last_observation_sha256": observation[
                "observation_sha256"
            ],
        }
        self.write_episode_state(lease, committed)
        return committed

    def append_observation(
        self,
        lease: SupervisorLease,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Append and fsync one bounded convergence observation."""

        lease._require_active(self)
        detail = _json_object(
            payload,
            "supervisor_event_payload_invalid",
        )
        recorded_at = _utc(self.now()).isoformat()
        if "running_evidence" in detail:
            try:
                detail["running_evidence"] = (
                    finalize_running_evidence(
                        _json_object(
                            detail["running_evidence"],
                            "supervisor_event_payload_invalid",
                        ),
                        persisted_at=recorded_at,
                    )
                )
            except RunningEvidenceError as exc:
                raise SupervisorStoreError(
                    "supervisor_event_payload_invalid"
                ) from exc
        path = (
            self.root
            / "observations"
            / f"{lease.cycle_id}.jsonl"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        observations = self._observations(lease.cycle_id)
        cache = self._cache_for_lease(lease)
        if (
            cache is not None
            and _file_identity(path) != cache.get("observation_identity")
        ):
            raise SupervisorStoreError("attempt_store_corrupt")
        if len(observations) >= MAX_OBSERVATIONS_PER_CYCLE:
            raise SupervisorStoreError("attempt_store_capacity_exceeded")
        previous = observations[-1] if observations else None
        observation = {
            "schema_version": OBSERVATION_SCHEMA_VERSION,
            "sequence": len(observations) + 1,
            "observation_id": f"supervisor-observation-{uuid.uuid4().hex}",
            "recorded_at": recorded_at,
            "cycle_id": lease.cycle_id,
            "lease_id": lease.lease_id,
            "previous_observation_sha256": (
                previous.get("observation_sha256")
                if previous
                else None
            ),
            "payload": detail,
        }
        observation["observation_sha256"] = _digest(observation)
        line = _canonical(observation) + "\n"
        encoded = line.encode("utf-8")
        existing_size = path.stat().st_size if path.exists() else 0
        if (
            len(encoded) > MAX_OBSERVATION_LINE_BYTES
            or existing_size + len(encoded)
            > MAX_OBSERVATION_CYCLE_BYTES
        ):
            raise SupervisorStoreError("attempt_store_capacity_exceeded")
        try:
            descriptor = os.open(
                path,
                os.O_CREAT | os.O_APPEND | os.O_WRONLY,
                0o600,
            )
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "ab") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            _fsync_directory(path.parent)
        except OSError as exc:
            raise SupervisorStoreError("attempt_store_corrupt") from exc
        if cache is not None:
            cache["observations"] = [*observations, observation]
            cache["observation_identity"] = _file_identity(path)
        return observation

    def _last_observation(
        self,
        cycle_id: str,
    ) -> dict[str, Any] | None:
        """Validate one bounded 12h cycle chain and return its tail."""

        observations = self._observations(cycle_id)
        return observations[-1] if observations else None

    def _observations(
        self,
        cycle_id: str,
        *,
        events: Sequence[Mapping[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        """Validate one bounded 12h cycle chain."""

        cycle = _cycle_id(cycle_id)
        cache = self._cache_for_cycle(cycle)
        if cache is not None:
            return list(cache["observations"])
        path = self.root / "observations" / f"{cycle}.jsonl"
        if not path.exists():
            return []
        if path.is_symlink() or not path.is_file():
            raise SupervisorStoreError("attempt_store_corrupt")
        previous: dict[str, Any] | None = None
        observations: list[dict[str, Any]] = []
        lines = _stable_jsonl_lines(path)
        if len(lines) > MAX_OBSERVATIONS_PER_CYCLE:
            raise SupervisorStoreError("attempt_store_capacity_exceeded")
        event_rows = (
            [dict(row) for row in events]
            if events is not None
            else self.events(cycle)
        )
        source_tick_keys: set[str] = set()
        prior_episode_event_count = 0
        prior_episode_event_tail: str | None = None
        for expected_sequence, line in enumerate(lines, start=1):
            try:
                row = json.loads(line)
            except (json.JSONDecodeError, TypeError) as exc:
                raise SupervisorStoreError(
                    "attempt_store_corrupt"
                ) from exc
            if not isinstance(row, dict):
                raise SupervisorStoreError("attempt_store_corrupt")
            supplied_hash = str(
                row.get("observation_sha256") or ""
            )
            try:
                _parse_timestamp(row.get("recorded_at"))
                if _cycle_id(row.get("cycle_id")) != cycle:
                    raise SupervisorStoreError("attempt_store_corrupt")
                _identity(
                    row.get("observation_id"),
                    "attempt_store_corrupt",
                )
                _identity(
                    row.get("lease_id"),
                    "attempt_store_corrupt",
                )
                payload = _json_object(
                    row.get("payload"),
                    "attempt_store_corrupt",
                )
            except (SupervisorStoreError, ValueError) as exc:
                raise SupervisorStoreError(
                    "attempt_store_corrupt"
                ) from exc
            expected_previous = (
                previous.get("observation_sha256")
                if previous
                else None
            )
            if (
                row.get("schema_version")
                != OBSERVATION_SCHEMA_VERSION
                or row.get("sequence") != expected_sequence
                or not str(
                    row.get("observation_id") or ""
                ).startswith("supervisor-observation-")
                or not str(row.get("lease_id") or "").startswith(
                    "supervisor-lease-"
                )
                or row.get("previous_observation_sha256")
                != expected_previous
                or supplied_hash
                != _digest(
                    {
                        key: value
                        for key, value in row.items()
                        if key != "observation_sha256"
                    }
                )
            ):
                raise SupervisorStoreError("attempt_store_corrupt")
            running_evidence = payload.get("running_evidence")
            if running_evidence is not None:
                try:
                    validated_evidence = validate_running_evidence(
                        _json_object(
                            running_evidence,
                            "attempt_store_corrupt",
                        )
                    )
                except RunningEvidenceError as exc:
                    raise SupervisorStoreError(
                        "attempt_store_corrupt"
                    ) from exc
                if (
                    validated_evidence["cycle_id"] != cycle
                    or validated_evidence["persisted_at"]
                    != _utc(_parse_timestamp(row["recorded_at"])).isoformat()
                ):
                    raise SupervisorStoreError(
                        "attempt_store_corrupt"
                    )
            wal_anchor = payload.get("wal_anchor")
            tick_claim = payload.get("tick_claim")
            if wal_anchor is not None:
                anchor = _json_object(
                    wal_anchor,
                    "attempt_store_corrupt",
                )
                _validate_wal_anchor(anchor, event_rows)
            if tick_claim is not None:
                claim = _json_object(
                    tick_claim,
                    "attempt_store_corrupt",
                )
                _validate_tick_claim_anchor(
                    claim,
                    events=event_rows,
                    wal_anchor=wal_anchor,
                )
                key = str(claim["source_tick_key"])
                if key in source_tick_keys:
                    raise SupervisorStoreError(
                        "attempt_store_corrupt"
                    )
                source_tick_keys.add(key)
            if payload.get("episode_snapshot") is not None:
                (
                    prior_episode_event_count,
                    prior_episode_event_tail,
                ) = _validate_episode_observation_payload(
                    payload,
                    previous_count=prior_episode_event_count,
                    previous_tail=prior_episode_event_tail,
                )
            elif payload.get("episode_state") is not None:
                legacy_episode = _json_object(
                    payload.get("episode_state"),
                    "attempt_store_corrupt",
                )
                legacy_events = legacy_episode.get("events")
                if not isinstance(legacy_events, list):
                    raise SupervisorStoreError(
                        "attempt_store_corrupt"
                    )
                prior_episode_event_count = len(legacy_events)
                prior_episode_event_tail = (
                    str(
                        (legacy_events[-1] or {}).get(
                            "event_digest"
                        )
                        or ""
                    )
                    if legacy_events
                    else None
                )
            previous = row
            observations.append(row)
        return observations

    def _cache_for_cycle(self, cycle_id: str) -> dict[str, Any] | None:
        cache = self._lease_cache
        if cache is None or str(cache.get("cycle_id") or "") != cycle_id:
            return None
        if not cache.get("initialized"):
            if cache.get("initializing"):
                return None
            cache["initializing"] = True
            try:
                snapshot = self._read_lease_snapshot(cycle_id)
            finally:
                cache["initializing"] = False
            cache.update(
                {
                    "events": list(snapshot["events"]),
                    "state": dict(snapshot["state"]),
                    "observations": list(snapshot["observations"]),
                    "event_identity": _file_identity(
                        self._events_path(cycle_id)
                    ),
                    "observation_identity": _file_identity(
                        self.root
                        / "observations"
                        / f"{cycle_id}.jsonl"
                    ),
                    "initialized": True,
                }
            )
        return cache

    def _cache_for_lease(
        self,
        lease: SupervisorLease,
    ) -> dict[str, Any] | None:
        cache = self._cache_for_cycle(lease.cycle_id)
        if cache is None or cache.get("lease_id") != lease.lease_id:
            return None
        return cache

    def _append(
        self,
        lease: SupervisorLease,
        *,
        event_type: str,
        attempt_id: str,
        payload: Mapping[str, Any],
        bind_tick: bool = True,
    ) -> dict[str, Any]:
        lease._require_active(self)
        if event_type not in _EVENT_TYPES:
            raise SupervisorStoreError("supervisor_event_type_invalid")
        attempt = _identity(
            attempt_id,
            "supervisor_attempt_id_invalid",
        )
        projected = self.current_state(lease.cycle_id)
        events = self.events(lease.cycle_id)
        tick_key = None
        if bind_tick and lease.source_tick_key is not None:
            tick_key = _identity(
                lease.source_tick_key,
                "supervisor_tick_claim_missing",
            )
        elif bind_tick and projected.get("tick_claims"):
            raise SupervisorStoreError(
                "supervisor_tick_claim_missing"
            )
        self._validate_transition(
            projected,
            event_type=event_type,
            attempt_id=attempt,
            payload=payload,
            source_tick_key=tick_key,
        )
        canonical_payload = _json_object(
            payload,
            "supervisor_event_payload_invalid",
        )
        if tick_key is not None:
            if (
                canonical_payload.get("source_tick_key")
                not in {None, tick_key}
            ):
                raise SupervisorStoreError(
                    "supervisor_tick_claim_mismatch"
                )
            canonical_payload["source_tick_key"] = tick_key
        _validate_event_payload(
            event_type,
            canonical_payload,
            code="supervisor_event_payload_invalid",
        )
        event = {
            "schema_version": EVENT_SCHEMA_VERSION,
            "sequence": len(events) + 1,
            "event_type": event_type,
            "event_id": f"supervisor-event-{uuid.uuid4().hex}",
            "cycle_id": lease.cycle_id,
            "attempt_id": attempt,
            "recorded_at": _utc(self.now()).isoformat(),
            "lease_id": lease.lease_id,
            "source_tick_key": (
                canonical_payload.get("source_tick_key")
            ),
            "previous_event_sha256": (
                events[-1]["event_sha256"] if events else None
            ),
            "payload": canonical_payload,
        }
        event["event_sha256"] = _digest(event)
        cache = self._cache_for_lease(lease)
        if (
            cache is not None
            and _file_identity(self._events_path(lease.cycle_id))
            != cache.get("event_identity")
        ):
            raise SupervisorStoreError("attempt_store_corrupt")
        self._append_event_line(lease.cycle_id, event)
        updated_events = [*events, event]
        updated_state = _project(
            updated_events,
            cycle_id=lease.cycle_id,
        )
        self._write_state(lease.cycle_id, updated_state)
        if cache is not None:
            cache["events"] = updated_events
            cache["state"] = updated_state
            cache["event_identity"] = _file_identity(
                self._events_path(lease.cycle_id)
            )
        return event

    def _spend_prepared_start_global(
        self,
        lease: SupervisorLease,
        *,
        prepared_start_id: str,
    ) -> None:
        """Spend one prepared capability across every cycle."""

        lease._require_active(self)
        prepared = _identity(
            prepared_start_id,
            "prepared_start_id_invalid",
        )
        directory = self.root / "spent_prepared_starts"
        directory.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(prepared.encode("utf-8")).hexdigest()
        path = directory / f"{digest}.json"
        payload = {
            "schema_version": (
                "paper-supervisor-prepared-start-consumption-v1"
            ),
            "prepared_start_id": prepared,
            "cycle_id": lease.cycle_id,
            "consumed_at": _utc(self.now()).isoformat(),
            "reuse_allowed": False,
        }
        encoded = (_canonical(payload) + "\n").encode("utf-8")
        descriptor = -1
        try:
            descriptor = os.open(
                path,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                0o600,
            )
            os.write(descriptor, encoded)
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = -1
            _fsync_directory(directory)
        except FileExistsError as exc:
            raise SupervisorStoreError(
                "prepared_start_id_already_spent"
            ) from exc
        except OSError as exc:
            raise SupervisorStoreError("attempt_store_corrupt") from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)

    @staticmethod
    def _validate_transition(
        state: Mapping[str, Any],
        *,
        event_type: str,
        attempt_id: str,
        payload: Mapping[str, Any],
        source_tick_key: str | None,
    ) -> None:
        attempts = [
            row
            for row in state.get("attempts", [])
            if isinstance(row, dict)
        ]
        existing = next(
            (
                row
                for row in attempts
                if str(row.get("attempt_id") or "") == attempt_id
            ),
            None,
        )
        pre_attempts = [
            row
            for row in state.get("pre_intent_attempts", [])
            if isinstance(row, dict)
        ]
        pre_existing = next(
            (
                row
                for row in pre_attempts
                if str(row.get("attempt_id") or "") == attempt_id
            ),
            None,
        )
        if event_type == "tick_claimed":
            key = _identity(
                payload.get("source_tick_key"),
                "supervisor_tick_key_invalid",
            )
            if (
                source_tick_key is not None
                or attempt_id != key
                or any(
                    str(row.get("source_tick_key") or "") == key
                    for row in state.get("tick_claims") or []
                    if isinstance(row, Mapping)
                )
            ):
                raise SupervisorStoreError(
                    "supervisor_tick_claim_reused"
                )
            return
        if source_tick_key is None:
            tick_key = None
        else:
            tick_key = _identity(
                source_tick_key,
                "supervisor_tick_claim_missing",
            )
        if tick_key is None:
            claim = None
        else:
            claim = next(
                (
                    row
                    for row in state.get("tick_claims") or []
                    if isinstance(row, Mapping)
                    and str(row.get("source_tick_key") or "")
                    == tick_key
                ),
                None,
            )
        if tick_key is not None and claim is None:
            raise SupervisorStoreError(
                "supervisor_tick_claim_missing"
            )
        if event_type == "typed_heartbeat_observed":
            if tick_key is not None and any(
                str(row.get("source_tick_key") or "") == tick_key
                for row in state.get("typed_heartbeats", [])
                if isinstance(row, Mapping)
            ):
                raise SupervisorStoreError(
                    "supervisor_tick_attempt_reused"
                )
            if any(
                str(row.get("observation_id") or "") == attempt_id
                for row in state.get("typed_heartbeats", [])
                if isinstance(row, dict)
            ):
                raise SupervisorStoreError(
                    "supervisor_attempt_id_reused"
                )
            return
        if event_type == "pre_intent_attempt_started":
            if tick_key is not None and any(
                str(row.get("source_tick_key") or "") == tick_key
                for row in pre_attempts
            ):
                raise SupervisorStoreError(
                    "supervisor_tick_attempt_reused"
                )
            if pre_existing is not None or existing is not None:
                raise SupervisorStoreError(
                    "supervisor_attempt_id_reused"
                )
            if state.get("unfinished_pre_intent"):
                raise SupervisorStoreError(
                    "unfinished_pre_intent_requires_recovery"
                )
            return
        if event_type in {
            "pre_intent_prepare_succeeded",
            "pre_intent_attempt_finished",
            "pre_intent_attempt_abandoned",
        }:
            if pre_existing is None:
                raise SupervisorStoreError(
                    "supervisor_attempt_missing"
                )
            if pre_existing.get("terminal_result") is not None:
                raise SupervisorStoreError(
                    "supervisor_attempt_already_resolved"
                )
            if (
                tick_key is not None
                and event_type != "pre_intent_attempt_abandoned"
                and pre_existing.get("source_tick_key") is not None
                and pre_existing.get("source_tick_key")
                != tick_key
            ):
                raise SupervisorStoreError(
                    "supervisor_tick_claim_mismatch"
                )
            if (
                event_type == "pre_intent_prepare_succeeded"
                and pre_existing.get("prepare_succeeded_sequence")
                is not None
            ):
                raise SupervisorStoreError(
                    "supervisor_attempt_already_resolved"
                )
            return
        if event_type == "start_intent":
            if tick_key is not None and any(
                str(row.get("source_tick_key") or "") == tick_key
                for row in attempts
            ):
                raise SupervisorStoreError(
                    "supervisor_tick_attempt_reused"
                )
            if state.get("unfinished_intent"):
                raise SupervisorStoreError(
                    "unfinished_start_intent_requires_recovery"
                )
            if existing is not None:
                raise SupervisorStoreError("supervisor_attempt_id_reused")
            if (
                pre_existing is not None
                and pre_existing.get("terminal_result") is not None
            ):
                raise SupervisorStoreError(
                    "supervisor_attempt_already_resolved"
                )
            if (
                tick_key is not None
                and pre_existing is not None
                and pre_existing.get("source_tick_key")
                != tick_key
            ):
                raise SupervisorStoreError(
                    "supervisor_tick_claim_mismatch"
                )
            prepared_id = _identity(
                payload.get("prepared_start_id"),
                "prepared_start_id_invalid",
            )
            if prepared_id in set(
                state.get("spent_prepared_start_ids") or []
            ):
                raise SupervisorStoreError(
                    "prepared_start_id_already_spent"
                )
            _validate_intent_payload(payload)
            return
        if existing is None:
            raise SupervisorStoreError("supervisor_attempt_missing")
        if existing.get("terminal_result") is not None:
            raise SupervisorStoreError("supervisor_attempt_already_resolved")
        if (
            tick_key is not None
            and event_type != "recovery_result"
            and existing.get("source_tick_key") != tick_key
        ):
            raise SupervisorStoreError(
                "supervisor_tick_claim_mismatch"
            )
        if event_type == "start_result":
            if str(payload.get("result") or "") not in _RESULTS:
                raise SupervisorStoreError(
                    "supervisor_start_result_invalid"
                )
            _optional_code(payload.get("machine_code"))
            _required_digest(payload.get("response_digest"))
            return
        if event_type == "recovery_result":
            if str(payload.get("resolution") or "") not in _RECOVERY_RESULTS:
                raise SupervisorStoreError(
                    "supervisor_recovery_result_invalid"
                )
            _optional_code(payload.get("machine_code"))
            _required_digest(payload.get("authority_digest"))

    def _append_event_line(
        self,
        cycle_id: str,
        event: Mapping[str, Any],
    ) -> None:
        path = self._events_path(cycle_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        line = _canonical(event) + "\n"
        try:
            descriptor = os.open(
                path,
                os.O_CREAT | os.O_APPEND | os.O_WRONLY,
                0o600,
            )
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "a", encoding="utf-8") as handle:
                handle.write(line)
                handle.flush()
                os.fsync(handle.fileno())
            _fsync_directory(path.parent)
        except OSError as exc:
            raise SupervisorStoreError("attempt_store_corrupt") from exc

    def _write_state(
        self,
        cycle_id: str,
        state: Mapping[str, Any],
    ) -> None:
        _atomic_write_json(self._state_path(cycle_id), state)

    def _write_lease_projection(
        self,
        lease: SupervisorLease,
        *,
        status: str,
    ) -> None:
        payload = {
            "schema_version": LEASE_SCHEMA_VERSION,
            "status": status,
            "lease_id": lease.lease_id,
            "cycle_id": lease.cycle_id,
            "holder_id": lease.holder_id,
            "acquired_at": lease.acquired_at,
            "hostname": str(self.hostname() or ""),
            "pid": int(self.pid()),
            "updated_at": _utc(self.now()).isoformat(),
            "lock_path": str(self.lock_path),
            "lock_inode": lease.lock_inode,
            "lock_inode_is_stable": True,
        }
        _atomic_write_json(self.lease_path, payload)

    def _events_path(self, cycle_id: str) -> Path:
        return self.root / "events" / f"{cycle_id}.jsonl"

    def _state_path(self, cycle_id: str) -> Path:
        return self.root / "states" / f"{cycle_id}.json"

    def _episode_path(self, cycle_id: str) -> Path:
        return self.root / "episodes" / f"{cycle_id}.json"


class SupervisorLease:
    """Capability proving the caller owns the process-level writer lock."""

    def __init__(
        self,
        *,
        store: PaperSupervisorStore,
        descriptor: int,
        lease_id: str,
        cycle_id: str,
        holder_id: str,
        acquired_at: str,
        lock_inode: int,
        source_tick_key: str | None,
        source_tick_claim_sequence: int | None,
        source_tick_claim_sha256: str | None,
    ) -> None:
        self._store = store
        self._descriptor = descriptor
        self.lease_id = lease_id
        self.cycle_id = cycle_id
        self.holder_id = holder_id
        self.acquired_at = acquired_at
        self.lock_inode = lock_inode
        self.source_tick_key = source_tick_key
        self.source_tick_claim_sequence = source_tick_claim_sequence
        self.source_tick_claim_sha256 = source_tick_claim_sha256
        self._active = True

    def claim_tick(
        self,
        *,
        source_tick_key: str,
        heartbeat_digest: str,
        trust: str,
        claimed_at: str,
        heartbeat_recorded_at: str | None = None,
    ) -> dict[str, Any]:
        if self.source_tick_key is not None:
            raise SupervisorStoreError(
                "supervisor_tick_claim_already_bound"
            )
        if self._store.unfinished_tick(self.cycle_id) is not None:
            raise SupervisorStoreError(
                "unfinished_tick_requires_recovery"
            )
        event = self._store._append(
            self,
            event_type="tick_claimed",
            attempt_id=source_tick_key,
            payload={
                "source_tick_key": source_tick_key,
                "heartbeat_digest": heartbeat_digest,
                "heartbeat_recorded_at": heartbeat_recorded_at,
                "trust": trust,
                "claimed_at": claimed_at,
            },
            bind_tick=False,
        )
        self._bind_claim(event)
        return event

    def resume_tick(
        self,
        claim: Mapping[str, Any],
    ) -> None:
        if self.source_tick_key is not None:
            raise SupervisorStoreError(
                "supervisor_tick_claim_already_bound"
            )
        row = _json_object(claim, "attempt_store_corrupt")
        sequence = row.get("claim_sequence")
        event_hash = row.get("claim_event_sha256")
        if (
            not isinstance(sequence, int)
            or sequence < 1
            or not isinstance(event_hash, str)
            or len(event_hash) != 64
        ):
            raise SupervisorStoreError("attempt_store_corrupt")
        self.source_tick_key = _identity(
            row.get("source_tick_key"),
            "attempt_store_corrupt",
        )
        self.source_tick_claim_sequence = sequence
        self.source_tick_claim_sha256 = event_hash

    def _bind_claim(self, event: Mapping[str, Any]) -> None:
        payload = _json_object(
            event.get("payload"),
            "attempt_store_corrupt",
        )
        self.source_tick_key = _identity(
            payload.get("source_tick_key"),
            "attempt_store_corrupt",
        )
        self.source_tick_claim_sequence = int(event["sequence"])
        self.source_tick_claim_sha256 = str(event["event_sha256"])

    def record_typed_heartbeat(
        self,
        *,
        observation_id: str,
        observed_at: str,
        status: str,
        machine_code: str,
        reason: str,
        heartbeat_recorded_at: str | None,
        heartbeat_digest: str,
    ) -> dict[str, Any]:
        return self._store._append(
            self,
            event_type="typed_heartbeat_observed",
            attempt_id=observation_id,
            payload={
                "observed_at": str(observed_at),
                "status": str(status),
                "machine_code": str(machine_code),
                "reason": str(reason),
                "heartbeat_recorded_at": heartbeat_recorded_at,
                "heartbeat_digest": heartbeat_digest,
            },
        )

    def record_pre_intent_started(
        self,
        *,
        attempt_id: str,
        observed_at: str,
        phase_scope: str,
    ) -> dict[str, Any]:
        return self._store._append(
            self,
            event_type="pre_intent_attempt_started",
            attempt_id=attempt_id,
            payload={
                "observed_at": str(observed_at),
                "phase_scope": str(phase_scope),
            },
        )

    def record_pre_intent_finished(
        self,
        *,
        attempt_id: str,
        result: str,
        machine_code: str | None,
        classification: str | None,
        observed_at: str,
    ) -> dict[str, Any]:
        return self._store._append(
            self,
            event_type="pre_intent_attempt_finished",
            attempt_id=attempt_id,
            payload={
                "result": str(result),
                "machine_code": machine_code,
                "classification": classification,
                "observed_at": str(observed_at),
            },
        )

    def record_pre_intent_prepare_succeeded(
        self,
        *,
        attempt_id: str,
        observed_at: str,
    ) -> dict[str, Any]:
        return self._store._append(
            self,
            event_type="pre_intent_prepare_succeeded",
            attempt_id=attempt_id,
            payload={"observed_at": str(observed_at)},
        )

    def abandon_pre_intent(
        self,
        *,
        attempt_id: str,
        observed_at: str,
    ) -> dict[str, Any]:
        return self._store._append(
            self,
            event_type="pre_intent_attempt_abandoned",
            attempt_id=attempt_id,
            payload={
                "result": "transient",
                "machine_code": (
                    "supervisor_attempt_deadline_before_intent"
                ),
                "classification": "transient",
                "observed_at": str(observed_at),
            },
        )

    def record_start_intent(
        self,
        *,
        attempt_id: str,
        preview_id: str,
        prepared_start_id: str,
        plan_identity: Mapping[str, Any],
        expected_order_fingerprints: Sequence[str],
        pre_start_plan_identity: Mapping[str, Any] | None | object = _UNSET,
    ) -> dict[str, Any]:
        fingerprints = _fingerprints(expected_order_fingerprints)
        prepared = _identity(
            prepared_start_id,
            "prepared_start_id_invalid",
        )
        if self._store.current_state(
            self.cycle_id
        ).get("unfinished_intent"):
            raise SupervisorStoreError(
                "unfinished_start_intent_requires_recovery"
            )
        self._store._spend_prepared_start_global(
            self,
            prepared_start_id=prepared,
        )
        return self._store._append(
            self,
            event_type="start_intent",
            attempt_id=attempt_id,
            payload={
                "preview_id": _identity(
                    preview_id,
                    "supervisor_preview_id_invalid",
                ),
                "prepared_start_id": prepared,
                "plan_identity": _plan_identity(plan_identity),
                "pre_start_plan_identity": (
                    _plan_identity(plan_identity)
                    if pre_start_plan_identity is _UNSET
                    else (
                        _plan_identity(pre_start_plan_identity)
                        if pre_start_plan_identity
                        else None
                    )
                ),
                "expected_order_count": len(fingerprints),
                "expected_order_fingerprints": fingerprints,
            },
        )

    def record_start_result(
        self,
        *,
        attempt_id: str,
        result: str,
        machine_code: str | None = None,
        response_digest: str,
    ) -> dict[str, Any]:
        return self._store._append(
            self,
            event_type="start_result",
            attempt_id=attempt_id,
            payload={
                "result": str(result),
                "machine_code": (
                    str(machine_code) if machine_code is not None else None
                ),
                "response_digest": response_digest,
            },
        )

    def execute_start(
        self,
        *,
        attempt_id: str,
        preview_id: str,
        prepared_start_id: str,
        plan_identity: Mapping[str, Any],
        expected_order_fingerprints: Sequence[str],
        operation: Callable[[], Mapping[str, Any]],
        pre_start_plan_identity: Mapping[str, Any] | None | object = _UNSET,
    ) -> Mapping[str, Any]:
        """Persist intent, invoke once, then persist a bounded result."""

        self.record_start_intent(
            attempt_id=attempt_id,
            preview_id=preview_id,
            prepared_start_id=prepared_start_id,
            plan_identity=plan_identity,
            expected_order_fingerprints=expected_order_fingerprints,
            pre_start_plan_identity=pre_start_plan_identity,
        )
        response = operation()
        if not isinstance(response, Mapping):
            raise SupervisorStoreError("supervisor_start_response_invalid")
        result = str(response.get("result") or "")
        machine_code = response.get("machine_code")
        response_digest = _digest(dict(response))
        self.record_start_result(
            attempt_id=attempt_id,
            result=result,
            machine_code=(
                str(machine_code) if machine_code is not None else None
            ),
            response_digest=response_digest,
        )
        return response

    def recover_unfinished_intent(
        self,
        authority: StartAuthoritySnapshot,
    ) -> dict[str, Any] | None:
        """Resolve one unfinished intent without calling start again."""

        self._require_active(self._store)
        intent = self._store.unfinished_intent(self.cycle_id)
        if intent is None:
            return None
        resolution = resolve_start_outcome(intent, authority)
        self._store._append(
            self,
            event_type="recovery_result",
            attempt_id=str(intent["attempt_id"]),
            payload=resolution,
        )
        return resolution

    def _require_active(self, store: PaperSupervisorStore) -> None:
        if (
            not self._active
            or store is not self._store
            or self._descriptor < 0
        ):
            raise SupervisorStoreError("supervisor_lease_invalid")


def resolve_start_outcome(
    intent: Mapping[str, Any],
    authority: StartAuthoritySnapshot,
) -> dict[str, Any]:
    """Return the only safe recovery interpretation of exact authority."""

    snapshot = _json_object(
        authority.as_dict(),
        "supervisor_authority_invalid",
    )
    _validate_authority(snapshot)
    if str(intent.get("cycle_id") or "") != snapshot["cycle_id"]:
        raise SupervisorStoreError("supervisor_authority_cycle_mismatch")
    payload = _json_object(
        intent.get("payload"),
        "supervisor_intent_payload_invalid",
    )
    plan = _plan_identity(payload.get("plan_identity"))
    raw_active_plan = snapshot.get("active_plan")
    active_plan = (
        _plan_identity(raw_active_plan)
        if raw_active_plan
        else {}
    )
    pre_start_plan = (
        _plan_identity(payload.get("pre_start_plan_identity"))
        if payload.get("pre_start_plan_identity")
        else {}
    )
    expected_fingerprints = _fingerprints(
        payload.get("expected_order_fingerprints") or []
    )
    runtime = dict(snapshot["runtime"])
    actual_fingerprints = _fingerprints(
        snapshot["accepted_order_fingerprints"],
        allow_empty=True,
    )
    accepted_identities = _accepted_order_identities(
        snapshot.get("accepted_order_identities")
    )
    authorized_identities = _accepted_order_identities(
        snapshot.get("authorized_order_identities")
    )
    position_identities = _open_position_identities(
        snapshot.get("open_position_identities")
    )
    receipt_fingerprints = sorted(
        row["fingerprint"] for row in accepted_identities
    )
    authorized_by_order_id = {
        row["order_id"]: row
        for row in authorized_identities
    }
    position_fingerprints = [
        dict(authorized_by_order_id.get(row["trade_id"]) or {}).get(
            "fingerprint"
        )
        for row in position_identities
    ]
    complete_position_authority = all(
        fingerprint
        and _command_position_side(
            dict(authorized_by_order_id[row["trade_id"]])["side"]
        )
        == row["side"]
        and dict(authorized_by_order_id[row["trade_id"]])[
            "quantity"
        ]
        == row["order_quantity"]
        for row, fingerprint in zip(
            position_identities,
            position_fingerprints,
        )
        if row["trade_id"] in authorized_by_order_id
    ) and all(
        row["trade_id"] in authorized_by_order_id
        for row in position_identities
    )
    current_fingerprints = sorted(
        [
            *receipt_fingerprints,
            *[
                str(fingerprint)
                for fingerprint in position_fingerprints
                if fingerprint
            ],
        ]
    )
    reconciliation = dict(snapshot["reconciliation"])
    exact_reconciliation = (
        reconciliation.get("execution") == "ok"
        and reconciliation.get("accounting") == "pass"
    )
    runtime_plan_identity = (
        active_plan == plan
        and
        str(runtime.get("strategy_plan_id") or "")
        == plan["strategy_plan_id"]
        and int(runtime.get("strategy_plan_version") or 0)
        == plan["strategy_plan_version"]
    )
    runtime_identity = (
        runtime_plan_identity
        and str(runtime.get("preview_id") or "") == payload["preview_id"]
        and str(runtime.get("prepared_start_id") or "")
        == payload["prepared_start_id"]
    )
    matching_audit = _matching_control_events(
        snapshot["control_events"],
        cycle_id=snapshot["cycle_id"],
        preview_id=str(payload["preview_id"]),
        prepared_start_id=str(payload["prepared_start_id"]),
    )
    authority_digest = _digest(
        {
            "schema_version": snapshot["schema_version"],
            "cycle_id": snapshot["cycle_id"],
            "active_plan": active_plan,
            "runtime": runtime,
            "accepted_order_fingerprints": actual_fingerprints,
            "accepted_order_identities": accepted_identities,
            "authorized_order_identities": authorized_identities,
            "open_position_identities": position_identities,
            "open_position_count": snapshot["open_position_count"],
            "reconciliation": reconciliation,
            "matching_control_events": matching_audit,
        }
    )
    if (
        exact_reconciliation
        and runtime_identity
        and runtime.get("actual_state") == "running"
        and runtime.get("desired_state") == "running"
        and receipt_fingerprints == actual_fingerprints
        and complete_position_authority
        and current_fingerprints == expected_fingerprints
        and len(current_fingerprints) == len(set(current_fingerprints))
        and all(
            row["strategy_plan_id"] == plan["strategy_plan_id"]
            and row["strategy_plan_version"]
            == plan["strategy_plan_version"]
            for row in position_identities
        )
        and int(snapshot["open_position_count"])
        == len(position_identities)
        and all(
            row in authorized_identities
            for row in accepted_identities
        )
        and len(current_fingerprints)
        == int(payload["expected_order_count"])
        and len(matching_audit) == 1
        and matching_audit[0]["result"] == "accepted"
    ):
        return {
            "resolution": "executed",
            "machine_code": None,
            "authority_digest": authority_digest,
            "same_prepared_start_retry_allowed": False,
            "fresh_attempt_classification_required": False,
            "orders_created_by_recovery": 0,
        }
    pre_start_runtime_identity = (
        str(runtime.get("strategy_plan_id") or "")
        == pre_start_plan["strategy_plan_id"]
        and int(runtime.get("strategy_plan_version") or 0)
        == pre_start_plan["strategy_plan_version"]
        if pre_start_plan
        else (
            not str(runtime.get("strategy_plan_id") or "")
            and int(runtime.get("strategy_plan_version") or 0) == 0
        )
    )
    clean_runtime = (
        active_plan == pre_start_plan
        and pre_start_runtime_identity
        and runtime.get("actual_state") == "stopped"
        and runtime.get("desired_state") == "stopped"
        and str(runtime.get("preview_id") or "")
        in {"", str(payload["preview_id"])}
        and str(runtime.get("prepared_start_id") or "")
        in {"", str(payload["prepared_start_id"])}
    )
    if (
        exact_reconciliation
        and clean_runtime
        and actual_fingerprints == []
        and accepted_identities == []
        and position_identities == []
        and int(snapshot["open_position_count"]) == 0
        and len(matching_audit) == 1
        and matching_audit[0]["result"] == "rejected"
    ):
        return {
            "resolution": "clean_rejection",
            "machine_code": matching_audit[0]["machine_code"],
            "authority_digest": authority_digest,
            "same_prepared_start_retry_allowed": False,
            "fresh_attempt_classification_required": True,
            "orders_created_by_recovery": 0,
        }
    return {
        "resolution": "control_outcome_unknown",
        "machine_code": "control_outcome_unknown",
        "authority_digest": authority_digest,
        "same_prepared_start_retry_allowed": False,
        "fresh_attempt_classification_required": False,
        "orders_created_by_recovery": 0,
    }


def _matching_control_events(
    rows: Sequence[Mapping[str, Any]],
    *,
    cycle_id: str,
    preview_id: str,
    prepared_start_id: str,
) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    for raw in rows:
        if not isinstance(raw, Mapping):
            continue
        request = raw.get("request")
        if not isinstance(request, Mapping):
            continue
        if (
            raw.get("schema_version") != "strategy-control-event-v1"
            or str(raw.get("cycle_id") or "") != cycle_id
            or str(raw.get("action") or "") != "start"
            or str(request.get("expected_preview_id") or "") != preview_id
            or str(request.get("prepared_start_id") or "")
            != prepared_start_id
        ):
            continue
        result = str(raw.get("result") or "")
        if result not in {"accepted", "rejected"}:
            continue
        code = raw.get("error")
        matches.append(
            {
                "result": result,
                "machine_code": _machine_code_or_unknown(code),
            }
        )
    return matches


def _project(
    events: Sequence[Mapping[str, Any]],
    *,
    cycle_id: str,
) -> dict[str, Any]:
    tick_claims: list[dict[str, Any]] = []
    attempts: list[dict[str, Any]] = []
    by_id: dict[str, dict[str, Any]] = {}
    pre_intent_attempts: list[dict[str, Any]] = []
    pre_by_id: dict[str, dict[str, Any]] = {}
    typed_heartbeats: list[dict[str, Any]] = []
    spent: list[str] = []
    for event in events:
        event_type = str(event.get("event_type") or "")
        attempt_id = str(event.get("attempt_id") or "")
        payload = dict(event.get("payload") or {})
        source_tick_key = payload.get("source_tick_key")
        if event_type == "tick_claimed":
            if any(
                row["source_tick_key"] == source_tick_key
                for row in tick_claims
            ):
                raise SupervisorStoreError("attempt_store_corrupt")
            tick_claims.append(
                {
                    "source_tick_key": source_tick_key,
                    "heartbeat_digest": payload.get(
                        "heartbeat_digest"
                    ),
                    "heartbeat_recorded_at": payload.get(
                        "heartbeat_recorded_at"
                    ),
                    "trust": payload.get("trust"),
                    "claimed_at": payload.get("claimed_at"),
                    "claim_sequence": event["sequence"],
                    "claim_event_sha256": event["event_sha256"],
                }
            )
            continue
        if event_type == "typed_heartbeat_observed":
            if any(
                row["observation_id"] == attempt_id
                for row in typed_heartbeats
            ):
                raise SupervisorStoreError("attempt_store_corrupt")
            typed_heartbeats.append(
                {
                    "observation_id": attempt_id,
                    "sequence": event["sequence"],
                    "event_sha256": event["event_sha256"],
                    "source_tick_key": source_tick_key,
                    **payload,
                }
            )
            continue
        if event_type == "pre_intent_attempt_started":
            if attempt_id in pre_by_id or attempt_id in by_id:
                raise SupervisorStoreError("attempt_store_corrupt")
            row = {
                "attempt_id": attempt_id,
                "started_sequence": event["sequence"],
                "started_event_sha256": event["event_sha256"],
                "observed_at": payload["observed_at"],
                "phase_scope": payload["phase_scope"],
                "source_tick_key": source_tick_key,
                "prepare_succeeded_sequence": None,
                "prepare_succeeded_event_sha256": None,
                "terminal_result": None,
                "terminal_sequence": None,
                "terminal_event_sha256": None,
            }
            pre_intent_attempts.append(row)
            pre_by_id[attempt_id] = row
            continue
        if event_type == "pre_intent_prepare_succeeded":
            row = pre_by_id.get(attempt_id)
            if (
                row is None
                or row["terminal_result"] is not None
                or row["prepare_succeeded_sequence"] is not None
                or (
                    row.get("source_tick_key") is not None
                    and row.get("source_tick_key")
                    != source_tick_key
                )
            ):
                raise SupervisorStoreError("attempt_store_corrupt")
            row["prepare_succeeded_sequence"] = event["sequence"]
            row["prepare_succeeded_event_sha256"] = event[
                "event_sha256"
            ]
            row["prepare_succeeded_at"] = payload["observed_at"]
            continue
        if event_type in {
            "pre_intent_attempt_finished",
            "pre_intent_attempt_abandoned",
        }:
            row = pre_by_id.get(attempt_id)
            if (
                row is None
                or row["terminal_result"] is not None
                or (
                    event_type != "pre_intent_attempt_abandoned"
                    and
                    row.get("source_tick_key") is not None
                    and row.get("source_tick_key")
                    != source_tick_key
                )
            ):
                raise SupervisorStoreError("attempt_store_corrupt")
            row.update(
                {
                    "terminal_result": payload["result"],
                    "terminal_machine_code": payload.get(
                        "machine_code"
                    ),
                    "terminal_classification": payload.get(
                        "classification"
                    ),
                    "terminal_observed_at": payload["observed_at"],
                    "terminal_event_type": event_type,
                    "terminal_sequence": event["sequence"],
                    "terminal_event_sha256": event["event_sha256"],
                }
            )
            if event_type == "pre_intent_attempt_abandoned":
                row["recovery_source_tick_key"] = source_tick_key
            continue
        if event_type == "start_intent":
            if attempt_id in by_id:
                raise SupervisorStoreError("attempt_store_corrupt")
            prepared_id = str(payload.get("prepared_start_id") or "")
            if prepared_id in spent:
                raise SupervisorStoreError("attempt_store_corrupt")
            row = {
                "attempt_id": attempt_id,
                "intent_sequence": event["sequence"],
                "intent_recorded_at": event["recorded_at"],
                "preview_id": payload.get("preview_id"),
                "prepared_start_id": prepared_id,
                "plan_identity": payload.get("plan_identity"),
                "pre_start_plan_identity": payload.get(
                    "pre_start_plan_identity"
                ),
                "expected_order_count": payload.get(
                    "expected_order_count"
                ),
                "expected_order_fingerprints": payload.get(
                    "expected_order_fingerprints"
                ),
                "source_tick_key": source_tick_key,
                "terminal_result": None,
                "terminal_event_type": None,
                "terminal_sequence": None,
            }
            attempts.append(row)
            by_id[attempt_id] = row
            spent.append(prepared_id)
            pre = pre_by_id.get(attempt_id)
            if pre is not None:
                if (
                    pre["terminal_result"] is not None
                    or (
                        pre.get("source_tick_key") is not None
                        and pre.get("source_tick_key")
                        != source_tick_key
                    )
                ):
                    raise SupervisorStoreError("attempt_store_corrupt")
                pre.update(
                    {
                        "terminal_result": "start_intent",
                        "terminal_machine_code": None,
                        "terminal_classification": None,
                        "terminal_observed_at": event["recorded_at"],
                        "terminal_event_type": "start_intent",
                        "terminal_sequence": event["sequence"],
                        "terminal_event_sha256": event[
                            "event_sha256"
                        ],
                    }
                )
            continue
        row = by_id.get(attempt_id)
        if (
            row is None
            or row.get("terminal_result") is not None
            or (
                event_type != "recovery_result"
                and
                row.get("source_tick_key") is not None
                and row.get("source_tick_key")
                != source_tick_key
            )
        ):
            raise SupervisorStoreError("attempt_store_corrupt")
        row["terminal_result"] = (
            payload.get("result")
            if event_type == "start_result"
            else payload.get("resolution")
        )
        row["terminal_event_type"] = event_type
        row["terminal_machine_code"] = payload.get("machine_code")
        row["terminal_authority_digest"] = payload.get(
            "authority_digest"
        )
        row["terminal_response_digest"] = payload.get("response_digest")
        row["terminal_sequence"] = event["sequence"]
        row["terminal_recorded_at"] = event["recorded_at"]
        if event_type == "recovery_result":
            row["recovery_source_tick_key"] = source_tick_key
    unfinished = next(
        (
            {
                "cycle_id": cycle_id,
                "attempt_id": row["attempt_id"],
                "payload": {
                    "preview_id": row["preview_id"],
                    "prepared_start_id": row["prepared_start_id"],
                    "plan_identity": row["plan_identity"],
                    "pre_start_plan_identity": row[
                        "pre_start_plan_identity"
                    ],
                    "expected_order_count": row["expected_order_count"],
                    "expected_order_fingerprints": row[
                        "expected_order_fingerprints"
                    ],
                },
            }
            for row in attempts
            if row["terminal_result"] is None
        ),
        None,
    )
    if sum(row["terminal_result"] is None for row in attempts) > 1:
        raise SupervisorStoreError("attempt_store_corrupt")
    unfinished_pre_intent = next(
        (
            dict(row)
            for row in pre_intent_attempts
            if row["terminal_result"] is None
        ),
        None,
    )
    if (
        sum(
            row["terminal_result"] is None
            for row in pre_intent_attempts
        )
        > 1
    ):
        raise SupervisorStoreError("attempt_store_corrupt")
    last_hash = str(events[-1]["event_sha256"]) if events else None
    state = {
        "schema_version": STATE_SCHEMA_VERSION,
        "cycle_id": cycle_id,
        "last_sequence": len(events),
        "last_event_sha256": last_hash,
        "tick_claims": tick_claims,
        "attempt_count": len(attempts),
        "attempts": attempts,
        "pre_intent_attempts": pre_intent_attempts,
        "unfinished_pre_intent": unfinished_pre_intent,
        "typed_heartbeats": typed_heartbeats,
        "spent_prepared_start_ids": spent,
        "budget_floor": {
            "dangerous_start_attempts": sum(
                1
                for row in attempts
                if (
                    row.get("terminal_result")
                    == "control_outcome_unknown"
                    or (
                        row.get("terminal_event_type")
                        == "start_result"
                        and row.get("terminal_result") == "unknown"
                    )
                )
            ),
            "clean_refusal_observations": sum(
                1
                for row in attempts
                if row.get("terminal_event_type")
                == "recovery_result"
                and row.get("terminal_result") == "clean_rejection"
            ),
        },
        "unfinished_intent": unfinished,
        "status": "intent_pending" if unfinished else "idle",
    }
    state["state_digest"] = _digest(state)
    return state


def _validate_event(
    event: Any,
    *,
    cycle_id: str,
    expected_sequence: int,
    previous_hash: str | None,
) -> None:
    if not isinstance(event, dict):
        raise SupervisorStoreError("attempt_store_corrupt")
    if (
        event.get("schema_version") != EVENT_SCHEMA_VERSION
        or event.get("sequence") != expected_sequence
        or event.get("event_type") not in _EVENT_TYPES
        or str(event.get("cycle_id") or "") != cycle_id
        or event.get("previous_event_sha256") != previous_hash
        or not str(event.get("event_id") or "").startswith(
            "supervisor-event-"
        )
        or not str(event.get("lease_id") or "").startswith(
            "supervisor-lease-"
        )
    ):
        raise SupervisorStoreError("attempt_store_corrupt")
    _identity(event.get("attempt_id"), "attempt_store_corrupt")
    _parse_timestamp(event.get("recorded_at"))
    payload = _json_object(event.get("payload"), "attempt_store_corrupt")
    source_tick_key = event.get("source_tick_key")
    if source_tick_key is not None:
        _identity(source_tick_key, "attempt_store_corrupt")
    if payload.get("source_tick_key") != source_tick_key:
        raise SupervisorStoreError("attempt_store_corrupt")
    _validate_event_payload(
        str(event["event_type"]),
        payload,
        code="attempt_store_corrupt",
    )
    supplied_hash = str(event.get("event_sha256") or "")
    if supplied_hash != _digest(
        {key: value for key, value in event.items() if key != "event_sha256"}
    ):
        raise SupervisorStoreError("attempt_store_corrupt")


def _validate_stored_state(
    stored: Any,
    *,
    events: Sequence[Mapping[str, Any]],
    cycle_id: str,
) -> None:
    if not isinstance(stored, dict):
        raise SupervisorStoreError("attempt_store_corrupt")
    supplied_digest = str(stored.get("state_digest") or "")
    if supplied_digest != _digest(
        {key: value for key, value in stored.items() if key != "state_digest"}
    ):
        raise SupervisorStoreError("attempt_store_corrupt")
    stored_sequence = stored.get("last_sequence")
    if not isinstance(stored_sequence, int):
        raise SupervisorStoreError("attempt_store_corrupt")
    if stored_sequence < 0 or stored_sequence > len(events):
        raise SupervisorStoreError("attempt_store_corrupt")
    expected = _project(
        events[:stored_sequence],
        cycle_id=cycle_id,
    )
    if stored != expected:
        raise SupervisorStoreError("attempt_store_corrupt")


def _validate_event_payload(
    event_type: str,
    payload: Mapping[str, Any],
    *,
    code: str,
) -> None:
    try:
        if event_type == "tick_claimed":
            if payload.get("trust") not in _TICK_TRUST:
                raise SupervisorStoreError(code)
            _identity(payload.get("source_tick_key"), code)
            _required_digest(payload.get("heartbeat_digest"), code=code)
            if payload.get("heartbeat_recorded_at") is not None:
                _parse_timestamp(payload.get("heartbeat_recorded_at"))
            _parse_timestamp(payload.get("claimed_at"))
            return
        if payload.get("source_tick_key") is not None:
            _identity(payload.get("source_tick_key"), code)
        if event_type == "typed_heartbeat_observed":
            _parse_timestamp(payload.get("observed_at"))
            if payload.get("status") not in {"fresh", "missing"}:
                raise SupervisorStoreError(code)
            _identity(payload.get("machine_code"), code)
            _identity(payload.get("reason"), code)
            if payload.get("heartbeat_recorded_at") is not None:
                _parse_timestamp(payload.get("heartbeat_recorded_at"))
            _required_digest(payload.get("heartbeat_digest"), code=code)
            return
        if event_type == "pre_intent_attempt_started":
            _parse_timestamp(payload.get("observed_at"))
            if payload.get("phase_scope") != "create_or_prepare":
                raise SupervisorStoreError(code)
            return
        if event_type == "pre_intent_prepare_succeeded":
            _parse_timestamp(payload.get("observed_at"))
            return
        if event_type in {
            "pre_intent_attempt_finished",
            "pre_intent_attempt_abandoned",
        }:
            _parse_timestamp(payload.get("observed_at"))
            result = str(payload.get("result") or "")
            if result not in _PRE_INTENT_RESULTS:
                raise SupervisorStoreError(code)
            classification = payload.get("classification")
            machine_code = payload.get("machine_code")
            if result in {"prepare_succeeded", "no_action"}:
                if classification is not None or machine_code is not None:
                    raise SupervisorStoreError(code)
            elif classification not in {"transient", "structural"}:
                raise SupervisorStoreError(code)
            else:
                _identity(machine_code, code)
            if (
                event_type == "pre_intent_attempt_abandoned"
                and (
                    result != "transient"
                    or machine_code
                    != "supervisor_attempt_deadline_before_intent"
                    or classification != "transient"
                )
            ):
                raise SupervisorStoreError(code)
            return
        if event_type == "start_intent":
            _validate_intent_payload(payload)
            return
        if event_type == "start_result":
            if str(payload.get("result") or "") not in _RESULTS:
                raise SupervisorStoreError(code)
            _optional_code(payload.get("machine_code"))
            _required_digest(payload.get("response_digest"), code=code)
            return
        if event_type == "recovery_result":
            if (
                str(payload.get("resolution") or "")
                not in _RECOVERY_RESULTS
            ):
                raise SupervisorStoreError(code)
            _optional_code(payload.get("machine_code"))
            _required_digest(payload.get("authority_digest"), code=code)
            return
    except SupervisorStoreError as exc:
        raise SupervisorStoreError(code) from exc
    raise SupervisorStoreError(code)


def _validate_intent_payload(payload: Mapping[str, Any]) -> None:
    _identity(payload.get("preview_id"), "supervisor_preview_id_invalid")
    _identity(payload.get("prepared_start_id"), "prepared_start_id_invalid")
    _plan_identity(payload.get("plan_identity"))
    if payload.get("pre_start_plan_identity") is not None:
        _plan_identity(payload.get("pre_start_plan_identity"))
    fingerprints = _fingerprints(
        payload.get("expected_order_fingerprints") or []
    )
    if payload.get("expected_order_count") != len(fingerprints):
        raise SupervisorStoreError(
            "supervisor_expected_order_count_invalid"
        )


def _validate_authority(snapshot: Mapping[str, Any]) -> None:
    if (
        snapshot.get("schema_version") != AUTHORITY_SCHEMA_VERSION
        or not isinstance(snapshot.get("active_plan"), dict)
        or not isinstance(snapshot.get("runtime"), dict)
        or not isinstance(snapshot.get("reconciliation"), dict)
        or not isinstance(snapshot.get("control_events"), list)
    ):
        raise SupervisorStoreError("supervisor_authority_invalid")
    _cycle_id(snapshot.get("cycle_id"))
    _fingerprints(
        snapshot.get("accepted_order_fingerprints") or [],
        allow_empty=True,
    )
    _accepted_order_identities(
        snapshot.get("accepted_order_identities")
    )
    _accepted_order_identities(
        snapshot.get("authorized_order_identities")
    )
    position_identities = _open_position_identities(
        snapshot.get("open_position_identities")
    )
    count = snapshot.get("open_position_count")
    if (
        not isinstance(count, int)
        or count < 0
        or count != len(position_identities)
    ):
        raise SupervisorStoreError("supervisor_authority_invalid")


def _plan_identity(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise SupervisorStoreError("supervisor_plan_identity_invalid")
    plan_id = _identity(
        value.get("strategy_plan_id"),
        "supervisor_plan_identity_invalid",
    )
    version = value.get("strategy_plan_version")
    strategy_type = str(value.get("strategy_type") or "")
    direction = str(value.get("direction") or "")
    if (
        not isinstance(version, int)
        or version < 1
        or strategy_type not in {"grid", "dca"}
        or direction not in {"neutral", "long", "short"}
    ):
        raise SupervisorStoreError("supervisor_plan_identity_invalid")
    return {
        "strategy_plan_id": plan_id,
        "strategy_plan_version": version,
        "strategy_type": strategy_type,
        "direction": direction,
    }


def _accepted_order_identities(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        raise SupervisorStoreError("supervisor_authority_invalid")
    result: list[dict[str, str]] = []
    for raw in value:
        if not isinstance(raw, Mapping):
            raise SupervisorStoreError("supervisor_authority_invalid")
        order_id = _identity(
            raw.get("order_id"),
            "supervisor_authority_invalid",
        )
        fingerprint = _required_digest(
            raw.get("fingerprint"),
            code="supervisor_authority_invalid",
        )
        side = str(raw.get("side") or "").lower()
        quantity = _positive_decimal_text(
            raw.get("quantity"),
            code="supervisor_authority_invalid",
        )
        if side not in {"buy", "sell"}:
            raise SupervisorStoreError("supervisor_authority_invalid")
        result.append(
            {
                "order_id": order_id,
                "fingerprint": fingerprint,
                "side": side,
                "quantity": quantity,
            }
        )
    if (
        len({row["order_id"] for row in result}) != len(result)
        or len({row["fingerprint"] for row in result}) != len(result)
    ):
        raise SupervisorStoreError("supervisor_authority_invalid")
    return sorted(result, key=lambda row: row["order_id"])


def _open_position_identities(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise SupervisorStoreError("supervisor_authority_invalid")
    result: list[dict[str, Any]] = []
    used_fill_ids: set[str] = set()
    for raw in value:
        if not isinstance(raw, Mapping):
            raise SupervisorStoreError("supervisor_authority_invalid")
        position_id = _identity(
            raw.get("position_id"),
            "supervisor_authority_invalid",
        )
        trade_id = _identity(
            raw.get("trade_id"),
            "supervisor_authority_invalid",
        )
        plan_id = _identity(
            raw.get("strategy_plan_id"),
            "supervisor_authority_invalid",
        )
        plan_version = raw.get("strategy_plan_version")
        try:
            entry_price = Decimal(str(raw.get("entry_price")))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise SupervisorStoreError(
                "supervisor_authority_invalid"
            ) from exc
        entry_quantity = _positive_decimal_text(
            raw.get("entry_quantity"),
            code="supervisor_authority_invalid",
        )
        order_quantity = _positive_decimal_text(
            raw.get("order_quantity"),
            code="supervisor_authority_invalid",
        )
        if Decimal(entry_quantity) > Decimal(order_quantity):
            raise SupervisorStoreError("supervisor_authority_invalid")
        fill_ids = raw.get("entry_fill_ids")
        if (
            not isinstance(plan_version, int)
            or plan_version < 1
            or not entry_price.is_finite()
            or entry_price <= 0
            or not isinstance(fill_ids, list)
            or not fill_ids
        ):
            raise SupervisorStoreError("supervisor_authority_invalid")
        normalized_fill_ids = [
            _identity(fill_id, "supervisor_authority_invalid")
            for fill_id in fill_ids
        ]
        if (
            len(set(normalized_fill_ids)) != len(normalized_fill_ids)
            or any(fill_id in used_fill_ids for fill_id in normalized_fill_ids)
        ):
            raise SupervisorStoreError("supervisor_authority_invalid")
        used_fill_ids.update(normalized_fill_ids)
        result.append(
            {
                "position_id": position_id,
                "trade_id": trade_id,
                "entry_fill_ids": sorted(normalized_fill_ids),
                "strategy_plan_id": plan_id,
                "strategy_plan_version": plan_version,
                "side": _position_side(
                    raw.get("side"),
                    code="supervisor_authority_invalid",
                ),
                "entry_price": format(entry_price.normalize(), "f"),
                "entry_quantity": entry_quantity,
                "order_quantity": order_quantity,
            }
        )
    if (
        len({row["position_id"] for row in result}) != len(result)
        or len({row["trade_id"] for row in result}) != len(result)
    ):
        raise SupervisorStoreError("supervisor_authority_invalid")
    return sorted(result, key=lambda row: row["position_id"])


def _position_side(value: Any, *, code: str) -> str:
    side = str(value or "").lower()
    if side in {"long", "buy"}:
        return "long"
    if side in {"short", "sell"}:
        return "short"
    raise SupervisorStoreError(code)


def _command_position_side(value: Any) -> str:
    side = str(value or "").lower()
    if side == "buy":
        return "long"
    if side == "sell":
        return "short"
    raise SupervisorStoreError("supervisor_authority_invalid")


def _positive_decimal_text(value: Any, *, code: str) -> str:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise SupervisorStoreError(code) from exc
    if not parsed.is_finite() or parsed <= 0:
        raise SupervisorStoreError(code)
    return format(parsed.normalize(), "f")


def _fingerprints(
    values: Sequence[Any],
    *,
    allow_empty: bool = False,
) -> list[str]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise SupervisorStoreError(
            "supervisor_order_fingerprints_invalid"
        )
    result = [
        _required_digest(
            value,
            code="supervisor_order_fingerprints_invalid",
        )
        for value in values
    ]
    if (not result and not allow_empty) or len(result) != len(set(result)):
        raise SupervisorStoreError(
            "supervisor_order_fingerprints_invalid"
        )
    return sorted(result)


def _cycle_id(value: Any) -> str:
    cycle = str(value or "")
    suffix = next(
        (candidate for candidate in _CYCLE_SUFFIXES if cycle.endswith(candidate)),
        "",
    )
    if not suffix:
        raise SupervisorStoreError("supervisor_cycle_id_invalid")
    date_part = cycle[: -len(suffix)]
    try:
        parsed_date = datetime.fromisoformat(date_part)
    except ValueError as exc:
        raise SupervisorStoreError("supervisor_cycle_id_invalid") from exc
    if (
        len(date_part) != 10
        or parsed_date.time().isoformat() != "00:00:00"
        or cycle != f"{parsed_date.date().isoformat()}{suffix}"
    ):
        raise SupervisorStoreError("supervisor_cycle_id_invalid")
    return cycle


def _identity(value: Any, code: str) -> str:
    text = str(value or "").strip()
    if (
        not text
        or len(text) > 256
        or any(character not in _IDENTITY_CHARS for character in text)
    ):
        raise SupervisorStoreError(code)
    return text


_IDENTITY_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyz"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "0123456789"
    "._:-"
)


def _optional_code(value: Any) -> str | None:
    if value is None:
        return None
    return _identity(value, "supervisor_machine_code_invalid")


def _machine_code_or_unknown(value: Any) -> str | None:
    if value is None:
        return None
    try:
        return _identity(value, "supervisor_machine_code_invalid")
    except SupervisorStoreError:
        return "unknown_blocker"


def _required_digest(
    value: Any,
    *,
    code: str = "supervisor_digest_invalid",
) -> str:
    text = str(value or "").lower()
    if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
        raise SupervisorStoreError(code)
    return text


def _json_object(value: Any, code: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise SupervisorStoreError(code)
    try:
        copied = json.loads(_canonical(dict(value)))
    except (TypeError, ValueError) as exc:
        raise SupervisorStoreError(code) from exc
    if not isinstance(copied, dict):
        raise SupervisorStoreError(code)
    return copied


def _parse_timestamp(value: Any) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise SupervisorStoreError("attempt_store_corrupt") from exc
    if parsed.tzinfo is None:
        raise SupervisorStoreError("attempt_store_corrupt")
    return parsed.astimezone(timezone.utc)


def _event_tail_identity(
    events: Sequence[Mapping[str, Any]],
) -> tuple[int, str | None]:
    return (
        len(events),
        str(events[-1].get("event_sha256") or "")
        if events
        else None,
    )


def _observation_tail_identity(
    observations: Sequence[Mapping[str, Any]],
) -> tuple[int, str | None]:
    return (
        len(observations),
        str(observations[-1].get("observation_sha256") or "")
        if observations
        else None,
    )


def _stable_jsonl_lines(path: Path) -> list[str]:
    """Read one immutable prefix, retrying a concurrently appended tail."""

    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    for _attempt in range(STABLE_READ_ATTEMPTS):
        descriptor = -1
        try:
            descriptor = os.open(path, flags)
            before = os.fstat(descriptor)
            if before.st_size > MAX_OBSERVATION_CYCLE_BYTES:
                raise SupervisorStoreError(
                    "attempt_store_capacity_exceeded"
                )
            remaining = before.st_size
            chunks: list[bytes] = []
            while remaining:
                chunk = os.read(descriptor, min(65_536, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            after = os.fstat(descriptor)
        except OSError as exc:
            raise SupervisorStoreError("attempt_store_corrupt") from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        raw = b"".join(chunks)
        if (
            remaining == 0
            and before.st_size == after.st_size
            and (not raw or raw.endswith(b"\n"))
        ):
            try:
                lines = raw.decode("utf-8").splitlines()
            except UnicodeDecodeError as exc:
                raise SupervisorStoreError(
                    "attempt_store_corrupt"
                ) from exc
            if any(
                not line
                or len(line.encode("utf-8"))
                > MAX_OBSERVATION_LINE_BYTES
                for line in lines
            ):
                raise SupervisorStoreError(
                    "attempt_store_capacity_exceeded"
                )
            return lines
        time.sleep(0.005)
    raise SupervisorStoreError("attempt_store_busy")


def _file_size(path: Path) -> int:
    """Return a regular file's size, treating a missing chain as empty."""

    try:
        return path.stat().st_size
    except FileNotFoundError:
        return 0


def _file_identity(path: Path) -> tuple[int, int, int] | None:
    """Return exact regular-file identity without following a symlink."""

    try:
        stat = path.lstat()
    except FileNotFoundError:
        return None
    if path.is_symlink() or not path.is_file():
        raise SupervisorStoreError("attempt_store_corrupt")
    return (int(stat.st_ino), int(stat.st_size), int(stat.st_mtime_ns))


def _validate_wal_anchor(
    anchor: Mapping[str, Any],
    events: Sequence[Mapping[str, Any]],
) -> None:
    if set(anchor) != {"event_sequence", "event_sha256"}:
        raise SupervisorStoreError("attempt_store_corrupt")
    sequence = anchor.get("event_sequence")
    event_hash = anchor.get("event_sha256")
    if (
        not isinstance(sequence, int)
        or sequence < 0
        or sequence > len(events)
        or (sequence == 0 and event_hash is not None)
        or (
            sequence > 0
            and (
                not isinstance(event_hash, str)
                or event_hash
                != str(events[sequence - 1].get("event_sha256") or "")
            )
        )
    ):
        raise SupervisorStoreError("attempt_store_corrupt")


def _validate_tick_claim_anchor(
    claim: Mapping[str, Any],
    *,
    events: Sequence[Mapping[str, Any]],
    wal_anchor: Any,
) -> None:
    if set(claim) != {
        "source_tick_key",
        "claim_sequence",
        "claim_event_sha256",
        "heartbeat_digest",
        "heartbeat_recorded_at",
        "trust",
    }:
        raise SupervisorStoreError("attempt_store_corrupt")
    key = _identity(
        claim.get("source_tick_key"),
        "attempt_store_corrupt",
    )
    sequence = claim.get("claim_sequence")
    if (
        not isinstance(sequence, int)
        or sequence < 1
        or sequence > len(events)
        or claim.get("trust") not in _TICK_TRUST
    ):
        raise SupervisorStoreError("attempt_store_corrupt")
    _required_digest(
        claim.get("claim_event_sha256"),
        code="attempt_store_corrupt",
    )
    _required_digest(
        claim.get("heartbeat_digest"),
        code="attempt_store_corrupt",
    )
    if claim.get("heartbeat_recorded_at") is not None:
        _parse_timestamp(claim.get("heartbeat_recorded_at"))
    event = events[sequence - 1]
    payload = _json_object(
        event.get("payload"),
        "attempt_store_corrupt",
    )
    if (
        event.get("event_type") != "tick_claimed"
        or event.get("event_sha256")
        != claim["claim_event_sha256"]
        or payload.get("source_tick_key") != key
        or payload.get("heartbeat_digest")
        != claim["heartbeat_digest"]
        or payload.get("heartbeat_recorded_at")
        != claim["heartbeat_recorded_at"]
        or payload.get("trust") != claim["trust"]
    ):
        raise SupervisorStoreError("attempt_store_corrupt")
    anchor = _json_object(wal_anchor, "attempt_store_corrupt")
    if int(anchor.get("event_sequence") or 0) < sequence:
        raise SupervisorStoreError("attempt_store_corrupt")


def _validate_episode_observation_payload(
    payload: Mapping[str, Any],
    *,
    previous_count: int,
    previous_tail: str | None,
) -> tuple[int, str | None]:
    snapshot = _json_object(
        payload.get("episode_snapshot"),
        "attempt_store_corrupt",
    )
    delta = payload.get("episode_events_delta")
    if not isinstance(delta, list):
        raise SupervisorStoreError("attempt_store_corrupt")
    event_count = snapshot.get("event_count")
    tail = snapshot.get("event_tail_digest")
    if (
        not isinstance(event_count, int)
        or event_count < previous_count
        or event_count != previous_count + len(delta)
        or (
            tail is not None
            and not isinstance(tail, str)
        )
    ):
        raise SupervisorStoreError("attempt_store_corrupt")
    running_tail = previous_tail
    for sequence, raw in enumerate(
        delta,
        start=previous_count + 1,
    ):
        event = _json_object(raw, "attempt_store_corrupt")
        if event.get("sequence") != sequence:
            raise SupervisorStoreError("attempt_store_corrupt")
        supplied = str(event.get("event_digest") or "")
        if supplied != _digest(
            {
                key: value
                for key, value in event.items()
                if key != "event_digest"
            }
        ):
            raise SupervisorStoreError("attempt_store_corrupt")
        running_tail = supplied
    if (
        (event_count == 0 and tail is not None)
        or (event_count > 0 and tail != running_tail)
    ):
        raise SupervisorStoreError("attempt_store_corrupt")
    return event_count, running_tail


def _observation_episode_event_count(
    observation: Mapping[str, Any] | None,
) -> int:
    if observation is None:
        return 0
    payload = _json_object(
        observation.get("payload"),
        "attempt_store_corrupt",
    )
    if payload.get("episode_snapshot") is not None:
        snapshot = _json_object(
            payload["episode_snapshot"],
            "attempt_store_corrupt",
        )
        count = snapshot.get("event_count")
        if not isinstance(count, int) or count < 0:
            raise SupervisorStoreError("attempt_store_corrupt")
        return count
    if payload.get("episode_state") is None:
        return 0
    legacy = _json_object(
        payload.get("episode_state"),
        "attempt_store_corrupt",
    )
    events = legacy.get("events")
    if not isinstance(events, list):
        raise SupervisorStoreError("attempt_store_corrupt")
    return len(events)


def _reconstruct_episode_state(
    *,
    cycle_id: str,
    stored: Mapping[str, Any] | None,
    observations: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    state = (
        _json_object(stored, "attempt_store_corrupt")
        if stored is not None
        else None
    )
    anchor = (
        str(state.get("last_observation_sha256") or "")
        if state is not None
        else ""
    )
    start_index = 0
    if anchor:
        start_index = next(
            (
                index + 1
                for index, row in enumerate(observations)
                if str(row.get("observation_sha256") or "")
                == anchor
            ),
            -1,
        )
        if start_index < 0:
            raise SupervisorStoreError("attempt_store_corrupt")
        anchored = observations[start_index - 1]
        anchored_count = _observation_episode_event_count(anchored)
        stored_events = state.get("events")
        if (
            not isinstance(stored_events, list)
            or len(stored_events) != anchored_count
        ):
            raise SupervisorStoreError("attempt_store_corrupt")
    for row in observations[start_index:]:
        payload = _json_object(
            row.get("payload"),
            "attempt_store_corrupt",
        )
        legacy = payload.get("episode_state")
        if legacy is not None:
            embedded = _json_object(
                legacy,
                "attempt_store_corrupt",
            )
            if str(
                embedded.get("last_observation_sha256") or ""
            ) != str(row.get("previous_observation_sha256") or ""):
                raise SupervisorStoreError("attempt_store_corrupt")
            state = embedded
        else:
            snapshot = _json_object(
                payload.get("episode_snapshot"),
                "attempt_store_corrupt",
            )
            delta = payload.get("episode_events_delta")
            if not isinstance(delta, list):
                raise SupervisorStoreError("attempt_store_corrupt")
            prior_events = (
                list(state.get("events") or [])
                if state is not None
                else []
            )
            if len(prior_events) + len(delta) != snapshot.get(
                "event_count"
            ):
                raise SupervisorStoreError("attempt_store_corrupt")
            rebuilt = {
                key: value
                for key, value in snapshot.items()
                if key not in {"event_count", "event_tail_digest"}
            }
            rebuilt["events"] = [*prior_events, *delta]
            state = rebuilt
        if (
            not isinstance(state, dict)
            or str(state.get("cycle_id") or "") != cycle_id
        ):
            raise SupervisorStoreError("attempt_store_corrupt")
        state["last_observation_sha256"] = str(
            row["observation_sha256"]
        )
    if state is None:
        raise SupervisorStoreError("attempt_store_corrupt")
    return state


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise SupervisorStoreError("supervisor_timestamp_invalid")
    return value.astimezone(timezone.utc)


def _canonical(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _atomic_write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = _canonical(dict(value)) + "\n"
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        raise SupervisorStoreError("attempt_store_corrupt") from exc


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
