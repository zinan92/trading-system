"""Pure projection from the canonical Paper DCA plan to an external binding.

The projection owns no transport and performs no I/O.  It keeps the canonical
StrategyPlan as the source of entry geometry and fixed-notional economics while
mapping quantities to the selected Broker Instrument precision.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_FLOOR
from typing import Any, Mapping

from services.standard_broker_external_dca import (
    ExternalDcaPlan,
    external_dca_plan_digest,
)


class DcaProjectionError(ValueError):
    """Canonical DCA cannot be projected without changing its semantics."""


@dataclass(frozen=True)
class ExternalDcaPlanProjection:
    """External plan mapping plus the canonical StrategyPlan identity."""

    plan: dict[str, Any]
    source_strategy_plan_id: str
    source_strategy_plan_digest: str


_BINDING_FIELDS = (
    "plan_id",
    "broker_id",
    "environment",
    "profile_id",
    "account_fingerprint",
    "runtime_id",
    "release_sha",
    "capability_revision",
    "instrument_id",
    "contract_multiplier",
    "quantity_step",
    "price_tick",
    "max_slippage",
    "max_notional",
    "max_leverage",
    "account_equity",
    "max_open_orders",
    "max_open_positions",
    "fee_budget_usd",
    "max_loss_usd",
    "time_in_force",
    "expires_at",
    "close_price",
)


def project_canonical_dca_plan(
    strategy_plan: Mapping[str, Any],
    *,
    binding: Mapping[str, Any],
) -> ExternalDcaPlanProjection:
    """Project one old Paper DCA StrategyPlan into an external plan mapping."""

    if not isinstance(strategy_plan, Mapping):
        raise DcaProjectionError("strategy_plan_invalid")
    if strategy_plan.get("schema_version") != "strategy-plan-v1":
        raise DcaProjectionError("strategy_plan_schema_invalid")
    if strategy_plan.get("strategy_type") != "dca":
        raise DcaProjectionError("strategy_plan_type_invalid")
    source_plan_id = _text(strategy_plan.get("strategy_plan_id"), "strategy_plan_id")
    source_digest = _text(strategy_plan.get("plan_digest"), "strategy_plan_digest")
    if not source_digest.startswith("sha256:"):
        raise DcaProjectionError("strategy_plan_digest_invalid")
    if not isinstance(binding, Mapping):
        raise DcaProjectionError("binding_invalid")
    missing = [field for field in _BINDING_FIELDS if field not in binding]
    if missing:
        raise DcaProjectionError("binding_fields_missing:" + ",".join(missing))

    dca = strategy_plan.get("dca")
    if not isinstance(dca, Mapping):
        raise DcaProjectionError("dca_section_missing")
    entries = dca.get("entries")
    if not isinstance(entries, list) or not entries:
        raise DcaProjectionError("dca_entries_missing")
    try:
        quantity_step = _decimal(binding["quantity_step"], "quantity_step")
        price_tick = _decimal(binding["price_tick"], "price_tick")
        contract_multiplier = _decimal(binding["contract_multiplier"], "contract_multiplier")
    except DcaProjectionError:
        raise

    mapped_levels: list[str] = []
    mapped_quantities: list[str] = []
    canonical_notional = _decimal(dca.get("notional_per_addition"), "notional_per_addition")
    for entry in entries:
        if not isinstance(entry, Mapping):
            raise DcaProjectionError("dca_entry_invalid")
        price = _decimal(entry.get("price"), "dca_entry_price")
        notional = _decimal(entry.get("notional"), "dca_entry_notional")
        if not _aligned(price, price_tick):
            raise DcaProjectionError("canonical_price_precision_mismatch")
        if notional > canonical_notional:
            raise DcaProjectionError("canonical_notional_mismatch")
        # The canonical strategy's target notional is authoritative.  The
        # source entry's ``notional`` may already be reduced by the old venue's
        # quantity floor, so reusing it would silently shrink the strategy.
        quantity = _floor_quantity(canonical_notional / price / contract_multiplier, quantity_step)
        mapped_levels.append(str(price))
        mapped_quantities.append(str(quantity))

    target_price = _decimal(dca.get("target_price"), "target_price")
    stop_price = _decimal(dca.get("stop_price"), "stop_price")
    for price in (target_price, stop_price, _decimal(binding["close_price"], "close_price")):
        if not _aligned(price, price_tick):
            raise DcaProjectionError("canonical_price_precision_mismatch")

    try:
        version = int(strategy_plan.get("version"))
    except (TypeError, ValueError) as exc:
        raise DcaProjectionError("strategy_plan_version_invalid") from exc
    mapped: dict[str, Any] = {
        "plan_id": _text(binding["plan_id"], "plan_id"),
        "plan_version": version,
        "cycle_id": _text(strategy_plan.get("cycle_id"), "cycle_id"),
        "strategy_session_id": _text(strategy_plan.get("strategy_session_id"), "strategy_session_id"),
        "strategy_revision_id": _text(strategy_plan.get("strategy_revision_id"), "strategy_revision_id"),
        "broker_id": _text(binding["broker_id"], "broker_id"),
        "environment": _text(binding["environment"], "environment"),
        "profile_id": _text(binding["profile_id"], "profile_id"),
        "account_fingerprint": _text(binding["account_fingerprint"], "account_fingerprint"),
        "runtime_id": _text(binding["runtime_id"], "runtime_id"),
        "release_sha": _text(binding["release_sha"], "release_sha"),
        "capability_revision": _text(binding["capability_revision"], "capability_revision"),
        "instrument_id": _text(binding["instrument_id"], "instrument_id"),
        "direction": _text(strategy_plan.get("direction"), "direction"),
        "entry_levels": mapped_levels,
        "entry_quantities": mapped_quantities,
        "contract_multiplier": str(contract_multiplier),
        "target_price": str(target_price),
        "stop_price": str(stop_price),
        "close_price": str(_decimal(binding["close_price"], "close_price")),
        "time_in_force": _text(binding["time_in_force"], "time_in_force"),
        "quantity_step": str(quantity_step),
        "price_tick": str(price_tick),
        "max_slippage": str(_decimal(binding["max_slippage"], "max_slippage")),
        "max_notional": str(_decimal(binding["max_notional"], "max_notional")),
        "max_leverage": str(_decimal(binding["max_leverage"], "max_leverage")),
        "account_equity": str(_decimal(binding["account_equity"], "account_equity")),
        "max_open_orders": int(binding["max_open_orders"]),
        "max_open_positions": int(binding["max_open_positions"]),
        "fee_budget_usd": str(_decimal(binding["fee_budget_usd"], "fee_budget_usd")),
        "max_loss_usd": str(_decimal(binding["max_loss_usd"], "max_loss_usd")),
        "expires_at": _text(binding["expires_at"], "expires_at"),
    }
    mapped["plan_digest"] = external_dca_plan_digest(mapped)
    try:
        ExternalDcaPlan.from_mapping(mapped)
    except Exception as exc:  # noqa: BLE001 - expose one projection error type.
        raise DcaProjectionError(f"external_plan_invalid:{type(exc).__name__}") from exc
    return ExternalDcaPlanProjection(
        plan=mapped,
        source_strategy_plan_id=source_plan_id,
        source_strategy_plan_digest=source_digest,
    )


def _text(value: object, field: str) -> str:
    result = str(value or "").strip()
    if not result:
        raise DcaProjectionError(f"{field}_missing")
    return result


def _decimal(value: object, field: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise DcaProjectionError(f"{field}_invalid") from exc
    if not result.is_finite() or result <= 0:
        raise DcaProjectionError(f"{field}_invalid")
    return result


def _aligned(value: Decimal, step: Decimal) -> bool:
    return step > 0 and value % step == 0


def _floor_quantity(value: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        raise DcaProjectionError("quantity_step_invalid")
    quantity = (value / step).to_integral_value(rounding=ROUND_FLOOR) * step
    if quantity <= 0:
        raise DcaProjectionError("mapped_quantity_rounds_to_zero")
    return quantity
