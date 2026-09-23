from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .venue_costs import resolve_venue_cost_model, venue_side_cost


@dataclass(frozen=True)
class DualTrackOrderCost:
    notional: float
    cost: float
    cost_model: dict[str, Any]
    quantity: float | None = None
    contracts: float | None = None

    def fill_fields(self) -> dict[str, Any]:
        fields: dict[str, Any] = {
            "notional": round(float(self.notional), 8),
            "cost": round(float(self.cost), 8),
            "cost_model": self.cost_model,
        }
        if self.quantity is not None:
            fields["quantity"] = _clean_number(self.quantity)
        if self.contracts is not None:
            fields["contracts"] = _clean_number(self.contracts)
        return fields


def dualtrack_cost_descriptor(config: dict[str, Any], cost_rules: dict[str, Any] | None = None) -> dict[str, Any]:
    model = _execution_cost_model(config)
    venue = str(model.get("venue") or "")
    if not venue:
        paper_fee_model = _paper_fee_model(config)
        if paper_fee_model:
            return {
                "model_type": "maker_taker_notional",
                "maker_fee_rate": _fee_rate(paper_fee_model, "maker"),
                "taker_fee_rate": _fee_rate(paper_fee_model, "taker"),
                "source": str(paper_fee_model.get("source") or "explicit_paper_fee_model"),
                "real_money_eligible": False,
            }
        return {"cost_per_side_bp": float(config["cost_per_side_bp"])}
    resolved = resolve_venue_cost_model(cost_rules or {}, venue)
    return {
        "venue": venue,
        "model_type": str(resolved.get("type", "notional_pct")),
        "quantity_mode": str(model.get("quantity_mode") or "integer_contracts"),
        "contracts_per_rung": _clean_number(_contracts_per_rung(model)),
        "contract_multiplier": _clean_number(_contract_multiplier(model, resolved)),
        "commission_per_contract_side": _clean_number(float(resolved.get("commission_per_contract_side", 0) or 0)),
        "requires_bill_confirmation": bool(resolved.get("requires_bill_confirmation", False)),
    }


def dualtrack_order_cost(
    *,
    config: dict[str, Any],
    price: float,
    notional: float | None = None,
    contracts: float | None = None,
    cost_rules: dict[str, Any] | None = None,
    require_contracts: bool = False,
    liquidity: str | None = None,
) -> DualTrackOrderCost:
    model = _execution_cost_model(config)
    venue = str(model.get("venue") or "")
    if not venue:
        if notional is None:
            raise ValueError("notional is required for bp dualtrack cost model")
        paper_fee_model = _paper_fee_model(config)
        if paper_fee_model:
            liquidity_value = str(liquidity or "").lower()
            if liquidity_value not in {"maker", "taker"}:
                raise ValueError("liquidity must be maker or taker for explicit paper fee model")
            rate = _fee_rate(paper_fee_model, liquidity_value)
            return DualTrackOrderCost(
                notional=float(notional),
                cost=float(notional) * rate,
                cost_model={
                    "model_type": "maker_taker_notional",
                    "liquidity": liquidity_value,
                    "fee_rate": rate,
                    "source": str(paper_fee_model.get("source") or "explicit_paper_fee_model"),
                    "real_money_eligible": False,
                },
            )
        bp = float(config["cost_per_side_bp"])
        cost = float(notional) * bp / 10_000.0
        return DualTrackOrderCost(
            notional=float(notional),
            cost=cost,
            cost_model={"cost_per_side_bp": bp},
        )

    if cost_rules is None:
        raise ValueError("cost_rules is required for venue dualtrack cost model")
    if contracts is None:
        if require_contracts:
            raise ValueError("contracts is required for venue dualtrack cost model")
        contracts = _contracts_per_rung(model)
    if float(contracts) <= 0:
        raise ValueError("contracts must be positive")

    resolved = resolve_venue_cost_model(cost_rules, venue)
    multiplier = _contract_multiplier(model, resolved)
    side_cost = venue_side_cost(
        cost_rules,
        venue=venue,
        price=float(price),
        quantity=float(contracts),
        contract_multiplier=multiplier,
    )
    descriptor = {
        "venue": side_cost.venue,
        "model_type": side_cost.model_type,
        "quantity_mode": str(model.get("quantity_mode") or "integer_contracts"),
        "contracts_per_rung": _clean_number(_contracts_per_rung(model)),
        "contract_multiplier": _clean_number(multiplier),
        "side_cost": side_cost.to_dict(),
    }
    return DualTrackOrderCost(
        notional=side_cost.notional,
        cost=side_cost.total_cost,
        quantity=float(contracts),
        contracts=float(contracts),
        cost_model=descriptor,
    )


def _execution_cost_model(config: dict[str, Any]) -> dict[str, Any]:
    model = config.get("execution_cost_model")
    return model if isinstance(model, dict) else {}


def _paper_fee_model(config: dict[str, Any]) -> dict[str, Any]:
    model = config.get("paper_fee_model")
    return model if isinstance(model, dict) else {}


def _fee_rate(model: dict[str, Any], liquidity: str) -> float:
    value = model.get(f"{liquidity}_fee_rate")
    if value in (None, ""):
        raise ValueError(f"explicit paper fee model is missing {liquidity}_fee_rate")
    rate = float(value)
    if rate < 0:
        raise ValueError(f"{liquidity}_fee_rate must not be negative")
    return rate


def _contracts_per_rung(model: dict[str, Any]) -> float:
    value = model.get("contracts_per_rung", model.get("min_contracts", 1))
    contracts = float(value)
    if contracts <= 0:
        raise ValueError("contracts_per_rung must be positive")
    if not contracts.is_integer():
        raise ValueError("contracts_per_rung must be a whole number")
    return contracts


def _contract_multiplier(model: dict[str, Any], resolved: dict[str, Any]) -> float:
    value = model.get("contract_multiplier", resolved.get("contract_multiplier", 1))
    multiplier = float(value or 1)
    if multiplier <= 0:
        raise ValueError("contract_multiplier must be positive")
    return multiplier


def _clean_number(value: float) -> int | float:
    number = float(value)
    return int(number) if number.is_integer() else round(number, 8)
