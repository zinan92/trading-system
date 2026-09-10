"""Shared, fail-closed Testnet market document assembly."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import time
from typing import Any, Mapping


MARKET_REQUIRED = (
    "execution_ready", "fresh", "is_synthetic", "fallback_policy", "observed_at",
    "bid", "ask", "mid", "mark", "oracle", "impact", "depth_notional",
    "max_slippage", "max_oracle_deviation_bps", "source", "cursor", "broker_id",
    "environment", "instrument_id", "asset_index", "mapping_revision",
    "universe_revision", "connection_epoch",
)
MARKET_BBO_FIELDS = ("bid", "ask", "mid")
MARKET_READ_ATTEMPTS = 5
DEFAULT_MARKET_MAX_DEVIATION_BPS = Decimal("10")
DEFAULT_MARKET_OBSERVATION_MAX_DELTA_SECONDS = Decimal("10")


class MarketDocumentError(ValueError):
    def __init__(self, reason_code: str, **details: Any) -> None:
        self.reason_code = reason_code
        self.details = details
        super().__init__(reason_code)


def compare_market_observations(
    binding_price: Any,
    reader_price: Any,
    *,
    bid: Any,
    ask: Any,
    max_slippage: Any,
    binding_observed_at: Any = None,
    reader_observed_at: Any = None,
    max_bps: Decimal = DEFAULT_MARKET_MAX_DEVIATION_BPS,
    max_observation_delta: Decimal = DEFAULT_MARKET_OBSERVATION_MAX_DELTA_SECONDS,
) -> dict[str, Any]:
    """Apply the shared bounded market-read rule used by startup and ticks."""
    check: dict[str, Any] = {
        "binding_price": str(binding_price),
        "reader_price": str(reader_price),
    }
    try:
        binding_number = Decimal(str(binding_price))
        reader_number = Decimal(str(reader_price))
        bid_number = Decimal(str(bid))
        ask_number = Decimal(str(ask))
        slippage = Decimal(str(max_slippage))
        bps = Decimal(str(max_bps))
        observation_limit = Decimal(str(max_observation_delta))
    except (InvalidOperation, TypeError, ValueError):
        check.update({"passed": False, "reason_code": "market_price_mismatch"})
        return check
    finite_values = (binding_number, reader_number, bid_number, ask_number, bps, observation_limit)
    if not all(value.is_finite() for value in finite_values) or (
        not slippage.is_finite() and slippage != Decimal("Infinity")
    ):
        check.update({"passed": False, "reason_code": "market_price_mismatch"})
        return check
    tolerance = min(slippage, reader_number * bps / Decimal("10000"))
    deviation = abs(binding_number - reader_number)
    check.update({"deviation": str(deviation), "tolerance": str(tolerance)})
    if (
        slippage <= 0 or bps <= 0 or observation_limit <= 0
        or bid_number >= ask_number
        or not (bid_number <= reader_number <= ask_number)
        or not (bid_number - tolerance <= binding_number <= ask_number + tolerance)
    ):
        check.update({"passed": False, "reason_code": "market_bbo_inconsistent"})
        return check
    if deviation > tolerance:
        check.update({"passed": False, "reason_code": "market_price_mismatch"})
        return check
    if binding_observed_at is not None or reader_observed_at is not None:
        binding_text = str(binding_observed_at or "")
        reader_text = str(reader_observed_at or "")
        try:
            binding_time = datetime.fromisoformat(binding_text.replace("Z", "+00:00"))
            reader_time = datetime.fromisoformat(reader_text.replace("Z", "+00:00"))
            if binding_time.tzinfo is None or reader_time.tzinfo is None:
                raise ValueError
            observed_delta = Decimal(str(abs((binding_time - reader_time).total_seconds())))
        except (TypeError, ValueError):
            check.update({"passed": False, "reason_code": "market_observation_mismatch"})
            return check
        check["observed_delta_s"] = str(observed_delta)
        if observed_delta > observation_limit:
            check.update({"passed": False, "reason_code": "market_observation_mismatch"})
            return check
    check.update({"passed": True, "reason_code": None})
    return check


def bbo_check(market: Mapping[str, Any]) -> dict[str, Any]:
    try:
        bid, ask, mid = (Decimal(str(market[field])) for field in MARKET_BBO_FIELDS)
        passed = bid < ask and bid <= mid <= ask
    except (KeyError, InvalidOperation, TypeError, ValueError):
        bid = ask = mid = None
        passed = False
    return {
        "bid": str(bid) if bid is not None else market.get("bid"),
        "mid": str(mid) if mid is not None else market.get("mid"),
        "ask": str(ask) if ask is not None else market.get("ask"),
        "passed": passed,
    }


def build_market_document(
    preview: Mapping[str, Any], binding_market: Mapping[str, Any], *, instrument_id: str
) -> dict[str, Any]:
    dashboard_market = preview.get("market")
    if not isinstance(dashboard_market, Mapping) or not isinstance(binding_market, Mapping):
        raise MarketDocumentError("market_fact_invalid")
    binding = dict(binding_market)
    source = str(binding.get("source") or "").strip().lower()
    if not source or binding.get("price") in (None, "") or binding.get("observed_at") in (None, ""):
        raise MarketDocumentError("market_fact_invalid")
    market = dict(dashboard_market)
    market.update({key: value for key, value in binding.items() if value is not None})
    market["source"] = source
    market["instrument_id"] = str(binding.get("instrument_id") or instrument_id)
    market["mid"] = binding["price"]
    market["observed_at"] = binding["observed_at"]
    market.setdefault("fallback_policy", "none")
    if "fresh" not in market and "freshness" in binding:
        market["fresh"] = str(binding["freshness"]).lower() == "fresh"
    missing = [field for field in MARKET_REQUIRED if field not in market]
    if missing:
        raise MarketDocumentError("market_facts_missing", fields=missing)
    check = bbo_check(market)
    if not check["passed"]:
        raise MarketDocumentError("market_bbo_inconsistent", market_check=check)
    return market


def read_coherent_market(
    preview: Mapping[str, Any], broker: object, *, instrument_id: str,
    sleep_fn: Any = time.sleep, market_reader: Any = None,
    max_attempts: int = MARKET_READ_ATTEMPTS, read_reader_always: bool = False,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Read binding and public facts, retrying bounded sampling races and BBO failures."""
    if market_reader is None:
        from services.hyperliquid_testnet_market_reader import HyperliquidTestnetMarketReader
        market_reader = HyperliquidTestnetMarketReader()
    checks: list[dict[str, Any]] = []
    for attempt in range(1, max_attempts + 1):
        observed_at = datetime.now(timezone.utc)
        try:
            raw_binding = broker.market_fact(instrument_id=instrument_id, now=observed_at)
        except Exception as exc:  # noqa: BLE001
            raise MarketDocumentError("market_fact_unavailable") from exc
        if not isinstance(raw_binding, Mapping):
            raise MarketDocumentError("market_fact_invalid")
        try:
            needs_reader = read_reader_always or not all(field in raw_binding for field in MARKET_REQUIRED)
            if needs_reader:
                try:
                    raw_reader = market_reader.read(instrument_id)
                except Exception as exc:  # noqa: BLE001
                    raise MarketDocumentError("market_fact_unavailable") from exc
                if not isinstance(raw_reader, Mapping):
                    raise MarketDocumentError("market_fact_invalid")
                check = compare_market_observations(
                    raw_binding.get("price"),
                    raw_reader.get("price"),
                    bid=raw_reader.get("bid"),
                    ask=raw_reader.get("ask"),
                    max_slippage=(raw_reader.get("max_slippage")
                                   or raw_binding.get("max_slippage")
                                   or (preview.get("market") or {}).get("max_slippage")
                                   or "Infinity"),
                    binding_observed_at=raw_binding.get("observed_at"),
                    reader_observed_at=raw_reader.get("observed_at"),
                )
                check["attempt"] = attempt
                if not check["passed"]:
                    checks.append(check)
                    if attempt == max_attempts:
                        raise MarketDocumentError(
                            check.get("reason_code") or "market_price_mismatch", attempts=checks
                        )
                    sleep_fn(1.0)
                    continue
                combined = dict(raw_reader)
                for field in ("source", "mapping_revision", "observed_at"):
                    if raw_binding.get(field) not in (None, ""):
                        combined[field] = raw_binding[field]
                market = build_market_document(preview, combined, instrument_id=instrument_id)
            else:
                market = build_market_document(preview, raw_binding, instrument_id=instrument_id)
        except MarketDocumentError as exc:
            if exc.reason_code != "market_bbo_inconsistent":
                raise
            check = dict(exc.details.get("market_check") or {})
            if needs_reader:
                check["binding_price"] = str(raw_binding.get("price"))
                check["reader_price"] = str(raw_reader.get("price"))
            check["attempt"] = attempt
            checks.append(check)
            if attempt == max_attempts:
                raise MarketDocumentError("market_bbo_inconsistent", attempts=checks) from exc
            sleep_fn(1.0)
            continue
        check = bbo_check(market)
        if needs_reader:
            check["binding_price"] = str(raw_binding.get("price"))
            check["reader_price"] = str(raw_reader.get("price"))
        check["attempt"] = attempt
        checks.append(check)
        return market, checks
    raise MarketDocumentError("market_bbo_inconsistent", attempts=checks)
