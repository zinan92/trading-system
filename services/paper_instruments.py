"""What Paper can run, one instrument per Strategy Session (ADR 0009).

Gold keeps its venue profile. Silver and crude have no hardcoded contract: their
increments come from the datafeed's instrument definition, and a definition that
cannot be read or does not match blocks the instrument. There is no fallback.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Mapping, NamedTuple

from services.dualtrack_instrument_source import fetch_execution_instrument_definition
from services.paper_gold_venue import (
    PROFILES,
    PaperGoldVenue,
    PaperGoldVenueError,
    active_paper_gold_venue,
    require_instrument_matches,
)

GOLD = "gold"
OKX_SWAPS = {"silver": ("XAG", "XAG-USDT-SWAP"), "crude": ("CL", "CL-USDT-SWAP")}
SOURCE = "okx_swap_public"

Fetch = Callable[..., dict[str, Any]]


class MarketIdentity(NamedTuple):
    market_source: str
    execution_market_venue: str
    execution_market_instrument_id: str
    instrument_symbol: str


def instrument_keys() -> tuple[str, ...]:
    return (GOLD, *OKX_SWAPS)


def contract_from_definition(definition: Mapping[str, Any]) -> dict[str, Any]:
    """The contract block Paper uses, in the shape gold's profile states it."""

    return {
        "price_precision": int(definition["price_precision"]),
        "price_increment": str(definition["price_increment"]),
        "quantity_precision": int(definition["size_precision"]),
        "quantity_increment": str(definition["size_increment"]),
    }


def _endpoint(dualtrack: Mapping[str, Any], symbol: str) -> str:
    nautilus = ((dualtrack.get("execution_shadow") or {}).get("nautilus") or {})
    gold_endpoint = str(nautilus.get("instrument_endpoint") or "http://127.0.0.1:8100/api/instruments/commodity/XAUUSDT")
    return f"{gold_endpoint.rsplit('/', 1)[0]}/{symbol}"


def _profile(key: str, asset: str, symbol: str, definition: Mapping[str, Any]) -> PaperGoldVenue:
    return PaperGoldVenue(
        key=key,
        label=f"OKX {symbol} 永续",
        venue_profile_id=PROFILES["okx"].venue_profile_id,
        market_source=SOURCE,
        execution_market_venue="okx",
        execution_market_instrument_id=f"{symbol}.OKX",
        instrument_symbol=symbol,
        instrument_venue="OKX",
        contract=contract_from_definition(definition),
        fee_model=PROFILES["okx"].fee_model,
        notes={"asset": asset},
    )


def require_instrument_key(key: Any) -> str:
    """A known instrument key; anything else fails closed."""

    text = str(key or "").strip().lower()
    if text not in instrument_keys():
        raise PaperGoldVenueError("paper_instrument_unknown", f"{key!r} is not one of {list(instrument_keys())}")
    return text


def session_instrument_key(session: Mapping[str, Any] | None) -> str:
    """Sessions started before #1406 recorded no instrument; they were gold."""

    return str((session or {}).get("paper_instrument") or GOLD)


def active_instrument_key(output_root: Path | str) -> str:
    """The running session's instrument; gold when Paper is idle."""

    from services.park_strategy_session import ParkStrategyIdentityJournal

    return session_instrument_key(ParkStrategyIdentityJournal(Path(output_root)).active_session())


def instrument_symbol(key: str, dualtrack: Mapping[str, Any]) -> str:
    """The execution symbol for a key, without reading the datafeed."""

    if key == GOLD:
        return active_paper_gold_venue(dualtrack).instrument_symbol
    return OKX_SWAPS[require_instrument_key(key)][1]


def market_identity(key: str, dualtrack: Mapping[str, Any]) -> PaperGoldVenue | MarketIdentity:
    """Where one instrument's price is read, without reading its definition."""

    if key == GOLD:
        return active_paper_gold_venue(dualtrack)
    symbol = OKX_SWAPS[require_instrument_key(key)][1]
    return MarketIdentity(SOURCE, "okx", f"{symbol}.OKX", symbol)


def resolve_paper_instrument(
    key: str,
    dualtrack: Mapping[str, Any],
    *,
    fetch: Fetch | None = None,
) -> PaperGoldVenue:
    """The trusted profile for one instrument, or a fail-closed error."""

    return resolve_paper_instrument_definition(key, dualtrack, fetch=fetch)[0]


def resolve_paper_instrument_definition(
    key: str,
    dualtrack: Mapping[str, Any],
    *,
    fetch: Fetch | None = None,
) -> tuple[PaperGoldVenue, dict[str, Any] | None]:
    """The profile and, for silver and crude, the definition it was built from.

    Gold returns no definition: its contract is the venue profile, and the
    runtime keeps fetching gold's definition the way it did before #1406.
    """

    if key == GOLD:
        return active_paper_gold_venue(dualtrack), None
    require_instrument_key(key)
    asset, symbol = OKX_SWAPS[key]
    try:
        definition = (fetch or fetch_execution_instrument_definition)(endpoint=_endpoint(dualtrack, symbol), source=SOURCE)
    except (RuntimeError, ValueError) as exc:
        raise PaperGoldVenueError("paper_instrument_unavailable", f"{symbol}: {exc}") from exc
    if str(definition.get("status")) != "TRADING":
        raise PaperGoldVenueError("paper_instrument_not_trading", f"{symbol} status is {definition.get('status')!r}")
    if str(definition.get("contract_multiplier")) != "1":
        # Quantity is in the base unit. Another multiplier would scale every loss Park is shown.
        raise PaperGoldVenueError("paper_instrument_multiplier", f"{symbol} multiplier is {definition.get('contract_multiplier')!r}")
    profile = _profile(key, asset, symbol, definition)
    require_instrument_matches(definition, profile)
    return profile, dict(definition)


def profile_for_definition(key: str, definition: Mapping[str, Any]) -> PaperGoldVenue:
    """Silver or crude's profile from a definition already read this pass."""

    asset, symbol = OKX_SWAPS[require_instrument_key(key)]
    profile = _profile(key, asset, symbol, definition)
    require_instrument_matches(definition, profile)
    return profile


def execution_contract(venue: PaperGoldVenue) -> dict[str, Any]:
    """The order-rounding contract for one instrument, in dualtrack's shape."""

    return {
        "schema_version": "dualtrack-execution-contract-v1",
        **dict(venue.contract),
        "execution_instrument_id": venue.execution_instrument_id,
    }
