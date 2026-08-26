"""Bounded canonical BTC Testnet transport proof.

This coordinator composes the existing attended canary and typed fact
contracts.  It adds one explicit position-protection gate between an observed
entry fill and the ordinary reduce-only close.  The injected Broker, facts,
and protection objects are public canonical ports; no venue-native payload is
accepted here.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
from typing import Any, Mapping

from services.journal_store import load_json, write_json
from services.park_confirmation import ParkConfirmationLedger
from services.standard_broker_testnet_canary import (
    TestnetCanary,
    TestnetCanaryOrderPort,
    TestnetCanaryPlan,
)
from services.standard_broker_testnet_canary_facts import TestnetCanaryFactPort


TRANSPORT_CANARY_SCHEMA = "testnet-transport-canary-v1"
TRANSPORT_CANARY_ROOT = "testnet_automation"
TRANSPORT_CANARY_FILE = "canary_current.json"
_BTC_INSTRUMENT = "BTC-USD-PERP"
_SAFE_PROTECTION_FIELDS = frozenset(
    {
        "status",
        "covered",
        "covered_quantity",
        "reduce_only",
        "instrument_id",
        "parent_order_id",
        "protection_id",
        "broker_id",
        "environment",
        "account_fingerprint",
        "runtime_id",
        "release_sha",
        "capability_revision",
        "cursor",
        "reason",
    }
)


class TestnetTransportCanaryError(RuntimeError):
    """Durable Testnet canary blocker; callers must stop and inspect it."""

    __test__ = False

    def __init__(self, code: str, evidence: Mapping[str, Any] | None = None) -> None:
        self.code = str(code)
        self.evidence = dict(evidence or {})
        super().__init__(self.code)


class TestnetPositionProtectionPort:
    """Protocol marker documented as a runtime-checkable duck-type contract."""

    __test__ = False


def _timestamp(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError as exc:
            raise TestnetTransportCanaryError("timestamp_invalid") from exc
    if parsed.tzinfo is None:
        raise TestnetTransportCanaryError("timestamp_timezone_missing")
    return parsed.astimezone(timezone.utc)


def _decimal(value: Any, field: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise TestnetTransportCanaryError(f"{field}_invalid") from exc
    if not result.is_finite() or result < 0:
        raise TestnetTransportCanaryError(f"{field}_invalid")
    return result


def _safe_mapping(value: Mapping[str, Any]) -> dict[str, Any]:
    return {
        str(key): value[key]
        for key in sorted(_SAFE_PROTECTION_FIELDS.intersection(value))
        if isinstance(value[key], (str, int, float, bool)) or value[key] is None
    }


class TestnetTransportCanary:
    """Run one attended entry/protection/flat proof for BTC Testnet."""

    __test__ = False

    def __init__(
        self,
        output_root: Path,
        broker: TestnetCanaryOrderPort,
        facts: TestnetCanaryFactPort,
        protection: object,
        *,
        park_user_id: str = "park",
        confirmation_ledger: ParkConfirmationLedger | None = None,
        approved_market_sources: set[str] | frozenset[str] | None = None,
    ) -> None:
        if not isinstance(broker, TestnetCanaryOrderPort):
            raise TypeError("transport canary broker must implement the public order port")
        if not isinstance(facts, TestnetCanaryFactPort):
            raise TypeError("transport canary facts must implement the public fact port")
        if not callable(getattr(protection, "preflight", None)):
            raise TypeError("transport canary protection must expose preflight")
        if not callable(getattr(protection, "ensure_position_coverage", None)):
            raise TypeError("transport canary protection must expose ensure_position_coverage")
        self.output_root = Path(output_root)
        self.path = self.output_root / TRANSPORT_CANARY_ROOT / TRANSPORT_CANARY_FILE
        self.broker = broker
        self.facts = facts
        self.protection = protection
        self.park_user_id = str(park_user_id or "park").strip() or "park"
        self.confirmation_ledger = confirmation_ledger or ParkConfirmationLedger(
            self.output_root,
            park_user_id=self.park_user_id,
        )
        self.approved_market_sources = frozenset(
            approved_market_sources
            or {"hyperliquid.external_testnet", "nautilus-hyperliquid.testnet", "fake.external_testnet"}
        )

    def snapshot(self) -> dict[str, Any]:
        try:
            rows = load_json(self.path)
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            return {
                "schema_version": TRANSPORT_CANARY_SCHEMA,
                "status": "BLOCKED",
                "blocker": "transport_canary_state_corrupt",
                "error": type(exc).__name__,
            }
        return dict(rows[-1]) if rows and isinstance(rows[-1], Mapping) else {}

    def run(
        self,
        plan: TestnetCanaryPlan | Mapping[str, Any],
        *,
        confirmation: Mapping[str, Any],
        timestamp: str | datetime,
    ) -> dict[str, Any]:
        mutation_started = False
        network_invoked = False
        try:
            now = _timestamp(timestamp)
            normalized = (
                plan
                if isinstance(plan, TestnetCanaryPlan)
                else TestnetCanaryPlan.from_mapping(plan, now=now)
            )
            normalized.validate(now=now)
            if normalized.instrument_id != _BTC_INSTRUMENT:
                raise TestnetTransportCanaryError("btc_instrument_required")
            network_invoked = True
            self._validate_protection_preflight(normalized)
            canary = TestnetCanary(
                self.output_root,
                self.broker,
                park_user_id=self.park_user_id,
                confirmation_ledger=self.confirmation_ledger,
                approved_market_sources=self.approved_market_sources,
                require_position_protection=True,
            )
            canary.prepare(
                normalized,
                confirmation=confirmation,
                timestamp=now.isoformat(),
            )
            mutation_started = True
            entry = canary.submit_entry(
                normalized,
                confirmation=confirmation,
                timestamp=now.isoformat(),
            )
            if entry.get("status") != "ENTRY_FILLED":
                raise TestnetTransportCanaryError(
                    "entry_not_filled",
                    {"status": entry.get("status")},
                )
            entry_order_id = str(entry.get("entry_order_id") or "").strip()
            filled_quantity = self._filled_quantity(entry)
            protection = self._ensure_protection(
                normalized,
                parent_order_id=entry_order_id,
                filled_quantity=filled_quantity,
                timestamp=now,
            )
            flat = __import__(
                "services.standard_broker_testnet_canary_facts",
                fromlist=["TestnetCanaryFillFlat"],
            ).TestnetCanaryFillFlat(canary, self.facts).complete(
                normalized,
                confirmation=dict(confirmation),
                timestamp=now.isoformat(),
            )
            result = {
                "schema_version": TRANSPORT_CANARY_SCHEMA,
                "event": "transport_canary_completed",
                "status": "FLAT_RECONCILED",
                "canary_id": normalized.canary_id,
                "plan_digest": normalized.plan_digest,
                "instrument_id": normalized.instrument_id,
                "broker_id": normalized.broker_id,
                "environment": normalized.environment,
                "account_fingerprint": normalized.account_fingerprint,
                "runtime_id": normalized.runtime_id,
                "release_sha": normalized.release_sha,
                "capability_revision": normalized.capability_revision,
                "protection": protection,
                "canary": flat,
                "execution_mutation": mutation_started,
                "network_operation_invoked": network_invoked,
                "next_action": "record_canary_evidence",
                "updated_at": now.isoformat(),
            }
            self._save(result)
            return result
        except TestnetTransportCanaryError as exc:
            self._save(
                {
                    "schema_version": TRANSPORT_CANARY_SCHEMA,
                    "event": "transport_canary_blocked",
                    "status": "BLOCKED",
                    "blocker": exc.code,
                    "execution_mutation": mutation_started,
                    "network_operation_invoked": network_invoked,
                    "next_action": "notify_park_and_wait",
                }
            )
            raise
        except Exception as exc:  # noqa: BLE001 - persist a safe blocker at the seam.
            blocker = str(exc) or f"{type(exc).__name__}"
            result = {
                "schema_version": TRANSPORT_CANARY_SCHEMA,
                "event": "transport_canary_blocked",
                "status": "BLOCKED",
                "blocker": blocker,
                "execution_mutation": mutation_started,
                "network_operation_invoked": network_invoked,
                "next_action": "notify_park_and_wait",
            }
            self._save(result)
            raise TestnetTransportCanaryError(blocker) from exc

    def _validate_protection_preflight(self, plan: TestnetCanaryPlan) -> None:
        try:
            value = self.protection.preflight()
        except Exception as exc:  # noqa: BLE001 - no operation is safe after an unknown preflight.
            raise TestnetTransportCanaryError("protection_preflight_unknown") from exc
        if not isinstance(value, Mapping):
            raise TestnetTransportCanaryError("protection_preflight_invalid")
        if (
            value.get("ready") is not True
            or value.get("position_coverage") is not True
            or value.get("reduce_only") is not True
            or value.get("real_money_eligible") is not False
            or str(value.get("environment") or "").lower() != "testnet"
            or str(value.get("capability_revision") or "") != plan.capability_revision
        ):
            raise TestnetTransportCanaryError("protection_capability_gap")

    @staticmethod
    def _filled_quantity(state: Mapping[str, Any]) -> Decimal:
        orders = state.get("orders") if isinstance(state.get("orders"), list) else []
        for row in reversed(orders):
            if str(row.get("operation") or "") == "query":
                quantity = _decimal(row.get("filled_quantity"), "entry_filled_quantity")
                if quantity > 0:
                    return quantity
        raise TestnetTransportCanaryError("entry_filled_quantity_missing")

    def _ensure_protection(
        self,
        plan: TestnetCanaryPlan,
        *,
        parent_order_id: str,
        filled_quantity: Decimal,
        timestamp: datetime,
    ) -> dict[str, Any]:
        try:
            value = self.protection.ensure_position_coverage(
                instrument_id=plan.instrument_id,
                parent_order_id=parent_order_id,
                filled_quantity=filled_quantity,
                account_fingerprint=plan.account_fingerprint,
                runtime_id=plan.runtime_id,
                release_sha=plan.release_sha,
                capability_revision=plan.capability_revision,
                timestamp=timestamp.isoformat(),
            )
        except Exception as exc:  # noqa: BLE001 - protection uncertainty freezes exposure.
            raise TestnetTransportCanaryError("protection_coverage_unknown") from exc
        if not isinstance(value, Mapping):
            raise TestnetTransportCanaryError("protection_coverage_invalid")
        status = str(value.get("status") or "").lower()
        if (
            status not in {"active", "covered"}
            or value.get("covered") is not True
            or value.get("reduce_only") is not True
            or str(value.get("instrument_id") or "") != plan.instrument_id
            or str(value.get("parent_order_id") or "") != parent_order_id
            or str(value.get("capability_revision") or "") != plan.capability_revision
        ):
            raise TestnetTransportCanaryError("protection_coverage_gap")
        covered_quantity = _decimal(value.get("covered_quantity"), "covered_quantity")
        if covered_quantity != filled_quantity:
            raise TestnetTransportCanaryError("protection_quantity_mismatch")
        return _safe_mapping(value)

    def _save(self, value: Mapping[str, Any]) -> None:
        write_json(self.path, [dict(value)])


__all__ = [
    "TRANSPORT_CANARY_SCHEMA",
    "TestnetTransportCanary",
    "TestnetTransportCanaryError",
    "TestnetPositionProtectionPort",
]
