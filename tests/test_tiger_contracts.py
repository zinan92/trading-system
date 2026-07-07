from __future__ import annotations

from services.tiger_contracts import TigerContractResolver


def _config() -> dict:
    return {
        "exchange": "COMEX",
        "execution_contract_map": {"MGCmain": "MGC2608", "MGC": "MGC2608"},
        "rollover_policy": {"rollover_days_before_contract_month": 14},
        "contract_specs": {
            "MGC2608": {
                "symbol": "MGC",
                "currency": "USD",
                "exchange": "COMEX",
                "contract_month": "202608",
                "multiplier": 10,
                "local_symbol": "MGC2608",
            }
        },
    }


def test_tiger_contract_resolver_maps_continuous_symbol_to_dated_execution_contract():
    result = TigerContractResolver(_config()).resolve("MGCmain", as_of="2026-07-05T00:00:00+00:00")

    assert result.ready is True
    assert result.requested_symbol == "MGCmain"
    assert result.execution_symbol == "MGC2608"
    assert result.continuous_input is True
    assert result.mapped is True
    assert result.days_to_contract_month == 27
    assert result.contract_spec["contract_month"] == "202608"


def test_tiger_contract_resolver_blocks_inside_rollover_window():
    result = TigerContractResolver(_config()).resolve("MGC2608", as_of="2026-07-25T00:00:00+00:00")

    assert result.ready is False
    assert result.status == "rollover_required"
    assert result.days_to_contract_month == 7
    assert "inside rollover window" in result.block_reason


def test_tiger_contract_resolver_blocks_missing_dated_contract_spec():
    result = TigerContractResolver(_config()).resolve("1OZmain", as_of="2026-07-05T00:00:00+00:00")

    assert result.ready is False
    assert result.status == "blocked"
    assert "contract spec is missing" in result.block_reason
