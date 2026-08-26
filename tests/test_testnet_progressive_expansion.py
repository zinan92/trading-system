from __future__ import annotations

from pathlib import Path

import pytest

from services.testnet_automation_coordinator import TestnetAutomationCoordinator
from services.testnet_progressive_expansion import (
    EXPANSION_SCHEMA,
    TestnetProgressiveExpansion,
    TestnetProgressiveExpansionError,
    bounded_canary_caps,
)


def _candidate(asset: str, rank: int) -> dict:
    return {
        "candidate_id": f"candidate-{asset.lower()}",
        "asset": asset,
        "instrument_id": f"{asset}-USD-PERP",
        "rank": rank,
    }


def test_pair_blocker_falls_through_before_any_canary_side_effect() -> None:
    calls: list[str] = []
    expansion = TestnetProgressiveExpansion()

    result = expansion.admit_ranked(
        [_candidate("ETH", 1), _candidate("BTC", 2)],
        preflight=lambda candidate: (
            {"status": "blocked", "reason": "thin_book"}
            if candidate["asset"] == "ETH"
            else {"status": "pass", "instrument_id": "BTC-USD-PERP"}
        ),
        canary=lambda candidate, caps: calls.append(candidate["asset"]) or {"status": "pass", "asset": candidate["asset"], "caps": caps},
        equity="1000",
    )

    assert result["schema_version"] == EXPANSION_SCHEMA
    assert result["status"] == "admitted"
    assert result["selected_asset"] == "BTC"
    assert calls == ["BTC"]
    assert result["inventory"][0]["scope"] == "pair"
    assert result["inventory"][0]["status"] == "blocked"
    assert result["caps"] == bounded_canary_caps("1000")


def test_global_unknown_creates_hold_and_never_falls_through() -> None:
    calls: list[str] = []
    result = TestnetProgressiveExpansion().admit_ranked(
        [_candidate("ETH", 1), _candidate("BTC", 2)],
        preflight=lambda _candidate: {"status": "blocked", "reason": "account_unknown", "scope": "account"},
        canary=lambda candidate, _caps: calls.append(candidate["asset"]) or {"status": "pass"},
        equity="1000",
    )

    assert result["status"] == "portfolio_hold"
    assert result["global_hold"] is True
    assert result["blocker"] == "account_unknown"
    assert calls == []


def test_unknown_canary_is_pair_local_by_default_but_account_scope_holds() -> None:
    local = TestnetProgressiveExpansion().admit(
        _candidate("BTC", 1),
        preflight=lambda _candidate: {"status": "pass"},
        canary=lambda _candidate, _caps: {"status": "unknown", "reason": "submit_unknown"},
        equity="1000",
    )
    global_result = TestnetProgressiveExpansion().admit(
        _candidate("BTC", 1),
        preflight=lambda _candidate: {"status": "pass"},
        canary=lambda _candidate, _caps: {"status": "unknown", "reason": "account_cursor_unknown", "scope": "account"},
        equity="1000",
    )

    assert local["status"] == "pair_blocked"
    assert local["global_hold"] is False
    assert global_result["status"] == "portfolio_hold"
    assert global_result["global_hold"] is True


def test_asset_switch_after_side_effect_is_forbidden() -> None:
    expansion = TestnetProgressiveExpansion()
    first = expansion.admit(
        _candidate("BTC", 1),
        preflight=lambda _candidate: {"status": "pass"},
        canary=lambda _candidate, _caps: {"status": "pass"},
        equity="1000",
    )
    assert first["status"] == "admitted"

    with pytest.raises(TestnetProgressiveExpansionError, match="asset_switch_forbidden"):
        expansion.admit(
            _candidate("ETH", 2),
            preflight=lambda _candidate: {"status": "pass"},
            canary=lambda _candidate, _caps: {"status": "pass"},
            equity="1000",
            side_effect_started=True,
            selected_asset="BTC",
        )


def test_coordinator_persists_progressive_expansion_result(tmp_path: Path) -> None:
    coordinator = TestnetAutomationCoordinator(tmp_path / "outputs", clock=lambda: "2026-08-26T01:00:00+00:00")
    coordinator.activate(
        {
            "strategy_family": "dca",
            "strategy_session_id": "session-expand",
            "strategy_revision_id": "revision-expand-1",
            "plan_digest": "sha256:" + "a" * 64,
            "account_fingerprint": "sha256:" + "b" * 64,
            "broker_id": "hyperliquid",
            "environment": "testnet",
            "transport_profile": "hyperliquid-testnet-default",
            "instrument_id": "BTC-USD-PERP",
            "runtime_id": "runtime-expand",
            "release_sha": "c" * 40,
            "capability_revision": "hyperliquid-testnet-runtime-v1",
        },
        command_id="activate-expand",
    )

    result = coordinator.progressive_expand(
        [_candidate("BTC", 1)],
        preflight=lambda _candidate: {"status": "pass"},
        canary=lambda candidate, _caps: {"status": "pass", "asset": candidate["asset"]},
        equity="1000",
        command_id="expand-1",
    )

    assert result["event"] == "progressive_expansion_admitted"
    assert result["status"] == "candidate_admitted"
    assert result["selected_asset"] == "BTC"
    assert coordinator.status()["expansion"]["status"] == "admitted"
