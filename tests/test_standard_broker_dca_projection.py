from __future__ import annotations

from copy import deepcopy

import pytest

from services.dca_plan import (
    build_dca_preview,
    build_dca_strategy_plan,
    dca_strategy_plan_digest,
)
from services.dualtrack_config import DEFAULT_DUALTRACK_CONFIG
from services.standard_broker_dca_projection import (
    DcaProjectionError,
    project_canonical_dca_plan,
)


def _market(
    *,
    provider: str = "hyperliquid.external_testnet",
    symbol: str = "PAXG-USD-PERP",
) -> dict:
    price = 4_010.0
    return {
        "status": "ready",
        "fresh": True,
        "is_synthetic": False,
        "provider": provider,
        "symbol": symbol,
        "timeframe": "1m",
        "latest_close": price,
        "latest_timestamp": "2026-07-22T00:19:00+00:00",
        "bars": [
            {
                "timestamp": f"2026-07-22T00:{index:02d}:00+00:00",
                "open": price,
                "high": price + 1,
                "low": price - 1,
                "close": price,
            }
            for index in range(20)
        ],
    }


def _strategy_plan(
    *,
    direction: str = "long",
    provider: str = "hyperliquid.external_testnet",
    symbol: str = "PAXG-USD-PERP",
) -> dict:
    config = deepcopy(DEFAULT_DUALTRACK_CONFIG)
    config["execution_contract"] = {
        **config["execution_contract"],
        "price_increment": "0.01",
        "quantity_increment": "0.001",
    }
    preview = build_dca_preview(
        "2026-07-22_NIGHT",
        {
            "direction": direction,
            "dca": {
                "entry_levels": (
                    [4_004.0, 3_996.0, 3_988.0]
                    if direction == "long"
                    else [4_016.0, 4_024.0, 4_032.0]
                ),
                "target_price": 4_050.0 if direction == "long" else 3_970.0,
                "stop_price": 3_970.0 if direction == "long" else 4_050.0,
                "notional_per_addition": 500.0,
                "max_additions": 3,
                "loop_enabled": False,
            },
            "risk_budget": {"leverage": 10},
        },
        market=_market(provider=provider, symbol=symbol),
        account={"equity": 10_000},
        config=config,
    )
    return build_dca_strategy_plan(
        preview,
        strategy_plan_id="strategy-plan-dca-parity",
        version=7,
        locked_at="2026-07-22T16:00:00+00:00",
    )


def _binding() -> dict[str, object]:
    return {
        "plan_id": "hl-dca-parity-1",
        "broker_id": "hyperliquid",
        "environment": "testnet",
        "profile_id": "hyperliquid-testnet-position-protection",
        "account_fingerprint": "sha256:" + "a" * 64,
        "runtime_id": "hl-runtime-1",
        "release_sha": "b" * 40,
        "capability_revision": "hyperliquid-testnet-position-protection-runtime-v1",
        "instrument_id": "PAXG-USD-PERP",
        "contract_multiplier": "1",
        "quantity_step": "0.001",
        "price_tick": "0.001",
        "max_slippage": "5",
        "max_notional": "1600",
        "max_leverage": "10",
        "account_equity": "10000",
        "max_open_orders": 3,
        "max_open_positions": 1,
        "fee_budget_usd": "1",
        "max_loss_usd": "50",
        "time_in_force": "gtc",
        "expires_at": "2099-01-01T00:00:00+00:00",
        "close_price": "4050",
        "market_source": {
            "source_id": "hyperliquid.external_testnet",
            "broker_id": "hyperliquid",
            "environment": "testnet",
            "instrument_id": "PAXG-USD-PERP",
            "execution_venue": True,
        },
    }


def test_projection_preserves_canonical_dca_and_maps_fixed_notional_to_external_precision() -> None:
    source = _strategy_plan()
    projection = project_canonical_dca_plan(source, binding=_binding())

    mapped = projection.plan
    assert projection.source_strategy_plan_id == "strategy-plan-dca-parity"
    assert projection.source_strategy_plan_digest == source["plan_digest"]
    assert projection.source_market == {
        "provider": "hyperliquid.external_testnet",
        "symbol": "PAXG-USD-PERP",
        "timeframe": "1m",
    }
    assert projection.execution_market_source.source_id == "hyperliquid.external_testnet"
    assert projection.canonical_semantics["max_additions"] == 3
    assert projection.canonical_semantics["loop_enabled"] is False
    assert projection.canonical_semantics["aggregate_take_profit"]["side"] == "sell"
    assert mapped["plan_id"] == "hl-dca-parity-1"
    assert mapped["plan_version"] == 7
    assert mapped["instrument_id"] == "PAXG-USD-PERP"
    assert mapped["entry_levels"] == ["4004.0", "3996.0", "3988.0"]
    assert mapped["entry_quantities"] == ["0.124", "0.125", "0.125"]
    assert mapped["target_price"] == "4050.0"
    assert mapped["stop_price"] == "3970.0"
    assert mapped["close_price"] == "4050"
    assert source["dca"]["notional_per_addition"] == 500.0
    assert source["dca"]["loop_enabled"] is False


def test_projection_preserves_short_dca_direction_and_aggregate_exit() -> None:
    source = _strategy_plan(direction="short")
    binding = {**_binding(), "close_price": "3970"}

    projection = project_canonical_dca_plan(source, binding=binding)

    assert projection.plan["direction"] == "short"
    assert projection.plan["entry_levels"] == ["4016.0", "4024.0", "4032.0"]
    assert projection.plan["target_price"] == "3970.0"
    assert projection.plan["stop_price"] == "4050.0"
    assert projection.canonical_semantics["aggregate_take_profit"]["side"] == "buy"


def test_projection_rejects_non_dca_or_missing_canonical_plan_identity() -> None:
    source = _strategy_plan()
    with pytest.raises(DcaProjectionError, match="strategy_plan_type_invalid"):
        project_canonical_dca_plan({**source, "strategy_type": "grid"}, binding=_binding())

    malformed = dict(source)
    malformed.pop("plan_digest")
    with pytest.raises(DcaProjectionError, match="strategy_plan_digest_missing"):
        project_canonical_dca_plan(malformed, binding=_binding())

    tampered = {**source, "dca": {**source["dca"], "stop_price": 3900.0}}
    with pytest.raises(DcaProjectionError, match="strategy_plan_digest_mismatch"):
        project_canonical_dca_plan(tampered, binding=_binding())


def test_projection_rejects_price_not_aligned_to_selected_instrument() -> None:
    source = _strategy_plan()
    source["dca"] = {
        **source["dca"],
        "entries": [
            {**source["dca"]["entries"][0], "price": 4004.0005},
            *source["dca"]["entries"][1:],
        ],
    }
    source["plan_digest"] = dca_strategy_plan_digest(source)

    with pytest.raises(DcaProjectionError, match="canonical_price_precision_mismatch"):
        project_canonical_dca_plan(source, binding=_binding())


def test_projection_rejects_market_source_from_another_binding() -> None:
    binding = {
        **_binding(),
        "market_source": {
            "source_id": "binance_usdm_futures",
            "broker_id": "binance",
            "environment": "demo",
            "instrument_id": "XAUUSDT",
            "execution_venue": True,
        },
    }

    with pytest.raises(DcaProjectionError, match="market_source_binding_mismatch"):
        project_canonical_dca_plan(_strategy_plan(), binding=binding)


def test_projection_rejects_strategy_market_from_another_venue() -> None:
    source = _strategy_plan(provider="binance_usdm_futures", symbol="GOLD")

    with pytest.raises(DcaProjectionError, match="strategy_market_binding_mismatch"):
        project_canonical_dca_plan(source, binding=_binding())


def test_projection_uses_canonical_notional_when_source_rounding_is_lower() -> None:
    source = _strategy_plan()
    source["dca"] = {
        **source["dca"],
        "entries": [
            {**source["dca"]["entries"][0], "notional": 490.0},
            *source["dca"]["entries"][1:],
        ],
    }
    source["plan_digest"] = dca_strategy_plan_digest(source)
    binding = {**_binding(), "quantity_step": "0.0001"}

    projection = project_canonical_dca_plan(source, binding=binding)

    assert projection.plan["entry_quantities"][0] == "0.1248"
