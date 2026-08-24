"""Pure projection from the canonical Paper DCA plan to an external binding."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_FLOOR
from typing import Any, Mapping

from services.dca_plan import dca_strategy_plan_digest
from services.standard_broker_external_dca import (
    ExternalDcaPlan,
    external_dca_plan_digest,
)


class DcaProjectionError(ValueError):
    """Canonical DCA cannot be projected without changing its semantics."""


@dataclass(frozen=True)
class ExternalDcaMarketSource:
    """Execution-grade market source identity selected by a Broker binding."""

    source_id: str
    broker_id: str
    environment: str
    instrument_id: str
    execution_venue: bool

    @classmethod
    def from_mapping(cls, value: object) -> "ExternalDcaMarketSource":
        if not isinstance(value, Mapping):
            raise DcaProjectionError("market_source_invalid")
        if value.get("execution_venue") is not True:
            raise DcaProjectionError("market_source_not_execution_venue")
        return cls(
            source_id=_text(value.get("source_id"), "market_source_id"),
            broker_id=_text(value.get("broker_id"), "market_source_broker_id").lower(),
            environment=_text(value.get("environment"), "market_source_environment").lower(),
            instrument_id=_text(value.get("instrument_id"), "market_source_instrument_id"),
            execution_venue=True,
        )


@dataclass(frozen=True)
class ExternalDcaBindingSpec:
    """Typed Broker/environment/risk binding for one external DCA projection."""

    plan_id: str
    broker_id: str
    environment: str
    profile_id: str
    account_fingerprint: str
    runtime_id: str
    release_sha: str
    capability_revision: str
    instrument_id: str
    contract_multiplier: Decimal
    quantity_step: Decimal
    price_tick: Decimal
    max_slippage: Decimal
    max_notional: Decimal
    max_leverage: Decimal
    account_equity: Decimal
    max_open_orders: int
    max_open_positions: int
    fee_budget_usd: Decimal
    max_loss_usd: Decimal
    time_in_force: str
    expires_at: str
    close_price: Decimal
    market_source: ExternalDcaMarketSource

    @classmethod
    def from_mapping(cls, value: object) -> "ExternalDcaBindingSpec":
        if not isinstance(value, Mapping):
            raise DcaProjectionError("binding_invalid")
        forbidden = {
            "secret",
            "private_key",
            "credential",
            "credential_source",
            "signed_payload",
            "signature",
        }.intersection(str(key) for key in value)
        if forbidden:
            raise DcaProjectionError("binding_secret_field_forbidden")
        required = (
            "plan_id", "broker_id", "environment", "profile_id",
            "account_fingerprint", "runtime_id", "release_sha",
            "capability_revision", "instrument_id", "contract_multiplier",
            "quantity_step", "price_tick", "max_slippage", "max_notional",
            "max_leverage", "account_equity", "max_open_orders",
            "max_open_positions", "fee_budget_usd", "max_loss_usd",
            "time_in_force", "expires_at", "close_price", "market_source",
        )
        missing = [field for field in required if field not in value]
        if missing:
            raise DcaProjectionError("binding_fields_missing:" + ",".join(missing))
        market_source = ExternalDcaMarketSource.from_mapping(value["market_source"])
        spec = cls(
            plan_id=_text(value["plan_id"], "plan_id"),
            broker_id=_text(value["broker_id"], "broker_id").lower(),
            environment=_text(value["environment"], "environment").lower(),
            profile_id=_text(value["profile_id"], "profile_id"),
            account_fingerprint=_text(value["account_fingerprint"], "account_fingerprint"),
            runtime_id=_text(value["runtime_id"], "runtime_id"),
            release_sha=_text(value["release_sha"], "release_sha"),
            capability_revision=_text(value["capability_revision"], "capability_revision"),
            instrument_id=_text(value["instrument_id"], "instrument_id"),
            contract_multiplier=_decimal(value["contract_multiplier"], "contract_multiplier"),
            quantity_step=_decimal(value["quantity_step"], "quantity_step"),
            price_tick=_decimal(value["price_tick"], "price_tick"),
            max_slippage=_decimal(value["max_slippage"], "max_slippage"),
            max_notional=_decimal(value["max_notional"], "max_notional"),
            max_leverage=_decimal(value["max_leverage"], "max_leverage"),
            account_equity=_decimal(value["account_equity"], "account_equity"),
            max_open_orders=_positive_int(value["max_open_orders"], "max_open_orders"),
            max_open_positions=_positive_int(value["max_open_positions"], "max_open_positions"),
            fee_budget_usd=_decimal(value["fee_budget_usd"], "fee_budget_usd", nonnegative=True),
            max_loss_usd=_decimal(value["max_loss_usd"], "max_loss_usd"),
            time_in_force=_text(value["time_in_force"], "time_in_force").lower(),
            expires_at=_text(value["expires_at"], "expires_at"),
            close_price=_decimal(value["close_price"], "close_price"),
            market_source=market_source,
        )
        if (
            spec.market_source.broker_id != spec.broker_id
            or spec.market_source.environment != spec.environment
            or spec.market_source.instrument_id != spec.instrument_id
        ):
            raise DcaProjectionError("market_source_binding_mismatch")
        return spec


@dataclass(frozen=True)
class ExternalDcaPlanProjection:
    """External plan mapping plus canonical strategy and source identities."""

    plan: dict[str, Any]
    source_strategy_plan_id: str
    source_strategy_plan_digest: str
    canonical_semantics: dict[str, Any]
    source_market: dict[str, str]
    execution_market_source: ExternalDcaMarketSource


def project_canonical_dca_plan(
    strategy_plan: Mapping[str, Any],
    *,
    binding: ExternalDcaBindingSpec | Mapping[str, Any],
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
    if dca_strategy_plan_digest(dict(strategy_plan)) != source_digest:
        raise DcaProjectionError("strategy_plan_digest_mismatch")
    spec = (
        binding
        if isinstance(binding, ExternalDcaBindingSpec)
        else ExternalDcaBindingSpec.from_mapping(binding)
    )

    dca = strategy_plan.get("dca")
    if not isinstance(dca, Mapping):
        raise DcaProjectionError("dca_section_missing")
    entries = dca.get("entries")
    if not isinstance(entries, list) or not entries:
        raise DcaProjectionError("dca_entries_missing")
    max_additions = _positive_int(dca.get("max_additions"), "max_additions")
    if max_additions > len(entries):
        raise DcaProjectionError("max_additions_exceeds_entries")
    if dca.get("loop_enabled") is not False:
        raise DcaProjectionError("loop_enabled_true_unsupported")

    mapped_levels: list[str] = []
    mapped_quantities: list[str] = []
    canonical_notional = _decimal(dca.get("notional_per_addition"), "notional_per_addition")
    for entry in entries[:max_additions]:
        if not isinstance(entry, Mapping):
            raise DcaProjectionError("dca_entry_invalid")
        price = _decimal(entry.get("price"), "dca_entry_price")
        notional = _decimal(entry.get("notional"), "dca_entry_notional")
        if not _aligned(price, spec.price_tick):
            raise DcaProjectionError("canonical_price_precision_mismatch")
        if notional > canonical_notional:
            raise DcaProjectionError("canonical_notional_mismatch")
        quantity = _floor_quantity(
            canonical_notional / price / spec.contract_multiplier,
            spec.quantity_step,
        )
        mapped_levels.append(str(price))
        mapped_quantities.append(str(quantity))

    target_price = _decimal(dca.get("target_price"), "target_price")
    stop_price = _decimal(dca.get("stop_price"), "stop_price")
    if not _aligned(target_price, spec.price_tick) or not _aligned(stop_price, spec.price_tick):
        raise DcaProjectionError("canonical_price_precision_mismatch")
    if not _aligned(spec.close_price, spec.price_tick):
        raise DcaProjectionError("binding_close_price_precision_mismatch")

    source_market = _source_market(strategy_plan)
    if (
        source_market["provider"] != spec.market_source.source_id
        or source_market["symbol"] != spec.market_source.instrument_id
    ):
        raise DcaProjectionError("strategy_market_binding_mismatch")
    aggregate_take_profit = _aggregate_take_profit(dca, target_price, strategy_plan)
    mapped: dict[str, Any] = {
        "plan_id": spec.plan_id,
        "plan_version": _positive_int(strategy_plan.get("version"), "strategy_plan_version"),
        "cycle_id": _text(strategy_plan.get("cycle_id"), "cycle_id"),
        "strategy_session_id": _text(
            strategy_plan.get("strategy_session_id"),
            "strategy_session_id",
        ),
        "strategy_revision_id": _text(
            strategy_plan.get("strategy_revision_id"),
            "strategy_revision_id",
        ),
        "broker_id": spec.broker_id,
        "environment": spec.environment,
        "profile_id": spec.profile_id,
        "account_fingerprint": spec.account_fingerprint,
        "runtime_id": spec.runtime_id,
        "release_sha": spec.release_sha,
        "capability_revision": spec.capability_revision,
        "instrument_id": spec.instrument_id,
        "direction": _text(strategy_plan.get("direction"), "direction"),
        "entry_levels": mapped_levels,
        "entry_quantities": mapped_quantities,
        "contract_multiplier": str(spec.contract_multiplier),
        "target_price": str(target_price),
        "stop_price": str(stop_price),
        "close_price": str(spec.close_price),
        "time_in_force": spec.time_in_force,
        "quantity_step": str(spec.quantity_step),
        "price_tick": str(spec.price_tick),
        "max_slippage": str(spec.max_slippage),
        "max_notional": str(spec.max_notional),
        "max_leverage": str(spec.max_leverage),
        "account_equity": str(spec.account_equity),
        "max_open_orders": spec.max_open_orders,
        "max_open_positions": spec.max_open_positions,
        "fee_budget_usd": str(spec.fee_budget_usd),
        "max_loss_usd": str(spec.max_loss_usd),
        "expires_at": spec.expires_at,
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
        canonical_semantics={
            "direction": _text(strategy_plan.get("direction"), "direction"),
            "notional_per_addition": str(canonical_notional),
            "max_additions": max_additions,
            "loop_enabled": False,
            "aggregate_take_profit": aggregate_take_profit,
            "target_price": str(target_price),
            "stop_price": str(stop_price),
        },
        source_market=source_market,
        execution_market_source=spec.market_source,
    )


def _source_market(strategy_plan: Mapping[str, Any]) -> dict[str, str]:
    context = strategy_plan.get("execution_context")
    market = context.get("market") if isinstance(context, Mapping) else None
    if not isinstance(market, Mapping):
        raise DcaProjectionError("strategy_market_missing")
    return {
        "provider": _text(market.get("provider"), "strategy_market_provider"),
        "symbol": _text(market.get("symbol"), "strategy_market_symbol"),
        "timeframe": _text(market.get("timeframe"), "strategy_market_timeframe"),
    }


def _aggregate_take_profit(
    dca: Mapping[str, Any],
    target_price: Decimal,
    strategy_plan: Mapping[str, Any],
) -> dict[str, Any]:
    value = dca.get("aggregate_take_profit")
    if not isinstance(value, Mapping):
        raise DcaProjectionError("aggregate_take_profit_missing")
    direction = _text(strategy_plan.get("direction"), "direction").lower()
    expected = {
        "side": "sell" if direction == "long" else "buy",
        "event": "target",
        "order_type": "limit",
        "reduce_only": True,
        "price": target_price,
        "quantity_source": "reconciled_open_dca_round_quantity",
        "replace_after_each_entry_fill": True,
        "one_active_order_required": True,
    }
    for field, expected_value in expected.items():
        actual = value.get(field)
        if field == "price":
            try:
                matches = Decimal(str(actual)) == expected_value
            except (InvalidOperation, TypeError, ValueError):
                matches = False
        else:
            matches = actual == expected_value
        if not matches:
            raise DcaProjectionError("aggregate_take_profit_semantics_invalid")
    return dict(value)


def _text(value: object, field: str) -> str:
    result = str(value or "").strip()
    if not result:
        raise DcaProjectionError(f"{field}_missing")
    return result


def _decimal(value: object, field: str, *, nonnegative: bool = False) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise DcaProjectionError(f"{field}_invalid") from exc
    if not result.is_finite() or (result < 0 if nonnegative else result <= 0):
        raise DcaProjectionError(f"{field}_invalid")
    return result


def _positive_int(value: object, field: str) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise DcaProjectionError(f"{field}_invalid") from exc
    if result <= 0:
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
