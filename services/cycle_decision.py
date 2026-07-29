"""One immutable, auditable Paper decision per market cycle."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Callable

from services.journal_store import load_json, write_json


SCHEMA_VERSION = "paper-cycle-decision-v1"
VALID_SOURCES = {"manual", "auto_ai"}
VALID_OUTCOMES = {"executed", "not_executed", "position_conflict"}


def _decision_id(payload: dict[str, Any]) -> str:
    canonical = {
        key: payload.get(key)
        for key in (
            "cycle_id",
            "source",
            "outcome",
            "reason_code",
            "evaluation_id",
            "strategy_plan_id",
        )
    }
    raw = json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"cycle-decision-{hashlib.sha256(raw.encode()).hexdigest()[:16]}"


class CycleDecisionLedger:
    def __init__(self, output_root: Path) -> None:
        self.output_root = Path(output_root)
        self.root = (
            self.output_root
            / "dualtrack"
            / "strategy_control"
            / "cycle_decisions"
        )

    def path(self, cycle_id: str) -> Path:
        return self.root / f"{cycle_id}.json"

    def read(self, cycle_id: str) -> dict[str, Any] | None:
        rows = [row for row in load_json(self.path(cycle_id)) if isinstance(row, dict)]
        return dict(rows[0]) if len(rows) == 1 else None

    def record(self, payload: dict[str, Any]) -> dict[str, Any]:
        row = dict(payload)
        cycle_id = str(row.get("cycle_id") or "")
        source = str(row.get("source") or "")
        outcome = str(row.get("outcome") or "")
        if not cycle_id:
            raise ValueError("cycle decision requires cycle_id")
        if source not in VALID_SOURCES:
            raise ValueError("cycle decision source must be manual or auto_ai")
        if outcome not in VALID_OUTCOMES:
            raise ValueError("invalid cycle decision outcome")
        if outcome != "executed":
            for field in ("reason_code", "reason", "next_action"):
                if not str(row.get(field) or "").strip():
                    raise ValueError(f"non-executed cycle decision requires {field}")
        row["schema_version"] = SCHEMA_VERSION
        row["decision_id"] = str(row.get("decision_id") or _decision_id(row))
        existing_rows = [
            candidate
            for candidate in load_json(self.path(cycle_id))
            if isinstance(candidate, dict)
        ]
        if existing_rows:
            if len(existing_rows) != 1:
                raise RuntimeError("cycle decision uniqueness violated")
            existing = dict(existing_rows[0])
            if existing.get("decision_id") != row["decision_id"]:
                raise RuntimeError("cycle decision is immutable")
            return existing
        write_json(self.path(cycle_id), [row])
        return row

    def health(self, cycle_id: str) -> dict[str, Any]:
        rows = [
            row
            for row in load_json(self.path(cycle_id))
            if isinstance(row, dict)
        ]
        if not rows:
            return {
                "status": "blocked",
                "code": "cycle_decision_missing",
                "cycle_id": cycle_id,
                "record_count": 0,
            }
        if len(rows) != 1:
            return {
                "status": "blocked",
                "code": "cycle_decision_invalid",
                "cycle_id": cycle_id,
                "record_count": len(rows),
            }
        row = rows[0]
        valid = (
            row.get("schema_version") == SCHEMA_VERSION
            and row.get("source") in VALID_SOURCES
            and row.get("outcome") in VALID_OUTCOMES
            and str(row.get("decision_id") or "").startswith("cycle-decision-")
        )
        return {
            "status": "ready" if valid else "blocked",
            "code": "cycle_decision_recorded" if valid else "cycle_decision_invalid",
            "cycle_id": cycle_id,
            "record_count": 1,
            "decision_id": row.get("decision_id"),
            "source": row.get("source"),
            "outcome": row.get("outcome"),
            "reason_code": row.get("reason_code"),
        }


def _position_direction(position: dict[str, Any]) -> str:
    side = str(position.get("side") or "").lower()
    if side in {"buy", "long"}:
        return "long"
    if side in {"sell", "short"}:
        return "short"
    return ""


def _error_code(exc: Exception) -> str:
    text = str(exc).strip()
    if not text:
        return type(exc).__name__
    return text.split(":", 1)[0].replace(" ", "_").lower()


class CycleDecisionCoordinator:
    """Execute one scheduler-owned Paper decision through normal controls."""

    def __init__(self, output_root: Path) -> None:
        self.output_root = Path(output_root)
        self.ledger = CycleDecisionLedger(output_root)

    def ensure(
        self,
        cycle_id: str,
        *,
        now: str,
        plane: Any,
        execution_snapshot: dict[str, Any],
        refresh_recommendation: Callable[[], dict[str, Any]],
        control: Callable[[str, dict[str, Any]], dict[str, Any]],
    ) -> dict[str, Any]:
        existing = self.ledger.read(cycle_id)
        if existing:
            return {"status": "existing", "decision": existing}

        runtime = plane.runtime_state(cycle_id)
        current = plane.active_plan(cycle_id)
        running = (
            runtime.get("desired_state") == "running"
            and runtime.get("actual_state") == "running"
        )
        if current and running:
            manual = self._is_manual(current)
            decision = self.ledger.record(
                {
                    "cycle_id": cycle_id,
                    "recorded_at": now,
                    "source": "manual" if manual else "auto_ai",
                    "outcome": "executed",
                    "reason_code": "",
                    "reason": "",
                    "next_action": "",
                    "strategy_plan_id": current.get("strategy_plan_id"),
                    "runtime_actual_state": runtime.get("actual_state"),
                }
            )
            return {"status": "recorded", "decision": decision}
        if current and self._is_manual(current):
            decision = self.ledger.record(
                {
                    "cycle_id": cycle_id,
                    "recorded_at": now,
                    "source": "manual",
                    "outcome": "not_executed",
                    "reason_code": "manual_start_pending",
                    "reason": "Manual plan exists but its operator start is not complete.",
                    "next_action": "Complete the normal operator preview and confirmation flow.",
                    "strategy_plan_id": current.get("strategy_plan_id"),
                    "runtime_actual_state": runtime.get("actual_state"),
                }
            )
            return {"status": "recorded", "decision": decision}

        try:
            evaluation = refresh_recommendation()
            recommendation = dict(evaluation.get("recommendation") or {})
            proposal = dict(evaluation.get("proposal") or {})
            preview = dict(evaluation.get("preview") or {})
            receipt = dict(recommendation.get("evaluation_receipt") or {})
            evaluation_id = str(receipt.get("evaluation_id") or "")
            direction = str(recommendation.get("direction") or proposal.get("direction") or "")
            strategy_type = str(recommendation.get("strategy_type") or "grid").lower()

            open_positions = [
                dict(row)
                for row in execution_snapshot.get("positions") or []
                if str(row.get("status") or "").lower() == "open"
            ]
            position_directions = {
                value
                for value in (_position_direction(row) for row in open_positions)
                if value
            }
            if open_positions:
                conflict = bool(
                    direction in {"long", "short"}
                    and position_directions
                    and direction not in position_directions
                )
                suspended_entry_ids: list[str] = []
                if conflict:
                    entry_ids = sorted(
                        str(row.get("order_id") or "")
                        for row in execution_snapshot.get("orders") or []
                        if str(row.get("state") or "").lower() == "accepted"
                        and str(row.get("event") or "entry").lower() == "entry"
                        and str(row.get("order_id") or "")
                    )
                    if entry_ids:
                        suspended = control(
                            "suspend_entries",
                            {"order_ids": entry_ids},
                        )
                        suspended_entry_ids = [
                            str(value)
                            for value in suspended.get("cancelled_entry_order_ids") or []
                        ]
                        if set(suspended_entry_ids) != set(entry_ids):
                            raise RuntimeError("cycle_conflict_entry_suspend_incomplete")
                decision = self.ledger.record(
                    {
                        "cycle_id": cycle_id,
                        "recorded_at": now,
                        "source": "auto_ai",
                        "outcome": "position_conflict" if conflict else "not_executed",
                        "reason_code": (
                            "existing_position_direction_conflict"
                            if conflict
                            else "existing_position_managed_without_new_entries"
                        ),
                        "reason": (
                            "The fresh recommendation opposes an existing Paper position."
                            if conflict
                            else "An existing Paper position remains under its current exit rules."
                        ),
                        "next_action": (
                            "Let the existing position exit naturally; any reversal requires an explicit later decision."
                        ),
                        "evaluation_id": evaluation_id,
                        "recommended_direction": direction,
                        "strategy_type": strategy_type,
                        "open_position_ids": [
                            str(row.get("position_id") or row.get("trade_id") or "")
                            for row in open_positions
                        ],
                        "open_position_directions": sorted(position_directions),
                        "suspended_entry_order_ids": suspended_entry_ids,
                        "orders_created": 0,
                    }
                )
                return {"status": "recorded", "decision": decision}

            if strategy_type != "dca":
                plane.lock_production_plan(
                    cycle_id,
                    selected_proposal_id=str(proposal.get("proposal_id") or ""),
                    now=now,
                )
            request = {
                "direction": direction,
                "style": recommendation.get("style") or proposal.get("style"),
                "strategy_type": strategy_type,
            }
            prepared = control("prepare_start", request)
            prepared_preview = dict(prepared.get("preview") or preview)
            confirmation = dict(prepared_preview.get("manual_confirmation") or {})
            if confirmation.get("required") is True:
                decision = self.ledger.record(
                    {
                        "cycle_id": cycle_id,
                        "recorded_at": now,
                        "source": "auto_ai",
                        "outcome": "not_executed",
                        "reason_code": "risk_confirmation_required",
                        "reason": "The deterministic preview requires explicit human risk confirmation.",
                        "next_action": "Review and confirm the frozen Paper preview in the Dashboard.",
                        "evaluation_id": evaluation_id,
                        "strategy_plan_id": (
                            (plane.active_plan(cycle_id) or {}).get("strategy_plan_id")
                        ),
                        "strategy_type": strategy_type,
                        "recommended_direction": direction,
                        "preview_id": prepared_preview.get("preview_id"),
                        "required_acknowledgement_codes": [
                            str(row.get("code") or "")
                            for row in confirmation.get("required_acknowledgements") or []
                        ],
                        "orders_created": 0,
                    }
                )
                return {"status": "recorded", "decision": decision}

            started = control(
                "start",
                {
                    **request,
                    "prepared_start_id": prepared.get("prepared_start_id"),
                    "expected_preview_id": prepared_preview.get("preview_id"),
                },
            )
            created = int(started.get("created_orders") or 0)
            accepted = int(started.get("accepted_orders") or 0)
            if created != accepted:
                raise RuntimeError(
                    f"paper_start_incomplete:{created}/{accepted}"
                )
            plan = dict(started.get("plan") or {})
            decision = self.ledger.record(
                {
                    "cycle_id": cycle_id,
                    "recorded_at": now,
                    "source": "auto_ai",
                    "outcome": "executed",
                    "reason_code": "",
                    "reason": "",
                    "next_action": "",
                    "evaluation_id": evaluation_id,
                    "strategy_plan_id": plan.get("strategy_plan_id"),
                    "strategy_type": strategy_type,
                    "recommended_direction": direction,
                    "preview_id": prepared_preview.get("preview_id"),
                    "orders_created": created,
                    "orders_accepted": accepted,
                }
            )
            return {"status": "recorded", "decision": decision}
        except Exception as exc:
            decision = self.ledger.record(
                {
                    "cycle_id": cycle_id,
                    "recorded_at": now,
                    "source": "auto_ai",
                    "outcome": "not_executed",
                    "reason_code": _error_code(exc),
                    "reason": str(exc) or type(exc).__name__,
                    "next_action": "Resolve the recorded blocker and wait for the next cycle; do not replay this cycle decision.",
                    "orders_created": 0,
                }
            )
            return {"status": "recorded", "decision": decision}

    @staticmethod
    def _is_manual(plan: dict[str, Any]) -> bool:
        sources = {
            str(value)
            for value in (plan.get("field_sources") or {}).values()
            if value
        }
        return bool(sources) and sources == {"human"}
