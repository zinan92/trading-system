from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from schemas.portfolio import PortfolioPolicy, PortfolioSnapshot
from services.testnet_candidate_selection import (
    CANDIDATE_SELECTION_SCHEMA,
    TestnetCandidateSelector,
)
from services.testnet_automation_coordinator import TestnetAutomationCoordinator


NOW = "2026-08-26T01:00:00+00:00"


def _snapshot(*, incoherent: bool = False) -> PortfolioSnapshot:
    return PortfolioSnapshot(
        snapshot_id="snapshot-btc",
        portfolio_session_id="portfolio-session-1",
        account_id="testnet-account",
        observed_at=NOW,
        equity="1000",
        available_cash="1000",
        coherent=not incoherent,
        fresh=True,
        provenance={
            "broker_id": "hyperliquid",
            "environment": "testnet",
            "account_fingerprint": "sha256:" + "b" * 64,
            "cursor": "account-cursor-1",
        },
    )


def _policy() -> PortfolioPolicy:
    return PortfolioPolicy(
        policy_id="testnet-policy",
        policy_revision="testnet-policy-v1",
        max_single_asset_aum_pct="30",
        max_active_assets=10,
        max_total_exposure_pct="100",
        max_margin_pct="100",
        max_leverage="5",
    )


def _candidate(
    asset: str,
    *,
    rank: int,
    requested_notional: str = "80",
    status: str = "active",
    delisted: bool = False,
    mapping_valid: bool = True,
    market: dict | None = None,
    direction: str = "long",
) -> dict:
    return {
        "candidate_id": f"candidate-{asset.lower()}",
        "asset": asset,
        "instrument_id": f"{asset}-USD-PERP",
        "rank": rank,
        "status": status,
        "delisted": delisted,
        "mapping_valid": mapping_valid,
        "price_tick": "1",
        "quantity_step": "0.001",
        "minimum_quantity": "0.001",
        "minimum_notional": "10",
        "strategy": {
            "direction": direction,
            "requested_quantity": "0.001",
            "requested_notional": requested_notional,
            "position_action": "open",
            "position_management": {"mode": "dca"},
            "protection_intent": {"mode": "position_following"},
        },
        "market": market
        or {
            "observed_at": NOW,
            "bid": "79999",
            "ask": "80001",
            "mid": "80000",
            "mark": "80000",
            "oracle": "80000",
            "impact": "80001",
            "depth_notional": "1000",
            "max_slippage": "5",
            "max_oracle_deviation_bps": "50",
            "source": "hyperliquid.external_testnet",
            "cursor": "market-cursor-1",
            "broker_id": "hyperliquid",
            "environment": "testnet",
            "instrument_id": f"{asset}-USD-PERP",
        },
    }


def test_selector_keeps_full_inventory_and_selects_first_eligible_candidate() -> None:
    result = TestnetCandidateSelector().select(
        [_candidate("ETH", rank=1, delisted=True), _candidate("BTC", rank=2)],
        strategy_family="dca",
        strategy_session_id="session-btc-dca",
        strategy_revision_id="revision-btc-dca-1",
        snapshot=_snapshot(),
        policy=_policy(),
        now=NOW,
    )

    assert result.schema_version == CANDIDATE_SELECTION_SCHEMA
    assert result.status == "selected"
    assert result.selected_asset == "BTC"
    assert result.selected_instrument_id == "BTC-USD-PERP"
    assert [row["asset"] for row in result.inventory] == ["ETH", "BTC"]
    assert result.inventory[0]["status"] == "blocked"
    assert "delisted" in result.inventory[0]["blockers"]
    assert result.candidate_set.candidates[0].asset == "BTC"


def test_selector_blocks_thin_or_stale_pair_without_global_hold() -> None:
    thin = _candidate("BTC", rank=1, market={
        "observed_at": "2026-08-26T00:56:00+00:00",
        "bid": "79999",
        "ask": "80001",
        "mid": "80000",
        "mark": "80000",
        "oracle": "80000",
        "impact": "80001",
        "depth_notional": "5",
        "max_slippage": "5",
        "max_oracle_deviation_bps": "50",
        "source": "hyperliquid.external_testnet",
        "cursor": "market-cursor-thin",
        "broker_id": "hyperliquid",
        "environment": "testnet",
        "instrument_id": "BTC-USD-PERP",
    })
    stale = _candidate("ETH", rank=2, market={
        **_candidate("ETH", rank=2)["market"],
        "observed_at": "2026-08-25T23:00:00+00:00",
    })

    result = TestnetCandidateSelector().select(
        [thin, stale],
        strategy_family="dca",
        strategy_session_id="session-btc-dca",
        strategy_revision_id="revision-btc-dca-1",
        snapshot=_snapshot(),
        policy=_policy(),
        now=NOW,
    )

    assert result.status == "blocked"
    assert result.selected_asset is None
    assert result.portfolio_result is not None
    assert {reason for row in result.inventory for reason in row["blockers"]} >= {
        "insufficient_depth",
        "market_stale",
    }
    assert result.global_hold is False


def test_selector_applies_portfolio_gate_downward_only_and_preserves_candidate_provenance() -> None:
    result = TestnetCandidateSelector().select(
        [_candidate("BTC", rank=1, requested_notional="400")],
        strategy_family="grid",
        strategy_session_id="session-btc-grid",
        strategy_revision_id="revision-btc-grid-1",
        snapshot=_snapshot(),
        policy=PortfolioPolicy(
            policy_id="tight-policy",
            policy_revision="tight-v1",
            max_single_asset_aum_pct="30",
            max_active_assets=10,
        ),
        now=NOW,
    )

    assert result.status == "selected"
    allocation = result.portfolio_result.selected_allocations[0]
    assert allocation.requested_notional == Decimal("400")
    assert allocation.effective_notional == Decimal("300")
    assert allocation.effective_notional < allocation.requested_notional
    assert allocation.execution_slice_id.startswith("execution-")
    assert allocation.status == "scaled"
    assert allocation.source_strategy_plan_digest.startswith("sha256:")
    assert result.read_model["status"] == "scaled"


def test_selector_returns_portfolio_hold_for_incoherent_shared_account() -> None:
    result = TestnetCandidateSelector().select(
        [_candidate("BTC", rank=1)],
        strategy_family="dca",
        strategy_session_id="session-btc-dca",
        strategy_revision_id="revision-btc-dca-1",
        snapshot=_snapshot(incoherent=True),
        policy=_policy(),
        now=NOW,
    )

    assert result.status == "held"
    assert result.global_hold is True
    assert result.portfolio_result.reason_code == "snapshot_not_coherent"
    assert result.read_model["status"] == "held"


def test_coordinator_persists_selection_and_keeps_execution_disabled(tmp_path: Path) -> None:
    coordinator = TestnetAutomationCoordinator(
        tmp_path / "outputs",
        clock=lambda: NOW,
    )
    coordinator.activate(
        {
            "strategy_family": "dca",
            "strategy_session_id": "session-btc-dca",
            "strategy_revision_id": "revision-btc-dca-1",
            "plan_digest": "sha256:" + "a" * 64,
            "account_fingerprint": "sha256:" + "b" * 64,
            "broker_id": "hyperliquid",
            "environment": "testnet",
            "transport_profile": "hyperliquid-testnet-default",
            "instrument_id": "BTC-USD-PERP",
            "runtime_id": "runtime-1",
            "release_sha": "c" * 40,
            "capability_revision": "hyperliquid-testnet-runtime-v1",
        },
        command_id="activate-1",
    )

    result = coordinator.command(
        "select_candidate",
        {
            "candidates": [_candidate("ETH", rank=2, delisted=True), _candidate("BTC", rank=1)],
            "snapshot": _snapshot(),
            "policy": _policy(),
        },
        command_id="select-1",
    )

    assert result["event"] == "candidate_selected"
    assert result["status"] == "candidate_selected"
    assert result["selected_asset"] == "BTC"
    assert result["execution_enabled"] is False
    assert result["execution_mutation"] is False
    assert result["candidate_selection"]["selected_asset"] == "BTC"
    assert coordinator.status()["selected_instrument_id"] == "BTC-USD-PERP"
