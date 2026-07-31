"""Sealed, independently revalidated Paper Supervisor running evidence.

The writer persists primitives rather than a trusted ``running=True`` claim.
Both the append boundary and every read-model projection call
``validate_running_evidence`` and recompute the same conjunction.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from services.paper_supervisor_heartbeat import (
    MAX_HEARTBEAT_AGE_SECONDS,
)


RUNNING_EVIDENCE_SCHEMA_VERSION = "paper-supervisor-running-evidence-v1"
MAX_EVIDENCE_PERSIST_LAG_SECONDS = 120
_AUTHORITY_STATUSES = frozenset({"available", "unknown"})
_REPRESENTATIVE_KINDS = frozenset({"accepted_order", "open_position"})


class RunningEvidenceError(ValueError):
    """Stable invalid-evidence signal used by the fail-closed store."""


def draft_running_evidence(
    *,
    cycle_id: str,
    evidence_at: str,
    heartbeat_recorded_at: str | None,
    heartbeat_digest: str,
    authority_status: str,
    plan_identity: Mapping[str, Any] | None,
    runtime: Mapping[str, Any] | None,
    expected_slots: Sequence[Mapping[str, Any]] = (),
    current_slots: Sequence[Mapping[str, Any]] = (),
    reconciliation: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build bounded primitives; the store alone may add ``persisted_at``."""

    if authority_status not in _AUTHORITY_STATUSES:
        raise RunningEvidenceError("running_evidence_authority_invalid")
    payload = {
        "schema_version": RUNNING_EVIDENCE_SCHEMA_VERSION,
        "cycle_id": _required_text(cycle_id),
        "evidence_at": _utc_text(evidence_at),
        "heartbeat": {
            "recorded_at": (
                _utc_text(heartbeat_recorded_at)
                if heartbeat_recorded_at
                else None
            ),
            "digest": _digest_text(heartbeat_digest),
        },
        "authority_status": authority_status,
        "plan_identity": _copy_object(plan_identity),
        "runtime": _copy_object(runtime),
        "expected_slots": _copy_rows(expected_slots),
        "current_slots": _copy_rows(current_slots),
        "reconciliation": _copy_object(reconciliation),
    }
    _validate_draft_shape(payload)
    return payload


def finalize_running_evidence(
    value: Mapping[str, Any],
    *,
    persisted_at: str,
) -> dict[str, Any]:
    """Add the store timestamp and seal the recomputed proof."""

    payload = _copy_object(value)
    forbidden = {
        "persisted_at",
        "running_proven",
        "proof_status",
        "expected_slot_digest",
    }
    if forbidden.intersection(payload):
        raise RunningEvidenceError("running_evidence_store_fields_forbidden")
    _validate_draft_shape(payload)
    payload["persisted_at"] = _utc_text(persisted_at)
    payload["expected_slot_digest"] = _digest(payload["expected_slots"])
    proven = _recompute(payload)
    payload["running_proven"] = proven
    payload["proof_status"] = (
        "proven"
        if proven
        else (
            "not_proven"
            if payload["authority_status"] == "available"
            else "unknown"
        )
    )
    validate_running_evidence(payload)
    return payload


def validate_running_evidence(
    value: Mapping[str, Any],
) -> dict[str, Any]:
    """Return a JSON copy only when every primitive and derived field agrees."""

    payload = _copy_object(value)
    _validate_draft_shape(payload)
    if (
        set(payload)
        != {
            "schema_version",
            "cycle_id",
            "evidence_at",
            "heartbeat",
            "authority_status",
            "plan_identity",
            "runtime",
            "expected_slots",
            "current_slots",
            "reconciliation",
            "persisted_at",
            "expected_slot_digest",
            "running_proven",
            "proof_status",
        }
        or payload.get("expected_slot_digest")
        != _digest(payload["expected_slots"])
        or not isinstance(payload.get("running_proven"), bool)
        or payload.get("proof_status")
        not in {"proven", "not_proven", "unknown"}
    ):
        raise RunningEvidenceError("running_evidence_invalid")
    _utc_text(payload.get("persisted_at"))
    proven = _recompute(payload)
    expected_status = (
        "proven"
        if proven
        else (
            "not_proven"
            if payload["authority_status"] == "available"
            else "unknown"
        )
    )
    if (
        payload["running_proven"] is not proven
        or payload["proof_status"] != expected_status
    ):
        raise RunningEvidenceError("running_evidence_invalid")
    return payload


def same_running_identity(
    left: Mapping[str, Any],
    right: Mapping[str, Any],
) -> bool:
    """Require exact cycle, plan, and canonical slot content at both endpoints."""

    try:
        one = validate_running_evidence(left)
        two = validate_running_evidence(right)
    except RunningEvidenceError:
        return False
    return (
        one["cycle_id"] == two["cycle_id"]
        and one["plan_identity"] == two["plan_identity"]
        and one["expected_slots"] == two["expected_slots"]
        and one["expected_slot_digest"]
        == two["expected_slot_digest"]
    )


def _validate_draft_shape(payload: Mapping[str, Any]) -> None:
    allowed = {
        "schema_version",
        "cycle_id",
        "evidence_at",
        "heartbeat",
        "authority_status",
        "plan_identity",
        "runtime",
        "expected_slots",
        "current_slots",
        "reconciliation",
        "persisted_at",
        "expected_slot_digest",
        "running_proven",
        "proof_status",
    }
    if (
        not isinstance(payload, Mapping)
        or set(payload) - allowed
        or payload.get("schema_version")
        != RUNNING_EVIDENCE_SCHEMA_VERSION
        or payload.get("authority_status") not in _AUTHORITY_STATUSES
    ):
        raise RunningEvidenceError("running_evidence_invalid")
    _required_text(payload.get("cycle_id"))
    _utc_text(payload.get("evidence_at"))
    heartbeat = _copy_object(payload.get("heartbeat"))
    if set(heartbeat) != {"recorded_at", "digest"}:
        raise RunningEvidenceError("running_evidence_invalid")
    if heartbeat.get("recorded_at") is not None:
        _utc_text(heartbeat["recorded_at"])
    _digest_text(heartbeat.get("digest"))
    plan = _copy_object(payload.get("plan_identity"))
    runtime = _copy_object(payload.get("runtime"))
    reconciliation = _copy_object(payload.get("reconciliation"))
    expected = _copy_rows(payload.get("expected_slots") or [])
    current = _copy_rows(payload.get("current_slots") or [])
    if payload["authority_status"] == "available":
        _validate_plan(plan)
        _validate_runtime(runtime)
        if set(reconciliation) != {"execution", "accounting"}:
            raise RunningEvidenceError("running_evidence_invalid")
    elif plan or runtime or expected or current or reconciliation:
        raise RunningEvidenceError("running_evidence_invalid")
    _validate_expected_slots(expected)
    _validate_current_slots(current)


def _recompute(payload: Mapping[str, Any]) -> bool:
    if payload.get("authority_status") != "available":
        return False
    try:
        evidence_at = _utc(payload["evidence_at"])
        persisted_at = _utc(payload["persisted_at"])
        heartbeat_at = _utc(
            _copy_object(payload["heartbeat"])["recorded_at"]
        )
        heartbeat_age = (evidence_at - heartbeat_at).total_seconds()
        persist_lag = (persisted_at - evidence_at).total_seconds()
        plan = _copy_object(payload["plan_identity"])
        runtime = _copy_object(payload["runtime"])
        reconciliation = _copy_object(payload["reconciliation"])
        expected = _copy_rows(payload["expected_slots"])
        current = _copy_rows(payload["current_slots"])
        _validate_plan(plan)
        _validate_runtime(runtime)
        _validate_expected_slots(expected)
        _validate_current_slots(current)
        expected_ids = [row["slot_id"] for row in expected]
        current_ids = [row["slot_id"] for row in current]
        accepted_ids = [
            row["representative_id"]
            for row in current
            if row["representative_kind"] == "accepted_order"
        ]
        position_ids = [
            row["representative_id"]
            for row in current
            if row["representative_kind"] == "open_position"
        ]
        plan_exact = (
            runtime["cycle_id"] == payload["cycle_id"]
            and runtime["strategy_plan_id"]
            == plan["strategy_plan_id"]
            and runtime["strategy_plan_version"]
            == plan["strategy_plan_version"]
            and runtime["desired_state"] == "running"
            and runtime["actual_state"] == "running"
            and runtime["accepted_order_count"] == len(accepted_ids)
        )
        slots_exact = (
            bool(expected_ids)
            and len(expected_ids) == len(current_ids)
            and len(expected_ids) == len(set(expected_ids))
            and len(current_ids) == len(set(current_ids))
            and expected_ids == current_ids
            and not set(accepted_ids).intersection(position_ids)
            and all(
                _valid_current_slot(row, expected, plan)
                for row in current
            )
        )
        return (
            0 <= heartbeat_age <= MAX_HEARTBEAT_AGE_SECONDS
            and 0
            <= persist_lag
            <= MAX_EVIDENCE_PERSIST_LAG_SECONDS
            and plan_exact
            and slots_exact
            and reconciliation.get("execution") == "ok"
            and reconciliation.get("accounting") == "pass"
        )
    except (
        KeyError,
        TypeError,
        ValueError,
        RunningEvidenceError,
    ):
        return False


def _validate_plan(value: Mapping[str, Any]) -> None:
    if set(value) != {
        "strategy_plan_id",
        "strategy_plan_version",
        "strategy_type",
        "direction",
    }:
        raise RunningEvidenceError("running_evidence_invalid")
    _required_text(value.get("strategy_plan_id"))
    if (
        not isinstance(value.get("strategy_plan_version"), int)
        or value["strategy_plan_version"] < 1
        or value.get("strategy_type") not in {"grid", "dca"}
        or value.get("direction") not in {"neutral", "long", "short"}
    ):
        raise RunningEvidenceError("running_evidence_invalid")


def _validate_runtime(value: Mapping[str, Any]) -> None:
    if set(value) != {
        "cycle_id",
        "strategy_plan_id",
        "strategy_plan_version",
        "desired_state",
        "actual_state",
        "accepted_order_count",
    }:
        raise RunningEvidenceError("running_evidence_invalid")
    _required_text(value.get("cycle_id"))
    _required_text(value.get("strategy_plan_id"))
    if (
        not isinstance(value.get("strategy_plan_version"), int)
        or value["strategy_plan_version"] < 0
        or not isinstance(value.get("accepted_order_count"), int)
        or value["accepted_order_count"] < 0
    ):
        raise RunningEvidenceError("running_evidence_invalid")
    for key in ("desired_state", "actual_state"):
        if str(value.get(key) or "") not in {
            "running",
            "stopped",
            "starting",
            "stopping",
            "error",
        }:
            raise RunningEvidenceError("running_evidence_invalid")


def _validate_expected_slots(rows: Sequence[Mapping[str, Any]]) -> None:
    previous = ""
    for raw in rows:
        row = _copy_object(raw)
        if set(row) != {
            "slot_id",
            "initial_command_id",
            "expected_fingerprint",
            "authorized_commands",
        }:
            raise RunningEvidenceError("running_evidence_invalid")
        slot_id = _required_text(row.get("slot_id"))
        _required_text(row.get("initial_command_id"))
        _digest_text(row.get("expected_fingerprint"))
        authorized = _copy_rows(row.get("authorized_commands") or [])
        if not authorized:
            raise RunningEvidenceError("running_evidence_invalid")
        for index, command in enumerate(authorized, start=1):
            _validate_command(command)
            if command["generation"] != index:
                raise RunningEvidenceError("running_evidence_invalid")
        if (
            authorized[0]["command_id"]
            != row["initial_command_id"]
            or authorized[0]["fingerprint"]
            != row["expected_fingerprint"]
        ):
            raise RunningEvidenceError("running_evidence_invalid")
        if slot_id <= previous:
            raise RunningEvidenceError("running_evidence_invalid")
        previous = slot_id


def _validate_current_slots(rows: Sequence[Mapping[str, Any]]) -> None:
    previous = ""
    representative_ids: set[str] = set()
    for raw in rows:
        row = _copy_object(raw)
        if set(row) != {
            "slot_id",
            "representative_kind",
            "representative_id",
            "command",
            "ancestry",
            "position",
        }:
            raise RunningEvidenceError("running_evidence_invalid")
        slot_id = _required_text(row.get("slot_id"))
        kind = str(row.get("representative_kind") or "")
        representative = _required_text(row.get("representative_id"))
        if (
            kind not in _REPRESENTATIVE_KINDS
            or representative in representative_ids
            or slot_id < previous
        ):
            raise RunningEvidenceError("running_evidence_invalid")
        representative_ids.add(representative)
        previous = slot_id
        _validate_command(_copy_object(row.get("command")))
        ancestry = _copy_rows(row.get("ancestry") or [])
        if not ancestry:
            raise RunningEvidenceError("running_evidence_invalid")
        for index, item in enumerate(ancestry, start=1):
            if set(item) != {
                "generation",
                "command_id",
                "rearm_of_order_id",
                "lifecycle_status",
                "reorder_order_id",
            }:
                raise RunningEvidenceError("running_evidence_invalid")
            if item.get("generation") != index:
                raise RunningEvidenceError("running_evidence_invalid")
            _required_text(item.get("command_id"))
            if index == 1:
                if (
                    item.get("rearm_of_order_id") is not None
                    or item.get("lifecycle_status")
                    not in {"initial", "completed_rearmed"}
                    or (
                        item.get("lifecycle_status") == "initial"
                        and item.get("reorder_order_id") is not None
                    )
                ):
                    raise RunningEvidenceError("running_evidence_invalid")
            else:
                previous_item = ancestry[index - 2]
                if (
                    item.get("rearm_of_order_id")
                    != previous_item["command_id"]
                    or previous_item.get("lifecycle_status")
                    != "completed_rearmed"
                    or previous_item.get("reorder_order_id")
                    != item["command_id"]
                ):
                    raise RunningEvidenceError("running_evidence_invalid")
        position = row.get("position")
        if kind == "accepted_order":
            if position is not None:
                raise RunningEvidenceError("running_evidence_invalid")
        else:
            _validate_position(_copy_object(position))


def _validate_command(value: Mapping[str, Any]) -> None:
    if set(value) != {
        "command_id",
        "fingerprint",
        "side",
        "quantity",
        "price",
        "generation",
        "economics",
    }:
        raise RunningEvidenceError("running_evidence_invalid")
    _required_text(value.get("command_id"))
    _digest_text(value.get("fingerprint"))
    _positive_decimal(value.get("quantity"))
    _positive_decimal(value.get("price"))
    _validate_economics(_copy_object(value.get("economics")))
    if (
        value.get("side") not in {"buy", "sell"}
        or not isinstance(value.get("generation"), int)
        or value["generation"] < 1
    ):
        raise RunningEvidenceError("running_evidence_invalid")


def _validate_economics(value: Mapping[str, Any]) -> None:
    if set(value) != {
        "event",
        "symbol",
        "order_type",
        "notional",
        "sl",
        "tp",
        "strategy_plan_id",
        "strategy_plan_version",
    }:
        raise RunningEvidenceError("running_evidence_invalid")
    if (
        value.get("event") != "entry"
        or not str(value.get("symbol") or "").strip()
        or not str(value.get("order_type") or "").strip()
        or not str(value.get("strategy_plan_id") or "").strip()
        or not isinstance(value.get("strategy_plan_version"), int)
        or value["strategy_plan_version"] < 1
    ):
        raise RunningEvidenceError("running_evidence_invalid")
    _positive_decimal(value.get("notional"))
    _positive_decimal(value.get("sl"))
    if value.get("tp") is not None:
        _positive_decimal(value.get("tp"))


def _validate_position(value: Mapping[str, Any]) -> None:
    if set(value) != {
        "trade_id",
        "strategy_plan_id",
        "strategy_plan_version",
        "side",
        "order_quantity",
    }:
        raise RunningEvidenceError("running_evidence_invalid")
    _required_text(value.get("trade_id"))
    _required_text(value.get("strategy_plan_id"))
    _positive_decimal(value.get("order_quantity"))
    if (
        not isinstance(value.get("strategy_plan_version"), int)
        or value["strategy_plan_version"] < 1
        or value.get("side") not in {"long", "short"}
    ):
        raise RunningEvidenceError("running_evidence_invalid")


def _valid_current_slot(
    row: Mapping[str, Any],
    expected: Sequence[Mapping[str, Any]],
    plan: Mapping[str, Any],
) -> bool:
    expected_row = next(
        (
            item
            for item in expected
            if item["slot_id"] == row["slot_id"]
        ),
        None,
    )
    if expected_row is None:
        return False
    command = _copy_object(row["command"])
    ancestry = _copy_rows(row["ancestry"])
    authorized = _copy_rows(expected_row["authorized_commands"])
    if (
        ancestry[0]["command_id"]
        != expected_row["initial_command_id"]
        or ancestry[-1]["command_id"] != command["command_id"]
        or command["generation"] != len(ancestry)
        or len(ancestry) != len(authorized)
        or command != authorized[-1]
        or any(
            ancestry[index]["command_id"]
            != authorized[index]["command_id"]
            or ancestry[index]["generation"]
            != authorized[index]["generation"]
            for index in range(len(authorized))
        )
        or any(
            item["side"] != authorized[0]["side"]
            or item["quantity"] != authorized[0]["quantity"]
            or item["price"] != authorized[0]["price"]
            or item["economics"] != authorized[0]["economics"]
            for item in authorized[1:]
        )
    ):
        return False
    if (
        authorized[0]["fingerprint"]
        != expected_row["expected_fingerprint"]
    ):
        return False
    if row["representative_kind"] == "open_position":
        position = _copy_object(row["position"])
        expected_side = "long" if command["side"] == "buy" else "short"
        return (
            position["trade_id"] == command["command_id"]
            and position["strategy_plan_id"]
            == plan["strategy_plan_id"]
            and position["strategy_plan_version"]
            == plan["strategy_plan_version"]
            and position["side"] == expected_side
            and position["order_quantity"] == command["quantity"]
        )
    return row["representative_id"] == command["command_id"]


def _copy_object(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise RunningEvidenceError("running_evidence_invalid")
    try:
        copied = json.loads(
            json.dumps(
                dict(value),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
    except (TypeError, ValueError) as exc:
        raise RunningEvidenceError("running_evidence_invalid") from exc
    if not isinstance(copied, dict):
        raise RunningEvidenceError("running_evidence_invalid")
    return copied


def _copy_rows(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(
        value,
        (str, bytes, bytearray),
    ):
        raise RunningEvidenceError("running_evidence_invalid")
    return [_copy_object(row) for row in value]


def _required_text(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        raise RunningEvidenceError("running_evidence_invalid")
    return text


def _digest_text(value: Any) -> str:
    text = str(value or "")
    if len(text) != 64:
        raise RunningEvidenceError("running_evidence_invalid")
    try:
        int(text, 16)
    except ValueError as exc:
        raise RunningEvidenceError("running_evidence_invalid") from exc
    return text


def _positive_decimal(value: Any) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise RunningEvidenceError("running_evidence_invalid") from exc
    if not parsed.is_finite() or parsed <= 0:
        raise RunningEvidenceError("running_evidence_invalid")
    return parsed


def _utc_text(value: Any) -> str:
    return _utc(value).isoformat()


def _utc(value: Any) -> datetime:
    text = str(value or "").strip().replace("Z", "+00:00")
    if not text:
        raise RunningEvidenceError("running_evidence_invalid")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise RunningEvidenceError("running_evidence_invalid") from exc
    if parsed.tzinfo is None:
        raise RunningEvidenceError("running_evidence_invalid")
    return parsed.astimezone(timezone.utc)


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
