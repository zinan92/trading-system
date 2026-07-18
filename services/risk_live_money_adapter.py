"""Canonical adapter around the mature live-money guardrail policy."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from schemas.risk import RiskRequest, build_risk_decision, build_risk_request
from services.risk_decision_store import FileRiskDecisionStore
from services.risk_policy_core import (
    blocker,
    digest,
    safe_action_identity_blockers,
)
from services.risk_port import RiskDecisionStorePort


LIVE_MONEY_RISK_EVALUATOR_VERSION = "live-money-risk-bridge-v1"


class LiveMoneyRiskDecisionAdapter:
    """Translate existing venue guardrails without loosening their answer."""

    name = "live_money_risk_bridge"

    def __init__(
        self,
        output_root: Path,
        *,
        broker_config: Mapping[str, Any] | None = None,
        legacy_guardrails=None,
        store: RiskDecisionStorePort | None = None,
    ) -> None:
        self.output_root = Path(output_root)
        self.broker_config = dict(broker_config or {})
        if legacy_guardrails is None:
            from services.live_money_guardrails import LiveMoneyGuardrails

            legacy_guardrails = LiveMoneyGuardrails(
                self.output_root,
                broker_config=self.broker_config,
            )
        self.legacy_guardrails = legacy_guardrails
        self.store = store or FileRiskDecisionStore(self.output_root)

    def evaluate_order(
        self,
        run_date: str,
        *,
        ticket: Mapping[str, Any],
        symbol: str,
        side: str,
        requested_price: float,
        quantity: float,
        source: str,
        reconciliation: Mapping[str, Any] | None = None,
        action_class: str = "increase_exposure",
        checked_at: str | None = None,
    ) -> dict[str, Any]:
        resolved_action = str(action_class or "").strip().lower()
        timestamp = (
            checked_at
            or datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        )
        if resolved_action != "increase_exposure":
            return self._safe_action(
                run_date,
                ticket=ticket,
                symbol=symbol,
                source=source,
                action_class=resolved_action,
                checked_at=timestamp,
            )

        legacy = self.legacy_guardrails.evaluate_order(
            run_date,
            ticket=dict(ticket),
            symbol=symbol,
            side=side,
            requested_price=requested_price,
            quantity=quantity,
            source=source,
            reconciliation=dict(reconciliation or {}),
        )
        return self.record_legacy_result(
            run_date,
            ticket=ticket,
            symbol=symbol,
            side=side,
            requested_price=requested_price,
            quantity=quantity,
            source=source,
            reconciliation=reconciliation,
            legacy=legacy,
            checked_at=timestamp,
        )

    def record_legacy_result(
        self,
        run_date: str,
        *,
        ticket: Mapping[str, Any],
        symbol: str,
        side: str,
        requested_price: float,
        quantity: float,
        source: str,
        reconciliation: Mapping[str, Any] | None,
        legacy: Mapping[str, Any] | None,
        checked_at: str | None = None,
    ) -> dict[str, Any]:
        result = dict(legacy) if isinstance(legacy, Mapping) else {}
        timestamp = str(
            result.get("checked_at")
            or checked_at
            or datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        )
        request = _live_money_risk_request(
            checked_at=timestamp,
            run_date=run_date,
            ticket=ticket,
            symbol=symbol,
            side=side,
            requested_price=requested_price,
            quantity=quantity,
            source=source,
            reconciliation=reconciliation or {},
            legacy=result,
            action_class="increase_exposure",
        )
        blockers = _canonical_live_blockers(result)
        decision = build_risk_decision(
            request,
            blockers=blockers,
            metrics={
                "legacy_status": str(result.get("status") or ""),
                "legacy_allows_new_order": result.get("allows_new_order") is True,
                "candidate_notional": (result.get("candidate") or {}).get(
                    "notional"
                ),
                "projected_total_notional": (result.get("exposure") or {}).get(
                    "projected_total_notional"
                ),
                "daily_loss_pct": (result.get("daily_loss") or {}).get("loss_pct"),
            },
            limits=dict(result.get("limits") or {}),
        )
        persisted = self.store.persist(decision)
        return {**result, "risk_decision": persisted}

    def _safe_action(
        self,
        run_date: str,
        *,
        ticket: Mapping[str, Any],
        symbol: str,
        source: str,
        action_class: str,
        checked_at: str,
    ) -> dict[str, Any]:
        candidate = {
            "kind": "venue_safe_action",
            "run_date": str(run_date),
            "cycle_id": str(ticket.get("cycle_id") or run_date),
            "event": str(ticket.get("event") or "").lower(),
            "trade_id": str(ticket.get("trade_id") or ""),
            "position_id": str(ticket.get("position_id") or ""),
            "symbol": str(symbol),
            "source": str(source),
        }
        request = build_risk_request(
            checked_at=checked_at,
            scope="venue_safe_action",
            action_class=action_class,
            candidate=candidate,
            account={"status": "not_required"},
            market={"status": "not_required"},
            execution={"status": "not_required"},
            policy={"policy_id": "safe-action-identity-v1"},
            evaluator=live_money_risk_evaluator(),
        )
        expected = "cancel" if action_class == "cancel" else "reduce_only"
        blockers = safe_action_identity_blockers(
            request.to_dict(),
            expected=expected,
        )
        decision = build_risk_decision(request, blockers=blockers)
        persisted = self.store.persist(decision)
        return {
            "run_date": run_date,
            "status": (
                "SAFE_ACTION_ALLOWED"
                if decision.to_dict()["outcome"] == "allow"
                else "SAFE_ACTION_BLOCKED"
            ),
            "allows_new_order": False,
            "blockers": decision.to_dict()["blockers"],
            "primary_blocker": decision.to_dict()["primary_blocker"],
            "risk_decision": persisted,
        }


def live_money_risk_evaluator() -> dict[str, Any]:
    root = Path(__file__).resolve().parents[1]
    paths = (
        root / "services" / "risk_live_money_adapter.py",
        root / "services" / "risk_policy_core.py",
        root / "schemas" / "risk.py",
        root / "services" / "live_money_guardrails.py",
    )
    hashes = {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in paths
    }
    return {
        "name": "live_money_risk_bridge",
        "version": LIVE_MONEY_RISK_EVALUATOR_VERSION,
        "source_hashes": hashes,
        "code_sha256": digest(hashes),
    }


def _live_money_risk_request(
    *,
    checked_at: str,
    run_date: str,
    ticket: Mapping[str, Any],
    symbol: str,
    side: str,
    requested_price: float,
    quantity: float,
    source: str,
    reconciliation: Mapping[str, Any],
    legacy: Mapping[str, Any],
    action_class: str,
) -> RiskRequest:
    candidate = dict(legacy.get("candidate") or {})
    candidate.update(
        {
            "kind": "venue_entry",
            "run_date": str(run_date),
            "ticket_id": str(
                ticket.get("ticket_id") or candidate.get("ticket_id") or ""
            ),
            "symbol": str(symbol),
            "side": str(side),
            "requested_price": requested_price,
            "quantity": quantity,
            "source": str(source),
        }
    )
    policy = {
        "schema_version": "live-money-risk-policy-v1",
        "limits": dict(legacy.get("limits") or {}),
        "legacy_policy_authority": "LiveMoneyGuardrails",
        "unknown_facts_block_new_exposure": True,
    }
    policy["policy_id"] = f"live-money-risk-policy-{digest(policy)}"
    return build_risk_request(
        checked_at=checked_at,
        scope="venue_entry",
        action_class=action_class,
        candidate=candidate,
        account={
            "daily_loss": dict(legacy.get("daily_loss") or {}),
            "halt": dict(legacy.get("halt") or {}),
        },
        market={
            "symbol": str(symbol),
            "price": requested_price,
            "source": str(source),
        },
        execution={
            "exposure": dict(legacy.get("exposure") or {}),
            "daily_entry_orders": dict(legacy.get("daily_entry_orders") or {}),
            "reconciliation": dict(reconciliation),
        },
        policy=policy,
        evaluator=live_money_risk_evaluator(),
    )


def _canonical_live_blockers(
    legacy: Mapping[str, Any],
) -> list[dict[str, Any]]:
    rows = legacy.get("blockers") if isinstance(legacy.get("blockers"), list) else []
    blockers: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            blockers.append(
                blocker(
                    "legacy_guardrail_blocker_invalid",
                    "LiveMoneyGuardrails",
                    "legacy live-money blocker is malformed",
                    {"index": index},
                )
            )
            continue
        blockers.append(
            {
                "code": str(row.get("code") or "legacy_guardrail_blocked"),
                "source": str(row.get("source") or "LiveMoneyGuardrails"),
                "message": str(
                    row.get("message")
                    or row.get("status")
                    or "legacy live-money guardrail blocked entry"
                ),
                "evidence": dict(row.get("evidence") or {}),
                **(
                    {"legacy_status": str(row.get("status"))}
                    if row.get("status")
                    else {}
                ),
            }
        )
    if legacy.get("allows_new_order") is not True and not blockers:
        blockers.append(
            blocker(
                "legacy_guardrail_denied_without_blocker",
                "LiveMoneyGuardrails",
                "legacy live-money guardrail did not grant entry permission",
                {
                    "status": legacy.get("status"),
                    "allows_new_order": legacy.get("allows_new_order"),
                },
            )
        )
    return blockers
