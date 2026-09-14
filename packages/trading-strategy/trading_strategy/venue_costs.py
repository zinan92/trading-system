from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SideCost:
    venue: str
    model_type: str
    notional: float
    spread_cost: float
    slippage_cost: float
    commission: float
    total_cost: float
    effective_bp: float
    model: dict

    def to_dict(self) -> dict:
        return {
            "venue": self.venue,
            "model_type": self.model_type,
            "notional": round(self.notional, 8),
            "spread_cost": round(self.spread_cost, 8),
            "slippage_cost": round(self.slippage_cost, 8),
            "commission": round(self.commission, 8),
            "total_cost": round(self.total_cost, 8),
            "effective_bp": round(self.effective_bp, 8),
            "model": self.model,
        }


def venue_side_cost(
    cost_rules: dict,
    *,
    venue: str,
    price: float,
    quantity: float,
    contract_multiplier: float | None = None,
) -> SideCost:
    model = resolve_venue_cost_model(cost_rules, venue)
    model_type = str(model.get("type", "notional_pct"))
    multiplier = float(contract_multiplier if contract_multiplier is not None else model.get("contract_multiplier", 1.0) or 1.0)
    notional = abs(float(price) * float(quantity) * multiplier)
    spread_pct = float(model.get("spread_pct", 0) or 0)
    slippage_pct = float(model.get("slippage_pct", 0) or 0)
    spread_cost = abs(notional * (spread_pct / 2) / 100)
    slippage_cost = abs(notional * slippage_pct / 100)

    if model_type == "fixed_per_contract":
        contracts = abs(float(quantity))
        commission = max(
            float(model.get("min_commission", 0) or 0),
            contracts
            * (
                float(model.get("commission_per_contract_side", 0) or 0)
                + float(model.get("exchange_fee_per_contract_side", 0) or 0)
                + float(model.get("clearing_fee_per_contract_side", 0) or 0)
            ),
        )
    else:
        commission_pct = float(model.get("commission_pct_notional", 0) or 0)
        commission = max(
            float(model.get("min_commission", 0) or 0),
            float(model.get("commission_per_order", 0) or 0) + notional * commission_pct / 100,
        )

    total = spread_cost + slippage_cost + commission
    effective_bp = (total / notional * 10_000) if notional else 0.0
    return SideCost(
        venue=venue,
        model_type=model_type,
        notional=notional,
        spread_cost=spread_cost,
        slippage_cost=slippage_cost,
        commission=commission,
        total_cost=total,
        effective_bp=effective_bp,
        model=model,
    )


def resolve_venue_cost_model(cost_rules: dict, venue: str) -> dict:
    venues = cost_rules.get("venues") if isinstance(cost_rules.get("venues"), dict) else {}
    if venue in venues:
        return {**_base_cost_fields(cost_rules), **venues[venue]}
    aliases = cost_rules.get("venue_aliases") if isinstance(cost_rules.get("venue_aliases"), dict) else {}
    target = aliases.get(venue)
    if target and target in venues:
        return {**_base_cost_fields(cost_rules), **venues[target]}
    return {**_base_cost_fields(cost_rules), "type": str(cost_rules.get("type", "notional_pct"))}


def _base_cost_fields(cost_rules: dict) -> dict:
    return {
        key: value
        for key, value in cost_rules.items()
        if key
        in {
            "type",
            "spread_pct",
            "slippage_pct",
            "commission_pct_notional",
            "commission_per_order",
            "min_commission",
        }
    }
