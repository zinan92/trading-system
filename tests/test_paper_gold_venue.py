"""One key picks the Paper gold venue; every touchpoint follows it (2026-11-05).

Spec: docs/plans/gold-source-switch-spec.md. Park chose OKX XAU-USDT-SWAP.
"""

from __future__ import annotations

from copy import deepcopy

import pytest

from services.datafeed_execution_market_client import execution_market_payload
from services.dualtrack_nautilus_instrument import validate_instrument_definition
from services.paper_gold_venue import (
    PROFILES,
    PaperGoldVenueError,
    active_paper_gold_venue,
    apply_to_dualtrack,
    apply_to_pipeline,
    require_instrument_matches,
    session_venue_key,
)
from tests.test_dualtrack_nautilus_instrument import _definition as _binance_definition


def _okx_definition() -> dict:
    definition = _binance_definition()
    definition.update(
        {
            "instrument_id": "XAU-USDT-SWAP.OKX",
            "venue": "OKX",
            "symbol": "XAU-USDT-SWAP",
            "market_type": "usdt_perpetual_swap",
            "contract_type": "PERPETUAL",
            "price_precision": 1,
            "price_increment": "0.1",
            "min_price": "0.1",
            "max_price": None,
            "min_notional": None,
            "provider": "okx_swap_public",
            "source_mode": "okx_swap_public",
            "not_applicable_fields": {
                "min_notional": "OKX publishes no minimum notional; min_quantity bounds order size",
                "max_price": "OKX publishes no static maximum price; price limits are dynamic",
            },
        }
    )
    return definition


# ---- choosing the venue ----------------------------------------------------


def test_unknown_venue_fails_closed() -> None:
    with pytest.raises(PaperGoldVenueError) as caught:
        active_paper_gold_venue({"paper_gold_venue": {"active": "kraken"}})
    assert caught.value.code == "paper_gold_venue_unknown"


def test_missing_block_keeps_the_historical_binance_default() -> None:
    assert active_paper_gold_venue({}).key == "binance"
    assert session_venue_key({"session_id": "old"}) == "binance"


@pytest.mark.parametrize("key", sorted(PROFILES))
def test_every_touchpoint_follows_one_key(key: str) -> None:
    venue = PROFILES[key]
    resolved = apply_to_dualtrack(
        {
            "paper_gold_venue": {"active": key},
            "paper_fee_model": {"taker_fee_rate": "0.000400", "source": "binance_demo_account"},
        }
    )
    nautilus = resolved["execution_shadow"]["nautilus"]
    assert resolved["market_data"]["provider"] == venue.market_source
    assert nautilus["instrument_source"] == venue.market_source
    assert nautilus["execution_instrument_id"] == venue.instrument_symbol
    assert resolved["execution_contract"]["execution_instrument_id"] == venue.instrument_symbol
    assert resolved["execution_contract"]["price_increment"] == venue.contract["price_increment"]
    assert resolved["paper_gold_venue"]["resolved"]["venue_profile_id"] == venue.venue_profile_id


def test_okx_fee_is_marked_as_an_unverified_paper_assumption() -> None:
    resolved = apply_to_dualtrack({"paper_gold_venue": {"active": "okx"}})
    fee = resolved["paper_fee_model"]
    assert fee["verified"] is False
    assert fee["real_money_eligible"] is False
    assert fee["environment"] == "paper"
    assert resolved["paper_gold_venue"]["resolved"]["fee_verified"] is False


def test_binance_keeps_its_account_observed_fee() -> None:
    observed = {"taker_fee_rate": "0.000400", "source": "binance_demo_account"}
    resolved = apply_to_dualtrack({"paper_gold_venue": {"active": "binance"}, "paper_fee_model": observed})
    assert resolved["paper_fee_model"] == observed


def test_pipeline_gold_route_follows_the_venue_without_inventing_routes() -> None:
    dualtrack = {"paper_gold_venue": {"active": "okx"}}
    routed = apply_to_pipeline(
        {"datafeed": {"source": "binance_usdm_futures", "instrument_routes": {"GOLD": {"source": "binance_usdm_futures"}}}},
        dualtrack,
    )
    assert routed["datafeed"]["source"] == "okx_swap_public"
    assert routed["datafeed"]["instrument_routes"]["GOLD"]["source"] == "okx_swap_public"

    bare = apply_to_pipeline({"datafeed": {}}, dualtrack)
    assert bare["datafeed"]["source"] == "okx_swap_public"
    assert "instrument_routes" not in bare["datafeed"]


def test_repository_config_resolves_to_okx_everywhere() -> None:
    from services.config_loader import load_pipeline_config
    from services.dualtrack_config import dualtrack_config

    resolved = dualtrack_config()
    assert resolved["paper_gold_venue"]["active"] == "okx"
    assert resolved["execution_contract"]["execution_instrument_id"] == "XAU-USDT-SWAP"
    assert resolved["execution_contract"]["price_increment"] == "0.1"
    assert resolved["execution_shadow"]["nautilus"]["instrument_source"] == "okx_swap_public"
    assert load_pipeline_config()["datafeed"]["source"] == "okx_swap_public"


# ---- never mix venues ------------------------------------------------------


def test_instrument_from_another_venue_is_refused() -> None:
    with pytest.raises(PaperGoldVenueError) as caught:
        require_instrument_matches(_binance_definition(), PROFILES["okx"])
    assert caught.value.code == "paper_gold_venue_mismatch"
    require_instrument_matches(_okx_definition(), PROFILES["okx"])
    require_instrument_matches(_binance_definition(), PROFILES["binance"])


def test_execution_market_price_from_another_source_is_untrusted() -> None:
    served = {"price": 4000.0, "trusted": True, "fresh": True, "source": "binance_usdm_futures"}
    read = execution_market_payload(
        served,
        source="okx_swap_public",
        provider="okx_swap_public",
        environment="production",
        instrument_id="XAU-USDT-SWAP.OKX",
        symbol="XAU-USDT-SWAP",
    )
    assert read["trusted"] is False
    assert read["fresh"] is False
    assert read["raw_status"] == "blocked"
    assert read["reason"] == "execution_market_source_mismatch:binance_usdm_futures"

    same = execution_market_payload(
        {**served, "source": "okx_swap_public"},
        source="okx_swap_public",
        provider="okx_swap_public",
        environment="production",
        instrument_id="XAU-USDT-SWAP.OKX",
        symbol="XAU-USDT-SWAP",
    )
    assert same["trusted"] is True and same["raw_status"] == "ready"


# ---- instrument fields a venue does not have -------------------------------


def test_okx_definition_with_declared_not_applicable_fields_is_valid() -> None:
    assert validate_instrument_definition(_okx_definition())["symbol"] == "XAU-USDT-SWAP"


def test_null_field_without_a_declaration_still_fails() -> None:
    definition = _okx_definition()
    del definition["not_applicable_fields"]
    with pytest.raises(ValueError, match="missing required fields: max_price, min_notional"):
        validate_instrument_definition(definition)


def test_zero_min_notional_still_fails_for_binance() -> None:
    definition = _binance_definition()
    definition["min_notional"] = "0"
    with pytest.raises(ValueError, match="min_notional must be positive"):
        validate_instrument_definition(definition)


@pytest.mark.parametrize("field", ["min_quantity", "price_increment", "margin_maint_rate", "upstream_server_time"])
def test_only_min_notional_and_max_price_may_be_declared_not_applicable(field: str) -> None:
    definition = _okx_definition()
    definition[field] = None
    definition["not_applicable_fields"] = {**definition["not_applicable_fields"], field: "venue does not say"}
    with pytest.raises(ValueError, match="cannot be declared not applicable"):
        validate_instrument_definition(definition)


def test_not_applicable_needs_a_reason_and_a_null_value() -> None:
    no_reason = _okx_definition()
    no_reason["not_applicable_fields"]["max_price"] = " "
    with pytest.raises(ValueError, match="needs a reason"):
        validate_instrument_definition(no_reason)

    has_value = _okx_definition()
    has_value["min_notional"] = "5"
    with pytest.raises(ValueError, match="must be null"):
        validate_instrument_definition(has_value)


def test_nautilus_builds_an_okx_instrument_without_min_notional_or_max_price() -> None:
    pytest.importorskip("nautilus_trader")
    from services.dualtrack_nautilus_instrument import build_nautilus_instrument

    instrument = build_nautilus_instrument(deepcopy(_okx_definition()), maker_fee_rate="0.0004", taker_fee_rate="0.0004")
    assert str(instrument.id) == "XAU-USDT-SWAP-PERP.OKX"
    assert str(instrument.price_increment) == "0.1"
    assert instrument.min_notional is None
    assert instrument.max_price is None


# ---- Dashboard venue profile -------------------------------------------------


def test_dashboard_offers_the_configured_paper_venue_only(tmp_path) -> None:
    from services.dashboard_control_plane import DashboardControlPlane

    catalog = DashboardControlPlane(tmp_path).catalog()
    paper = [row for row in catalog["venue_profiles"] if row["environment"] == "paper"]
    assert [row["id"] for row in paper] == ["okx.paper"]
    assert paper[0]["label"] == "OKX Paper"
    assert [row["instrument_id"] for row in paper[0]["instruments"]] == ["XAU-USDT-SWAP.OKX"]
    assert paper[0]["paper_gold_venue"]["fee_verified"] is False


def test_saved_binance_selection_is_shown_as_retired_not_remapped(tmp_path) -> None:
    from services.dashboard_control_plane import SELECTION_PATH, DashboardControlPlane
    from services.journal_store import write_json

    target = tmp_path / SELECTION_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    write_json(
        target,
        [
            {
                "venue_profile_id": "binance.paper",
                "instrument_id": "XAUUSDT.BINANCE",
                "strategy_family": "dca",
                "updated_at": "2026-10-01T00:00:00+00:00",
            }
        ],
    )
    status = DashboardControlPlane(tmp_path).status()
    assert status["venue_profile_id"] is None
    assert status["instrument_id"] is None
    assert status["retired_selection"]["venue_profile_id"] == "binance.paper"
    assert status["retired_selection"]["reason"] == "venue_profile_no_longer_offered"
