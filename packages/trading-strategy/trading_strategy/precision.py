"""Engine-neutral precision normalization used by Strategy plans.

The helper keeps the source contract's exact increment rounding and immutable
contract evidence without importing an execution engine or a venue adapter.
"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any


EXECUTION_COMMAND_CONTRACT_SCHEMA = "dualtrack-execution-contract-v1"


def normalize_execution_command(
    command: dict[str, Any],
    config: dict[str, Any] | None,
) -> dict[str, Any]:
    """Apply explicit price/quantity increments to a plain command mapping."""

    normalized = dict(command)
    contract_evidence = execution_contract_evidence(config)
    if not contract_evidence:
        return normalized
    price_increment = _positive_decimal(
        contract_evidence["price_increment"],
        "execution price_increment",
    )
    quantity_increment = _positive_decimal(
        contract_evidence["quantity_increment"],
        "execution quantity_increment",
    )

    raw_price = _decimal_or_none(normalized.get("price"))
    raw_market_price = _decimal_or_none(normalized.get("market_price"))
    raw_quantity = _decimal_or_none(
        normalized.get("quantity", normalized.get("contracts"))
    )
    raw_notional = _decimal_or_none(normalized.get("notional"))
    price_basis = raw_price or raw_market_price
    if (
        raw_quantity is None
        and raw_notional is not None
        and price_basis is not None
        and price_basis > 0
    ):
        raw_quantity = raw_notional / price_basis

    if raw_price is not None:
        executable_price = _round_to_increment(raw_price, price_increment)
        if executable_price != raw_price and normalized.get("requested_price") in (
            None,
            "",
        ):
            normalized["requested_price"] = float(raw_price)
        normalized["price"] = float(executable_price)
    if raw_market_price is not None:
        normalized["market_price"] = float(
            _round_to_increment(raw_market_price, price_increment)
        )
    for key in ("sl", "tp"):
        value = _decimal_or_none(normalized.get(key))
        if value is not None:
            normalized[key] = float(_round_to_increment(value, price_increment))

    if raw_quantity is not None:
        executable_quantity = _round_to_increment(raw_quantity, quantity_increment)
        if executable_quantity <= 0:
            raise ValueError("execution quantity rounds to zero at venue precision")
        if executable_quantity != raw_quantity and normalized.get(
            "requested_quantity"
        ) in (None, ""):
            normalized["requested_quantity"] = float(raw_quantity)
        normalized["quantity"] = float(executable_quantity)
        if command.get("contracts") not in (None, ""):
            normalized["contracts"] = float(executable_quantity)
        executable_price = _decimal_or_none(
            normalized.get("price") or normalized.get("market_price")
        )
        if executable_price is not None:
            normalized["notional"] = float(
                (executable_price * executable_quantity).quantize(
                    Decimal("0.00000001")
                )
            )

    normalized["execution_contract"] = contract_evidence
    return normalized


def execution_contract_evidence(
    config: dict[str, Any] | None,
) -> dict[str, Any]:
    """Return deterministic precision and fee-contract evidence."""

    settings = dict((config or {}).get("execution_contract") or {})
    if not settings:
        return {}
    price_increment = _positive_decimal(
        settings.get("price_increment"),
        "execution price_increment",
    )
    quantity_increment = _positive_decimal(
        settings.get("quantity_increment"),
        "execution quantity_increment",
    )
    contract_payload = {
        "schema_version": str(
            settings.get("schema_version") or EXECUTION_COMMAND_CONTRACT_SCHEMA
        ),
        "execution_instrument_id": str(
            settings.get("execution_instrument_id") or ""
        ),
        "price_increment": str(price_increment),
        "quantity_increment": str(quantity_increment),
    }
    if (config or {}).get("capital_per_track_usd") not in (None, ""):
        contract_payload["starting_cash"] = str(
            _positive_decimal(
                (config or {}).get("capital_per_track_usd"),
                "execution starting_cash",
            )
        )
    if (config or {}).get("max_leverage") not in (None, ""):
        contract_payload["max_leverage"] = str(
            _positive_decimal(
                (config or {}).get("max_leverage"),
                "execution max_leverage",
            )
        )
    fee_payload = dict((config or {}).get("paper_fee_model") or {})
    return {
        **contract_payload,
        "contract_hash": _payload_hash(contract_payload),
        "fee_contract_hash": _payload_hash(fee_payload),
    }


def _decimal_or_none(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("execution numeric value is invalid") from exc
    if not parsed.is_finite():
        raise ValueError("execution numeric value must be finite")
    return parsed


def _positive_decimal(value: Any, label: str) -> Decimal:
    parsed = _decimal_or_none(value)
    if parsed is None or parsed <= 0:
        raise ValueError(f"{label} must be positive")
    return parsed


def _round_to_increment(value: Decimal, increment: Decimal) -> Decimal:
    units = (value / increment).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return (units * increment).quantize(increment)


def _payload_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"
