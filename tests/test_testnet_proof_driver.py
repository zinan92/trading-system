from __future__ import annotations

import json
from pathlib import Path

import pytest

from pipelines.testnet_proof_driver import (
    ProofDriverError,
    _run_with_market_retry,
    build_market_document,
    build_plan,
    map_confirmation,
    read_coherent_market,
    validate_grid_plan,
)
from pipelines import testnet_automation_proof as proof
from pipelines.testnet_automation_proof import _MARKET_REQUIRED
from services.park_confirmation_ledger import DurableParkConfirmationError, parse_durable_confirmation
from services.grid_testnet_lifecycle import GridTestnetLifecycle
from services.testnet_plan_builder import build_plan as public_build_plan


def _preview() -> tuple[dict, dict]:
    digest = "sha256:" + "a" * 64
    confirmation = {
        "status": "confirmed",
        "acknowledged": True,
        "operator_id": "park",
        "activation_id": "sha256:" + "b" * 64,
        "preview_digest": digest,
        "plan_digest": digest,
        "strategy_family": "grid",
        "instrument_id": "BTC-USD-PERP",
        "strategy_session_id": "dashboard-session:a",
        "strategy_revision_id": "dashboard-revision:a",
        "confirmation_id": "dashboard-confirmation:a",
        "confirmation_digest": "sha256:" + "d" * 64,
        "confirmed_at": "2026-09-08T01:00:00+00:00",
    }
    preview = {
        "preview_digest": digest,
        "strategy_family": "grid",
        "instrument_id": "BTC-USD-PERP",
        "minimum_notional": "10",
        "quantity_step": "1",
        "price_tick": "1",
        "minimum_quantity": "1",
        "preview": {
            "cycle_id": "2026-09-08_DAY",
            "direction": "long",
            "market": {"timestamp": "2026-09-08T01:00:00+00:00"},
            "range": {"low": 90, "high": 110},
            "grid": {"hard_stop": 80},
            "orders": [
                {"level": 0, "side": "buy", "price": 90, "quantity": "1", "tp": 95},
                {"level": 1, "side": "buy", "price": 95, "quantity": "1", "tp": 100},
            ],
        },
    }
    # The helper is tested with a digest token representing a persisted row;
    # digest recomputation is covered by the Dashboard contract suite.
    return preview, confirmation


def test_plan_uses_preview_orders_and_keeps_plan_digest() -> None:
    preview, confirmation = _preview()
    plan = build_plan(preview, confirmation)

    assert plan["plan_digest"] == preview["preview_digest"]
    assert plan["grid"]["rungs"] == [
        {"rung": 0, "side": "buy", "price": 90, "quantity": "1", "tp": 95, "hard_stop": 80},
        {"rung": 1, "side": "buy", "price": 95, "quantity": "1", "tp": 100, "hard_stop": 80},
    ]
    assert plan["cycle_id"] == "2026-09-08_DAY"
    assert plan["execution_context"]["cycle_id"] == plan["cycle_id"]
    identity = GridTestnetLifecycle._identity(plan)
    assert identity["cycle_id"] == "2026-09-08_DAY"


def test_public_plan_builder_is_the_same_dashboard_projection() -> None:
    preview, confirmation = _preview()
    assert public_build_plan(preview, confirmation) == build_plan(preview, confirmation)


def test_plan_projects_all_dashboard_grid_risk_sources_for_lifecycle() -> None:
    preview, confirmation = _preview()
    preview["preview"]["range"]["low"] = 80
    preview["account"] = {"equity": 10_000}
    preview["risk"] = {
        "maximum_loss_at_full_depth": 100,
        "selected_leverage": 5,
        "max_slippage": 10,
    }
    preview["risk_gate"] = {"effective_notional": 1_000}
    preview["preview"]["grid"].update({"max_open_orders": 4, "max_open_positions": 2})
    plan = build_plan(preview, confirmation)

    assert plan["risk_budget"] == {
        "maximum_loss_at_full_depth": 25.0,
        "equity": 10_000,
        "leverage_limit": 5,
        "max_notional": 1_000,
        "max_open_orders": 4,
        "max_open_positions": 2,
        "max_slippage": 10,
    }


def test_plan_projects_catalog_and_standard_broker_instrument_constraints() -> None:
    preview, confirmation = _preview()
    for key in ("minimum_notional", "quantity_step", "price_tick", "minimum_quantity"):
        preview.pop(key, None)
    preview["instrument"] = {
        "instrument_id": "BTC-USD-PERP",
        "asset": "BTC",
        "size_decimals": 5,
        "max_leverage": 40,
    }

    plan = build_plan(
        preview,
        confirmation,
        catalog_rows=[preview["instrument"]],
    )

    assert plan["execution_context"] == {
        "venue_profile_id": None,
        "broker_id": None,
        "environment": None,
        "transport_profile": None,
        "instrument_id": "BTC-USD-PERP",
        "runtime_id": None,
        "capability_revision": None,
        "price_tick": "0.1",
        "price_max_decimal_places": 1,
        "price_max_significant_digits": 5,
        "quantity_step": "0.00001",
        "minimum_quantity": "0.00001",
        "minimum_notional": "10",
        "cycle_id": "2026-09-08_DAY",
        "max_leverage": 40,
    }


def test_validate_grid_plan_reports_every_lifecycle_prewrite_check() -> None:
    preview, confirmation = _preview()
    preview["preview"]["range"]["low"] = 80
    preview["account"] = {"equity": 10_000}
    preview["risk"] = {
        "maximum_loss_at_full_depth": 100,
        "selected_leverage": 5,
        "max_slippage": 10,
    }
    preview["risk_gate"] = {"effective_notional": 1_000}
    preview["preview"]["grid"].update({"max_open_orders": 4, "max_open_positions": 2})
    checks = validate_grid_plan(build_plan(preview, confirmation))["checks"]

    assert all(check["passed"] for check in checks)
    assert {check["name"] for check in checks} >= {
        "maximum_loss_at_full_depth", "equity", "leverage_limit", "max_notional",
        "max_open_orders", "max_open_positions", "max_slippage",
        "grid_geometry", "full_depth_risk",
    }


def test_validate_grid_plan_fails_closed_with_missing_source() -> None:
    preview, confirmation = _preview()
    preview["preview"]["range"]["low"] = 80
    preview["account"] = {"equity": 10_000}
    preview["risk"] = {"maximum_loss_at_full_depth": 100, "selected_leverage": 5}
    preview["risk_gate"] = {"effective_notional": 1_000}

    with pytest.raises(ProofDriverError, match="lifecycle_preflight_missing") as error:
        validate_grid_plan(build_plan(preview, confirmation))

    assert error.value.details["fields"] == ["max_slippage"]


def test_validate_grid_plan_checks_effective_rungs_against_instrument_catalog() -> None:
    preview, confirmation = _preview()
    preview["price_tick"] = "1"
    preview["quantity_step"] = "0.1"
    preview["minimum_quantity"] = "0.1"
    preview["minimum_notional"] = "10"
    preview["preview"]["range"]["low"] = 80
    preview["account"] = {"equity": 10_000}
    preview["risk"] = {
        "maximum_loss_at_full_depth": 100,
        "selected_leverage": 5,
        "max_slippage": 10,
    }
    preview["risk_gate"] = {"effective_notional": 1_000}
    preview["preview"]["grid"].update({"max_open_orders": 4, "max_open_positions": 2})
    preview["preview"]["orders"][0]["quantity"] = "0.1"
    preview["preview"]["orders"][1]["quantity"] = "0.1"

    with pytest.raises(ProofDriverError, match="instrument_constraints_blocked") as error:
        validate_grid_plan(build_plan(preview, confirmation))

    violation = error.value.details["violations"][0]
    assert float(violation["notional"]) == 9.0
    assert "minimum_notional=10" in violation["reasons"][-1]
    assert "reduce grid count" in error.value.details["suggestion"]


def test_validate_grid_plan_rejects_hyperliquid_significant_digit_violation() -> None:
    preview, confirmation = _preview()
    preview["preview"]["range"] = {"low": 75000, "high": 76000}
    preview["preview"]["grid"]["hard_stop"] = 74000
    preview["preview"]["orders"] = [
        {"level": 0, "side": "buy", "price": 75688.6, "quantity": "0.00025", "tp": 75800},
        {"level": 1, "side": "buy", "price": 75500, "quantity": "0.00025", "tp": 75700},
    ]
    preview["preview"]["direction"] = "long"
    preview["account"] = {"equity": 10_000}
    preview["risk"] = {"maximum_loss_at_full_depth": 100, "selected_leverage": 5, "max_slippage": 10}
    preview["risk_gate"] = {"effective_notional": 1_000}
    preview["preview"]["grid"].update({"max_open_orders": 4, "max_open_positions": 2})
    preview["instrument"] = {"instrument_id": "BTC-USD-PERP", "asset": "BTC", "size_decimals": 5, "max_leverage": 40}

    with pytest.raises(ProofDriverError, match="instrument_constraints_blocked") as error:
        validate_grid_plan(build_plan(preview, confirmation, catalog_rows=[preview["instrument"]]))

    assert "5sig/1dp" in error.value.details["violations"][0]["reasons"][0]


def test_validate_grid_plan_accepts_effective_rungs_on_catalog_rules() -> None:
    preview, confirmation = _preview()
    preview.update({"price_tick": "1", "quantity_step": "0.1", "minimum_quantity": "0.1", "minimum_notional": "10"})
    preview["preview"]["range"]["low"] = 80
    preview["account"] = {"equity": 10_000}
    preview["risk"] = {"maximum_loss_at_full_depth": 100, "selected_leverage": 5, "max_slippage": 10}
    preview["risk_gate"] = {"effective_notional": 1_000}
    preview["preview"]["grid"].update({"max_open_orders": 4, "max_open_positions": 2})

    result = validate_grid_plan(build_plan(preview, confirmation))
    assert next(check for check in result["checks"] if check["name"] == "venue_constraints")["passed"] is True


def test_plan_requires_canonical_cycle_identity() -> None:
    preview, confirmation = _preview()
    del preview["preview"]["cycle_id"]

    with pytest.raises(ProofDriverError, match="dashboard_cycle_id_missing"):
        build_plan(preview, confirmation)


def test_market_document_combines_preview_facts_with_binding_price_and_identity() -> None:
    preview, _ = _preview()
    preview["market"] = {
        "execution_ready": True, "fresh": True, "is_synthetic": False,
        "fallback_policy": "none", "bid": "59999", "ask": "60001",
        "mark": "60000", "oracle": "60000", "impact": "60000.1",
        "depth_notional": "100000", "max_slippage": "10",
        "max_oracle_deviation_bps": "5", "cursor": "cursor-1",
        "broker_id": "hyperliquid", "environment": "testnet", "asset_index": 0,
        "mapping_revision": "mapping-v1", "universe_revision": "universe-v1",
        "connection_epoch": "epoch-1",
    }
    binding = {
        "instrument_id": "BTC-USD-PERP", "price": "60000", "freshness": "fresh",
        "observed_at": "2026-09-08T01:00:03+00:00",
        "source": "nautilus-hyperliquid.testnet", "mapping_revision": "mapping-v1",
    }

    market = build_market_document(preview, binding, instrument_id="BTC-USD-PERP")

    assert set(_MARKET_REQUIRED).issubset(market)
    assert market["mid"] == binding["price"]
    assert market["observed_at"] == binding["observed_at"]
    assert market["source"] == binding["source"]


def test_market_document_missing_dashboard_fact_fails_closed() -> None:
    preview, _ = _preview()
    preview["market"] = {"execution_ready": True, "fallback_policy": "none"}
    binding = {
        "instrument_id": "BTC-USD-PERP", "price": "60000", "freshness": "fresh",
        "observed_at": "2026-09-08T01:00:03+00:00",
        "source": "nautilus-hyperliquid.testnet",
    }

    with pytest.raises(ProofDriverError, match="market_facts_missing") as error:
        build_market_document(preview, binding, instrument_id="BTC-USD-PERP")

    assert "bid" in error.value.details["fields"]


class _MarketSequence:
    def __init__(self, values: list[dict]) -> None:
        self.values = iter(values)

    def market_fact(self, *, instrument_id: str, now: object) -> dict:
        return next(self.values)


def _complete_market(mid: str) -> dict:
    value = {
        "instrument_id": "BTC-USD-PERP", "price": mid, "freshness": "fresh",
        "observed_at": "2026-09-08T01:00:03+00:00", "source": "nautilus-hyperliquid.testnet",
        "broker_id": "hyperliquid", "environment": "testnet", "asset_index": 0,
        "mapping_revision": "mapping-v1", "universe_revision": "universe-v1",
        "connection_epoch": "epoch-1", "cursor": "cursor-1", "execution_ready": True,
        "fresh": True, "is_synthetic": False, "fallback_policy": "none",
        "bid": "59999", "ask": "60001", "mark": "60000", "oracle": "60000",
        "impact": "60000", "depth_notional": "100000", "max_slippage": "10",
        "max_oracle_deviation_bps": "5",
    }
    value["mid"] = mid
    return value


def test_market_bbo_outside_retries_then_succeeds() -> None:
    preview, _ = _preview()
    preview["market"] = {"fallback_policy": "none"}
    broker = _MarketSequence([_complete_market("59998"), _complete_market("60000")])
    sleeps: list[float] = []

    market, checks = read_coherent_market(preview, broker, instrument_id="BTC-USD-PERP", sleep_fn=sleeps.append)

    assert market["mid"] == "60000"
    assert [check["passed"] for check in checks] == [False, True]
    assert sleeps == [1.0]


def test_market_bbo_always_outside_fails_closed_after_five_attempts() -> None:
    preview, _ = _preview()
    preview["market"] = {"fallback_policy": "none"}
    broker = _MarketSequence([_complete_market("59998")] * 5)
    sleeps: list[float] = []

    with pytest.raises(ProofDriverError, match="market_bbo_inconsistent") as error:
        read_coherent_market(preview, broker, instrument_id="BTC-USD-PERP", sleep_fn=sleeps.append)

    assert len(error.value.details["attempts"]) == 5
    assert sleeps == [1.0] * 4


def test_market_price_mismatch_retries_then_succeeds_with_attempt_evidence() -> None:
    preview, _ = _preview()
    preview["market"] = {"fallback_policy": "none"}
    broker = _MarketSequence([
        {"price": "60000", "source": "binding", "observed_at": "now"},
        {"price": "60001", "source": "binding", "observed_at": "now"},
        {"price": "60000", "source": "binding", "observed_at": "now"},
    ])
    reader = _MarketSequence([
        {**_complete_market("60001"), "max_slippage": "0.1"},
        {**_complete_market("60000"), "max_slippage": "0.1"},
        {**_complete_market("60000"), "max_slippage": "0.1"},
    ])
    reader.read = lambda _instrument_id: next(reader.values)  # type: ignore[attr-defined]
    sleeps: list[float] = []

    market, checks = read_coherent_market(
        preview, broker, instrument_id="BTC-USD-PERP", sleep_fn=sleeps.append,
        market_reader=reader, read_reader_always=True,
    )

    assert market["mid"] == "60000"
    assert len(checks) == 3
    assert [(check["binding_price"], check["reader_price"]) for check in checks] == [
        ("60000", "60001"), ("60001", "60000"), ("60000", "60000"),
    ]
    assert sleeps == [1.0, 1.0]


def test_market_price_mismatch_fails_closed_after_attempt_limit() -> None:
    preview, _ = _preview()
    preview["market"] = {"fallback_policy": "none"}
    broker = _MarketSequence([
        {"price": "60000", "source": "binding", "observed_at": "now"},
    ] * 3)
    reader = _MarketSequence([{**_complete_market("60001"), "max_slippage": "0.1"}] * 3)
    reader.read = lambda _instrument_id: next(reader.values)  # type: ignore[attr-defined]
    sleeps: list[float] = []

    with pytest.raises(ProofDriverError, match="market_price_mismatch") as error:
        read_coherent_market(
            preview, broker, instrument_id="BTC-USD-PERP", sleep_fn=sleeps.append,
            market_reader=reader, max_attempts=3, read_reader_always=True,
        )

    assert len(error.value.details["attempts"]) == 3
    assert sleeps == [1.0, 1.0]


def test_market_reader_can_be_required_for_every_coherent_tick_read() -> None:
    preview, _ = _preview()
    preview["market"] = {"fallback_policy": "none"}
    broker = _MarketSequence([{"price": "60000", "source": "binding", "observed_at": "now"}])
    reader = _MarketSequence([_complete_market("60000")])
    reader.read = lambda _instrument_id: next(reader.values)  # type: ignore[attr-defined]

    market, checks = read_coherent_market(
        preview, broker, instrument_id="BTC-USD-PERP", market_reader=reader,
        read_reader_always=True,
    )

    assert market["mid"] == "60000"
    assert checks == [{"bid": "59999", "mid": "60000", "ask": "60001", "passed": True,
                      "binding_price": "60000", "reader_price": "60000", "attempt": 1}]


def test_driver_retries_price_and_observation_mismatch_then_records_success() -> None:
    outcomes = iter([
        proof.TestnetAutomationProofError("market_price_mismatch"),
        proof.TestnetAutomationProofError("market_observation_mismatch"),
        {"status": "candidate_selected"},
    ])
    refreshed = iter([({"mid": "60001"}, [{"attempt": 2}]), ({"mid": "60002"}, [{"attempt": 3}])])
    sleeps: list[float] = []
    written: list[dict] = []
    checks: list[dict] = []

    def run_attempt() -> dict:
        outcome = next(outcomes)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    result, attempts = _run_with_market_retry(
        run_attempt,
        lambda: next(refreshed), written.append, checks, sleep_fn=sleeps.append,
    )

    assert result == {"status": "candidate_selected"}
    assert attempts == [
        {"attempt": 1, "reason_code": "market_price_mismatch"},
        {"attempt": 2, "reason_code": "market_observation_mismatch"},
    ]
    assert sleeps == [2.0, 2.0]
    assert written == [{"mid": "60001"}, {"mid": "60002"}]
    assert checks == [{"attempt": 2}, {"attempt": 3}]


def test_driver_market_retry_fails_after_ten_attempts_with_attempt_evidence() -> None:
    def always_mismatch() -> dict:
        raise proof.TestnetAutomationProofError("market_price_mismatch")

    sleeps: list[float] = []
    checks: list[dict] = []
    with pytest.raises(proof.TestnetAutomationProofError) as error:
        _run_with_market_retry(
            always_mismatch,
            lambda: ({"mid": "60001"}, [{"attempt": 1}]),
            lambda _market: None,
            checks,
            sleep_fn=sleeps.append,
        )

    assert len(error.value.driver_retry_attempts) == 10
    assert all(item["reason_code"] == "market_price_mismatch" for item in error.value.driver_retry_attempts)
    assert sleeps == [2.0] * 9


def test_confirmation_mapping_rejects_dashboard_authorization_forgery(tmp_path: Path) -> None:
    _, dashboard = _preview()
    dashboard["execution_authorized"] = True
    path = tmp_path / "park_strategy" / "confirmations.jsonl"
    path.parent.mkdir()
    path.write_text(
        json.dumps({"event": "proposal", "proposal_id": "p", "plan_digest": dashboard["plan_digest"]})
        + "\n"
        + json.dumps({"event": "rejected", "proposal_id": "p", "plan_digest": dashboard["plan_digest"]}),
        encoding="utf-8",
    )

    with pytest.raises(ProofDriverError, match="durable_park_confirmation_missing"):
        map_confirmation(tmp_path, dashboard, approval_id="approval", approved_by="park")


def _dashboard_root(tmp_path: Path, dashboard: dict) -> None:
    control = tmp_path / "dashboard_control_plane"
    control.mkdir(parents=True)
    (control / "confirmations.json").write_text(json.dumps([dashboard]), encoding="utf-8")
    current = tmp_path / "testnet_automation"
    current.mkdir()
    (current / "current.json").write_text(
        json.dumps([{"activation_id": dashboard["activation_id"], "plan_digest": dashboard["plan_digest"]}]),
        encoding="utf-8",
    )


def test_dashboard_confirmation_maps_to_canonical_confirmation(tmp_path: Path) -> None:
    _, dashboard = _preview()
    _dashboard_root(tmp_path, dashboard)

    result = map_confirmation(tmp_path, dashboard, approval_id="approval", approved_by="park")

    assert result["proposal_id"] == dashboard["plan_digest"]
    assert result["receipt_digest"] == dashboard["confirmation_digest"]
    assert result["execution_authorized"] is True
    assert result["confirmation_source"] == "dashboard"


def test_historical_confirm_and_run_confirmation_without_acknowledged_maps(tmp_path: Path) -> None:
    _, dashboard = _preview()
    dashboard.pop("acknowledged")
    dashboard["schema_version"] = "dashboard-confirmation-v1"
    dashboard["event"] = "operator_confirmed"
    _dashboard_root(tmp_path, dashboard)

    result = map_confirmation(tmp_path, dashboard, approval_id="approval", approved_by="park")

    assert result["confirmation_source"] == "dashboard"
    assert result["operator_id"] == "park"


def test_historical_non_confirm_and_run_confirmation_without_acknowledged_is_blocked(tmp_path: Path) -> None:
    _, dashboard = _preview()
    dashboard.pop("acknowledged")
    dashboard["event"] = "operator_confirmed_legacy_import"
    _dashboard_root(tmp_path, dashboard)

    with pytest.raises(ProofDriverError, match="dashboard_confirmation_not_acknowledged"):
        map_confirmation(tmp_path, dashboard, approval_id="approval", approved_by="park")


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("operator_id", "jessie", "dashboard_operator_invalid"),
        ("acknowledged", False, "dashboard_confirmation_not_acknowledged"),
    ],
)
def test_dashboard_confirmation_rejects_identity_and_acknowledgement_failures(
    tmp_path: Path, field: str, value: object, reason: str
) -> None:
    _, dashboard = _preview()
    dashboard[field] = value
    _dashboard_root(tmp_path, dashboard)

    with pytest.raises(ProofDriverError, match=reason):
        map_confirmation(tmp_path, dashboard, approval_id="approval", approved_by="park")


def test_dashboard_parser_rejects_plan_mismatch(tmp_path: Path) -> None:
    _, dashboard = _preview()
    _dashboard_root(tmp_path, dashboard)
    confirmation = {
        "event": "confirmed", "execution_authorized": True, "execution_environment": "testnet",
        "plan_digest": dashboard["plan_digest"], "confirmation_id": dashboard["confirmation_id"],
        "proposal_id": dashboard["plan_digest"], "receipt_digest": dashboard["confirmation_digest"],
        "confirmed_at": "2026-09-08T01:00:00+00:00", "operator_id": "park",
        "activation_id": dashboard["activation_id"], "confirmation_source": "dashboard",
    }
    with pytest.raises(DurableParkConfirmationError, match="dashboard_plan_digest_mismatch"):
        parse_durable_confirmation(
            tmp_path, plan={"plan_digest": "sha256:" + "e" * 64}, confirmation=confirmation
        )


def test_dashboard_parser_rejects_activation_mismatch(tmp_path: Path) -> None:
    _, dashboard = _preview()
    _dashboard_root(tmp_path, dashboard)
    confirmation = {
        "event": "confirmed", "execution_authorized": True, "execution_environment": "testnet",
        "plan_digest": dashboard["plan_digest"], "confirmation_id": dashboard["confirmation_id"],
        "proposal_id": dashboard["plan_digest"], "receipt_digest": dashboard["confirmation_digest"],
        "confirmed_at": dashboard["confirmed_at"], "operator_id": "park",
        "activation_id": "sha256:" + "c" * 64, "confirmation_source": "dashboard",
    }
    with pytest.raises(DurableParkConfirmationError, match="activation_identity_mismatch"):
        parse_durable_confirmation(tmp_path, plan={"plan_digest": dashboard["plan_digest"]}, confirmation=confirmation)


def test_confirmation_mapping_requires_explicit_park_approval(tmp_path: Path) -> None:
    _, dashboard = _preview()
    with pytest.raises(ProofDriverError, match="park_approval_required"):
        map_confirmation(tmp_path, dashboard, approval_id="", approved_by="park")
