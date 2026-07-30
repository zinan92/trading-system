"""Source-bound, append-only timing observations for natural Paper live ticks.

This module observes existing work. It has no control-plane dependency and
must never change whether a live-tick phase runs or whether its exception is
propagated.
"""

from __future__ import annotations

import json
import math
import os
import socket
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, TypeVar

from services.config_loader import ROOT
from services.paper_release_receipt import current_source_attestation
from services.scheduler_ownership import SchedulerOwnershipStore


SCHEMA_VERSION = "paper-live-tick-timing-v1"
REQUIRED_PHASES = (
    "lifecycle",
    "protective_sweep",
    "plan_sync",
    "intraday",
    "ledger",
    "heartbeat",
    "cycle_decision",
)
_T = TypeVar("_T")


class LiveTickTimingSession:
    """Measure phases without becoming part of their success contract."""

    def __init__(
        self,
        *,
        output_root: Path,
        cycle_id: str,
        observed_at: datetime,
        monotonic_ns: Callable[[], int] | None = None,
        wall_now: Callable[[], datetime] | None = None,
        source_attestation: Callable[[], dict[str, Any]] | None = None,
        hostname: Callable[[], str] | None = None,
        ownership: Callable[[], dict[str, Any]] | None = None,
        runtime_mode: str | None = None,
    ) -> None:
        self.output_root = Path(output_root)
        self.cycle_id = str(cycle_id)
        self.observed_at = _utc(observed_at)
        self.monotonic_ns = monotonic_ns or time.perf_counter_ns
        self.wall_now = wall_now or (lambda: datetime.now(timezone.utc))
        self.source_attestation = source_attestation or (
            lambda: current_source_attestation(ROOT)
        )
        self.hostname = hostname or socket.gethostname
        self.ownership = ownership or (
            lambda: SchedulerOwnershipStore(self.output_root).current()
        )
        self.runtime_mode = str(
            runtime_mode
            if runtime_mode is not None
            else os.getenv("GRIDMIND_RUNTIME_MODE") or "local"
        )
        self.started_monotonic_ns = self.monotonic_ns()
        self.started_at = _utc(self.wall_now())
        self.phases: list[dict[str, Any]] = []

    def measure(self, name: str, operation: Callable[[], _T]) -> _T:
        """Run exactly once, preserve its result/exception, and observe time."""

        started = self.monotonic_ns()
        status = "success"
        error_type = None
        try:
            return operation()
        except Exception as exc:
            status = "failed"
            error_type = type(exc).__name__
            raise
        finally:
            ended = self.monotonic_ns()
            self.phases.append(
                {
                    "name": str(name),
                    "status": status,
                    "duration_ms": _milliseconds(ended - started),
                    "error_type": error_type,
                }
            )

    def finish(self, *, status: str, error_type: str | None = None) -> dict[str, Any]:
        ended_monotonic_ns = self.monotonic_ns()
        ended_at = _utc(self.wall_now())
        total_ms = _milliseconds(
            ended_monotonic_ns - self.started_monotonic_ns
        )
        phase_sum_ms = round(
            sum(float(row["duration_ms"]) for row in self.phases),
            3,
        )
        source, source_error = self._safe_metadata(self.source_attestation)
        owner, owner_error = self._safe_metadata(self.ownership)
        try:
            host = str(self.hostname() or "").strip()
            host_error = None if host else "hostname_missing"
        except Exception as exc:  # pragma: no cover - defensive host seam
            host = ""
            host_error = type(exc).__name__
        metadata_errors = [
            item
            for item in (source_error, owner_error, host_error)
            if item
        ]
        receipt = {
            "schema_version": SCHEMA_VERSION,
            "status": str(status),
            "cycle_id": self.cycle_id,
            "observed_at": self.observed_at.isoformat(),
            "started_at": self.started_at.isoformat(),
            "ended_at": ended_at.isoformat(),
            "hostname": host,
            "runtime_mode": self.runtime_mode,
            "scheduler_owner": {
                "status": owner.get("status"),
                "active_owner_id": owner.get("active_owner_id"),
                "epoch": owner.get("epoch"),
            },
            "source": {
                "source_sha": source.get("source_sha"),
                "source_tree_sha": source.get("source_tree_sha"),
                "tracked_tree_clean": source.get("tracked_tree_clean"),
            },
            "phases": self.phases,
            "phase_sum_ms": phase_sum_ms,
            "total_duration_ms": total_ms,
            "unattributed_duration_ms": round(
                max(0.0, total_ms - phase_sum_ms),
                3,
            ),
            "control_actions_executed": 0,
            "control_action_scope": "timing_instrumentation_only",
            "metadata_status": "pass" if not metadata_errors else "invalid",
            "metadata_errors": metadata_errors,
            "error_type": str(error_type) if error_type else None,
        }
        self._append_without_affecting_tick(receipt)
        return receipt

    @staticmethod
    def _safe_metadata(
        resolver: Callable[[], dict[str, Any]],
    ) -> tuple[dict[str, Any], str | None]:
        try:
            value = resolver()
            if not isinstance(value, dict):
                return {}, "metadata_not_object"
            return value, None
        except Exception as exc:  # noqa: BLE001 - observation cannot break tick.
            return {}, type(exc).__name__

    def _append_without_affecting_tick(self, receipt: dict[str, Any]) -> None:
        try:
            directory = (
                self.output_root
                / "dualtrack"
                / "observability"
                / "live_tick_timing"
            )
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / f"{self.started_at.date().isoformat()}.jsonl"
            line = json.dumps(receipt, ensure_ascii=False, sort_keys=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        except Exception:
            # The existing tick and all safety gates retain their original
            # outcome. Missing timing evidence fails the later report closed.
            return


def validate_success_receipt(receipt: dict[str, Any]) -> dict[str, Any]:
    """Reject any row that cannot be a homogeneous Cloud timing sample."""

    if not isinstance(receipt, dict):
        raise ValueError("live_tick_timing_receipt_not_object")
    if receipt.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("live_tick_timing_schema_invalid")
    if receipt.get("status") != "success":
        raise ValueError("live_tick_timing_not_successful")
    if receipt.get("runtime_mode") != "cloud":
        raise ValueError("live_tick_timing_not_cloud")
    if receipt.get("metadata_status") != "pass":
        raise ValueError("live_tick_timing_metadata_invalid")
    if receipt.get("control_actions_executed") != 0:
        raise ValueError("live_tick_timing_control_action_detected")

    source = receipt.get("source")
    if not isinstance(source, dict):
        raise ValueError("live_tick_timing_source_missing")
    for key in ("source_sha", "source_tree_sha"):
        value = str(source.get(key) or "")
        if len(value) != 40 or any(
            character not in "0123456789abcdef" for character in value.lower()
        ):
            raise ValueError(f"live_tick_timing_{key}_invalid")
    if source.get("tracked_tree_clean") is not True:
        raise ValueError("live_tick_timing_source_tree_dirty")

    owner = receipt.get("scheduler_owner")
    if (
        not isinstance(owner, dict)
        or owner.get("status") != "active"
        or not str(owner.get("active_owner_id") or "").strip()
        or not isinstance(owner.get("epoch"), int)
        or int(owner["epoch"]) < 1
    ):
        raise ValueError("live_tick_timing_owner_invalid")
    if not str(receipt.get("hostname") or "").strip():
        raise ValueError("live_tick_timing_hostname_invalid")

    phases = receipt.get("phases")
    if not isinstance(phases, list):
        raise ValueError("live_tick_timing_phases_invalid")
    names = [str(row.get("name") or "") for row in phases if isinstance(row, dict)]
    if names != list(REQUIRED_PHASES):
        raise ValueError("live_tick_timing_phases_incomplete")
    if any(row.get("status") != "success" for row in phases):
        raise ValueError("live_tick_timing_phase_failed")

    durations = [_duration(row.get("duration_ms")) for row in phases]
    total = _duration(receipt.get("total_duration_ms"))
    phase_sum = _duration(receipt.get("phase_sum_ms"))
    unattributed = _duration(receipt.get("unattributed_duration_ms"))
    if abs(sum(durations) - phase_sum) > 0.01:
        raise ValueError("live_tick_timing_phase_sum_invalid")
    if abs(total - phase_sum - unattributed) > 0.01:
        raise ValueError("live_tick_timing_total_does_not_close")

    _parse_utc_field(receipt, "observed_at")
    started = _parse_utc_field(receipt, "started_at")
    ended = _parse_utc_field(receipt, "ended_at")
    if ended < started:
        raise ValueError("live_tick_timing_wall_clock_invalid")
    if not str(receipt.get("cycle_id") or "").strip():
        raise ValueError("live_tick_timing_cycle_missing")
    return receipt


def read_timing_receipts(output_root: Path) -> list[dict[str, Any]]:
    directory = (
        Path(output_root)
        / "dualtrack"
        / "observability"
        / "live_tick_timing"
    )
    rows: list[dict[str, Any]] = []
    for path in sorted(directory.glob("*.jsonl")) if directory.is_dir() else []:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError("live_tick_timing_json_invalid") from exc
            if not isinstance(row, dict):
                raise ValueError("live_tick_timing_receipt_not_object")
            rows.append(row)
    return rows


def validate_homogeneous_sample(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    validated = [validate_success_receipt(row) for row in rows]
    identities = {
        (
            row["hostname"],
            row["source"]["source_sha"],
            row["source"]["source_tree_sha"],
            row["scheduler_owner"]["active_owner_id"],
            row["scheduler_owner"]["epoch"],
        )
        for row in validated
    }
    if len(identities) > 1:
        raise ValueError("live_tick_timing_sample_mixed_identity")
    return validated


def _milliseconds(duration_ns: int) -> float:
    return round(max(0, int(duration_ns)) / 1_000_000, 3)


def _duration(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("live_tick_timing_duration_invalid") from exc
    if not math.isfinite(number) or number < 0:
        raise ValueError("live_tick_timing_duration_invalid")
    return number


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _parse_utc_field(receipt: dict[str, Any], key: str) -> datetime:
    try:
        value = datetime.fromisoformat(str(receipt.get(key) or ""))
    except ValueError as exc:
        raise ValueError(f"live_tick_timing_{key}_invalid") from exc
    if value.tzinfo is None:
        raise ValueError(f"live_tick_timing_{key}_invalid")
    return value.astimezone(timezone.utc)
