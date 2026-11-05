"""One place that decides which venue Paper gold comes from.

A Paper gold plan is trustworthy only when its bars, its instrument definition
and its execution instrument come from the same venue. Before this module those
were five independent config keys. Now one key, ``paper_gold_venue.active`` in
``configs/dualtrack.yaml``, picks a profile, and the two config loaders derive
every touchpoint from it, so they cannot disagree.

There is no fallback: if the active venue is down, Paper gold is blocked. An
unknown ``active`` value fails closed (spec: docs/plans/gold-source-switch-spec.md).
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Mapping


class PaperGoldVenueError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class PaperGoldVenue:
    key: str
    label: str
    venue_profile_id: str
    market_source: str
    execution_market_venue: str
    execution_market_instrument_id: str
    instrument_symbol: str
    instrument_venue: str
    contract: Mapping[str, Any]
    fee_model: Mapping[str, Any] | None = None
    notes: Mapping[str, str] = field(default_factory=dict)

    @property
    def execution_instrument_id(self) -> str:
        return self.instrument_symbol


PROFILES: dict[str, PaperGoldVenue] = {
    "binance": PaperGoldVenue(
        key="binance",
        label="Binance XAUUSDT 永续",
        venue_profile_id="binance.paper",
        market_source="binance_usdm_futures",
        execution_market_venue="binance",
        execution_market_instrument_id="XAUUSDT.BINANCE",
        instrument_symbol="XAUUSDT",
        instrument_venue="BINANCE",
        contract={
            "price_precision": 2,
            "price_increment": "0.01",
            "quantity_precision": 3,
            "quantity_increment": "0.001",
        },
        # None: keep the account-observed Binance demo fee in dualtrack.yaml.
        fee_model=None,
    ),
    "okx": PaperGoldVenue(
        key="okx",
        label="OKX XAU-USDT-SWAP 永续",
        venue_profile_id="okx.paper",
        market_source="okx_swap_public",
        execution_market_venue="okx",
        execution_market_instrument_id="XAU-USDT-SWAP.OKX",
        instrument_symbol="XAU-USDT-SWAP",
        instrument_venue="OKX",
        contract={
            "price_precision": 1,
            "price_increment": "0.1",
            "quantity_precision": 3,
            "quantity_increment": "0.001",
        },
        fee_model={
            "mode": "paper_assumption",
            # Exactly the current Paper rates (Binance demo account, observed
            # 2026-07-10), so switching venue changes one thing, not two.
            "maker_fee_rate": "0",
            "taker_fee_rate": "0.000400",
            "source": "carried_over_current_paper_fee_model",
            "verified": False,
            "note": (
                "No OKX account to observe a rate; okx.com/fees redirected to the "
                "OKX US spot schedule on 2026-11-05. Paper keeps the current "
                "maker 0 / taker 0.04% until Park sets an OKX rate."
            ),
            "environment": "paper",
            "real_money_eligible": False,
        },
    ),
}

DEFAULT_VENUE = "binance"


def active_paper_gold_venue(dualtrack: Mapping[str, Any] | None) -> PaperGoldVenue:
    """Return the configured profile; an unknown key is a fail-closed error."""

    block = (dualtrack or {}).get("paper_gold_venue")
    block = block if isinstance(block, Mapping) else {}
    key = str(block.get("active") or DEFAULT_VENUE).strip().lower()
    if key not in PROFILES:
        raise PaperGoldVenueError(
            "paper_gold_venue_unknown",
            f"paper_gold_venue.active={key!r} is not one of {sorted(PROFILES)}",
        )
    return PROFILES[key]


def apply_to_dualtrack(config: Mapping[str, Any]) -> dict[str, Any]:
    """Derive the dualtrack execution touchpoints from the active profile."""

    venue = active_paper_gold_venue(config)
    result = deepcopy(dict(config))
    result.setdefault("market_data", {})["provider"] = venue.market_source
    contract = result.setdefault("execution_contract", {})
    contract.update(dict(venue.contract))
    contract["execution_instrument_id"] = venue.execution_instrument_id
    nautilus = result.setdefault("execution_shadow", {}).setdefault("nautilus", {})
    nautilus["instrument_source"] = venue.market_source
    nautilus["execution_instrument_id"] = venue.execution_instrument_id
    if venue.fee_model is not None:
        result["paper_fee_model"] = dict(venue.fee_model)
    result["paper_gold_venue"] = {
        **dict(result.get("paper_gold_venue") or {}),
        "active": venue.key,
        "resolved": describe(venue),
    }
    return result


def apply_to_pipeline(config: Mapping[str, Any], dualtrack: Mapping[str, Any]) -> dict[str, Any]:
    """Point the GOLD market route (charts and the `dual` direct read) at the venue."""

    venue = active_paper_gold_venue(dualtrack)
    result = deepcopy(dict(config))
    datafeed = result.setdefault("datafeed", {})
    datafeed["source"] = venue.market_source
    # Only rewrite a GOLD route that exists: with no routes configured the
    # repository builds its defaults (GOLD from datafeed.source, MGCmain, …).
    routes = datafeed.get("instrument_routes")
    if isinstance(routes, dict) and isinstance(routes.get("GOLD"), dict):
        routes["GOLD"]["source"] = venue.market_source
    return result


def describe(venue: PaperGoldVenue) -> dict[str, Any]:
    """The provenance block shown on the chart, the trust gate and receipts."""

    return {
        "key": venue.key,
        "label": venue.label,
        "venue_profile_id": venue.venue_profile_id,
        "market_source": venue.market_source,
        "execution_market": f"{venue.execution_market_venue}/{venue.execution_market_instrument_id}",
        "instrument": f"{venue.instrument_symbol}.{venue.instrument_venue}",
        "price_increment": str(venue.contract["price_increment"]),
        "fee_verified": bool((venue.fee_model or {}).get("verified", True)),
    }


def require_instrument_matches(definition: Mapping[str, Any], venue: PaperGoldVenue) -> None:
    """Bars, instrument definition and execution venue must agree; never mix."""

    source = str(definition.get("source_mode") or definition.get("provider") or "")
    def_venue = str(definition.get("venue") or "").upper()
    symbol = str(definition.get("symbol") or "")
    if (source, def_venue, symbol) != (venue.market_source, venue.instrument_venue, venue.instrument_symbol):
        raise PaperGoldVenueError(
            "paper_gold_venue_mismatch",
            "Paper instrument "
            f"{symbol or '?'}.{def_venue or '?'} ({source or 'unknown source'}) does not match the "
            f"configured Paper gold venue {venue.instrument_symbol}.{venue.instrument_venue} "
            f"({venue.market_source})",
        )


def session_venue_key(session: Mapping[str, Any] | None) -> str:
    """Sessions started before the stamp existed were Binance sessions."""

    return str((session or {}).get("paper_gold_venue") or DEFAULT_VENUE)
