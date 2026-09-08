from __future__ import annotations

import json
from pathlib import Path

import pytest

from services.dashboard_control_plane import (
    DashboardControlPlane,
    canonical_preview_digest,
    canonical_risk_gate_digest,
)


def _market() -> dict:
    bars = [
        {"open": 100, "high": 102, "low": 98, "close": 100, "timestamp": f"2026-08-29T00:{index:02d}:00+00:00"}
        for index in range(20)
    ]
    return {
        "status": "ready",
        "fresh": True,
        "is_synthetic": False,
        "provider": "testnet-fixture",
        "latest_close": 100,
        "latest_timestamp": "2026-08-29T00:20:00+00:00",
        "bars": bars,
        "strategy_timeframes": {
            "1d": {"provider": "testnet-fixture", "is_synthetic": False, "bars": bars},
            "4h": {"provider": "testnet-fixture", "is_synthetic": False, "bars": bars},
        },
    }


def test_catalog_exposes_explicit_venue_profiles_without_live_environment() -> None:
    catalog = DashboardControlPlane(Path("/tmp/dashboard-control-test")).catalog()

    profiles = {row["id"]: row for row in catalog["venue_profiles"]}

    assert set(profiles) == {"binance.paper", "hyperliquid.testnet"}
    assert profiles["hyperliquid.testnet"]["broker_id"] == "hyperliquid"
    assert profiles["hyperliquid.testnet"]["environment"] == "testnet"
    assert all("live" not in str(row).lower() for row in catalog["venue_profiles"])
    assert catalog["safety"] == {
        "read_only": True,
        "credentials_exposed": False,
        "broker_calls": False,
        "orders_submitted": False,
    }


def test_control_plane_resolves_selected_testnet_bars_without_cross_venue_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services.hyperliquid_testnet_market_reader import HyperliquidTestnetMarketReader

    observed: list[dict] = []

    def read_bars(_reader, instrument_id, *, timeframe, limit, end):
        observed.append(
            {
                "instrument_id": instrument_id,
                "timeframe": timeframe,
                "limit": limit,
                "end": end,
            }
        )

        return {
            "provider": "hyperliquid",
            "instrument_id": instrument_id,
            "timeframe": timeframe,
            "bars": [{"timestamp": "2026-08-29T00:00:00+00:00", "close": 80000.0}],
            "trusted": True,
        }

    monkeypatch.setattr(HyperliquidTestnetMarketReader, "read_bars", read_bars)
    plane = DashboardControlPlane(tmp_path)

    result = plane.resolve_market_bars(
        venue_profile_id="hyperliquid.testnet",
        instrument_id="BTC-USD-PERP",
        timeframe="30m",
        limit=240,
        end=None,
    )

    assert result["provider"] == "hyperliquid"
    assert observed == [
        {
            "instrument_id": "BTC-USD-PERP",
            "timeframe": "30m",
            "limit": 240,
            "end": None,
        }
    ]
    with pytest.raises(ValueError, match="dashboard_market_venue_not_supported"):
        plane.resolve_market_bars(
            venue_profile_id="binance.paper",
            instrument_id="XAUUSDT.BINANCE",
            timeframe="30m",
            limit=240,
            end=None,
        )


def test_runtime_facts_include_trusted_strategy_history_for_preview(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services import hyperliquid_testnet_market_reader

    class Reader:
        def read(self, instrument_id):
            return {
                "provider": "hyperliquid",
                "source": "hyperliquid.external_testnet",
                "broker_id": "hyperliquid",
                "environment": "testnet",
                "instrument_id": instrument_id,
                "price": 100.0,
                "mid": 100.0,
                "bid": 99.0,
                "ask": 101.0,
                "mark": 100.0,
                "oracle": 100.0,
                "impact": 100.1,
                "depth_notional": 1000.0,
                "max_slippage": 50.0,
                "max_oracle_deviation_bps": 50.0,
                "asset_index": 0,
                "mapping_revision": "mapping-v1",
                "universe_revision": "universe-v1",
                "connection_epoch": "epoch-v1",
                "cursor": "cursor-v1",
                "source_cursor": "cursor-v1",
                "observed_at": "2026-08-31T00:00:00+00:00",
                "fresh": True,
                "trusted": True,
                "execution_ready": True,
                "is_synthetic": False,
            }

        def read_bars(self, instrument_id, *, timeframe, limit, end):
            return {
                "provider": "hyperliquid",
                "source": "hyperliquid.external_testnet",
                "instrument_id": instrument_id,
                "timeframe": timeframe,
                "bars": [
                    {
                        "timestamp": f"2026-08-31T00:{index:02d}:00+00:00",
                        "open": 100.0,
                        "high": 101.0,
                        "low": 99.0,
                        "close": 100.0,
                    }
                    for index in range(limit)
                ],
                "fresh": True,
                "is_synthetic": False,
            }

    monkeypatch.setattr(hyperliquid_testnet_market_reader, "HyperliquidTestnetMarketReader", Reader)
    plane = DashboardControlPlane(tmp_path)
    market, account = plane.resolve_runtime_facts(
        venue_profile_id="hyperliquid.testnet",
        instrument_id="BTC-USD-PERP",
    )

    assert account is None
    assert market is not None
    assert len(market["bars"]) == 240
    assert set(market["strategy_timeframes"]) == {"1m", "1d", "4h"}


def test_catalog_keeps_every_dynamic_perp_with_stable_eligibility() -> None:
    def loader(profile_id: str):
        assert profile_id == "hyperliquid.testnet"
        return [
            {
                "instrument_id": "BTC-USD-PERP",
                "asset": "BTC",
                "asset_index": 0,
                "size_decimals": 5,
                "max_leverage": 50,
                "eligibility": "eligible",
            },
            {
                "instrument_id": "ZEC-USD-PERP",
                "asset": "ZEC",
                "asset_index": 9,
                "size_decimals": 3,
                "max_leverage": 10,
                "eligibility": "blocked",
                "blockers": ["market_facts_pending"],
            },
        ]

    catalog = DashboardControlPlane(Path("/tmp/dashboard-control-test"), catalog_loader=loader).catalog()
    profile = next(row for row in catalog["venue_profiles"] if row["id"] == "hyperliquid.testnet")

    assert [row["instrument_id"] for row in profile["instruments"]] == [
        "BTC-USD-PERP",
        "ZEC-USD-PERP",
    ]
    assert profile["instruments"][1]["eligibility"] == "blocked"
    assert profile["instruments"][1]["blockers"] == ["market_facts_pending"]
    assert profile["catalog_revision"].startswith("sha256:")


def test_selecting_venue_and_instrument_clears_stale_downstream_selection(tmp_path: Path) -> None:
    plane = DashboardControlPlane(tmp_path, catalog_loader=lambda profile_id: (
        [{"instrument_id": "BTC-USD-PERP", "asset": "BTC", "eligibility": "eligible"}]
        if profile_id == "hyperliquid.testnet"
        else [{"instrument_id": "XAUUSDT.BINANCE", "asset": "XAU", "eligibility": "eligible"}]
    ))

    selected = plane.select(venue_profile_id="hyperliquid.testnet", instrument_id="BTC-USD-PERP")

    assert selected["venue_profile_id"] == "hyperliquid.testnet"
    assert selected["instrument_id"] == "BTC-USD-PERP"
    assert selected["strategy_family"] is None

    reset = plane.select(venue_profile_id="binance.paper")

    assert reset["venue_profile_id"] == "binance.paper"
    assert reset["instrument_id"] is None
    assert reset["strategy_family"] is None
    assert plane.status()["selection_digest"].startswith("sha256:")


def test_dashboard_catalog_builder_projects_public_profiles_and_instruments(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from pipelines import dashboard_server

    monkeypatch.setattr(
        dashboard_server,
        "public_catalog_loader",
        lambda profile_id: (
            [{"instrument_id": "BTC-USD-PERP", "asset": "BTC", "eligibility": "eligible"}]
            if profile_id == "hyperliquid.testnet"
            else []
        ),
    )

    result = dashboard_server.build_dashboard_control_catalog_response(output_root=tmp_path)

    profile = next(row for row in result["venue_profiles"] if row["id"] == "hyperliquid.testnet")
    assert profile["instruments"][0]["instrument_id"] == "BTC-USD-PERP"
    assert result["safety"]["orders_submitted"] is False


def test_dashboard_selection_builder_is_non_executing(tmp_path: Path) -> None:
    from pipelines import dashboard_server

    selected = dashboard_server.build_dashboard_control_selection_response(
        {
            "venue_profile_id": "hyperliquid.testnet",
            "instrument_id": "BTC-USD-PERP",
        },
        output_root=tmp_path,
        catalog_loader=lambda profile_id: (
            [{"instrument_id": "BTC-USD-PERP", "asset": "BTC", "eligibility": "eligible"}]
            if profile_id == "hyperliquid.testnet"
            else []
        ),
    )

    assert selected["selection"]["instrument_id"] == "BTC-USD-PERP"
    assert selected["safety"] == {
        "read_only": True,
        "credentials_exposed": False,
        "orders_submitted": False,
    }


def test_dashboard_v5_contains_the_venue_asset_strategy_track() -> None:
    html = (Path(__file__).resolve().parents[1] / "dashboard-gridmind.html").read_text(
        encoding="utf-8"
    )

    for element_id in (
        "dashboardControlCard",
        "dashboardVenueSelect",
        "dashboardInstrumentSelect",
        "dashboardStrategyChoices",
        "dashboardControlStatus",
        "dashboardAccountSummary",
        "dashboardPreviewButton",
        "dashboardConfirmButton",
        "dashboardRuntimeStatus",
    ):
        assert f'id="{element_id}"' in html
    assert "/api/dashboard-control/catalog" in html
    assert "/api/dashboard-control/selection" in html
    assert "/api/dashboard-control/preview" in html
    assert "/api/dashboard-control/confirm" in html
    assert "/api/dashboard-control/runtime-status" in html
    assert "/api/dashboard-control/market-bars" in html
    assert "dashboardMarketBinding" in html
    assert "applyPersistedDashboardSelection" in html
    assert "syncPersistedStrategyFamily" in html
    assert "Hyperliquid Testnet" not in html or "Venue Profile" in html


def test_dca_preview_uses_the_canonical_builder_and_exposes_derived_risk(tmp_path: Path) -> None:
    preview = DashboardControlPlane(tmp_path).preview(
        venue_profile_id="binance.paper",
        instrument_id="XAUUSDT.BINANCE",
        strategy_family="dca",
        strategy={
            "direction": "long",
            "dca": {
                "entry_prices": [98, 96],
                "count": 2,
                "notional_per_entry": 1_000,
                "take_profit": 105,
                "stop_loss": 90,
            },
            "risk_budget": {"leverage": 5},
        },
        market=_market(),
        account={"equity": 10_000},
    )

    assert preview["execution_ready"] is True
    assert preview["authorizing"] is False
    assert preview["preview"]["dca"]["entry_count"] == 2
    assert preview["preview"]["risk"]["estimated_margin_at_full_depth"] == 400.0
    assert preview["preview"]["risk"]["maximum_loss_at_full_depth"] > 0
    assert preview["preview_digest"].startswith("sha256:")


def test_testnet_preview_carries_non_secret_activation_identity_from_host(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HYPERLIQUID_TESTNET_ACCOUNT_ADDRESS", "0x" + "11" * 20)
    monkeypatch.setenv(
        "HYPERLIQUID_TESTNET_PROTECTION_PROFILE",
        "hyperliquid-testnet-position-protection",
    )
    monkeypatch.setenv("HYPERLIQUID_TESTNET_SECRET_FILE", "/opaque/testnet-secret")
    monkeypatch.setenv("HYPERLIQUID_TESTNET_RUNTIME_ID", "dashboard-testnet-runtime")
    monkeypatch.setenv("TRADING_ORCHESTRATOR_RELEASE_SHA", "a" * 40)

    account = {
        "broker_id": "hyperliquid",
        "environment": "testnet",
        "account_fingerprint": "sha256:" + "b" * 64,
        "source_cursor": "sha256:" + "c" * 64,
        "fresh": True,
        "coherent": True,
        "equity": 10_000,
        "positions": [],
        "open_orders": [],
        "capabilities": {"protection": True},
    }
    market = {
        **_market(),
        "provider": "hyperliquid",
        "source": "hyperliquid.external_testnet",
        "instrument_id": "BTC-USD-PERP",
        "environment": "testnet",
    }
    preview = DashboardControlPlane(tmp_path).preview(
        venue_profile_id="hyperliquid.testnet",
        instrument_id="BTC-USD-PERP",
        strategy_family="dca",
        strategy={
            "direction": "long",
            "dca": {
                "entry_prices": [98, 96],
                "count": 2,
                "notional_per_entry": 100,
                "take_profit": 105,
                "stop_loss": 90,
            },
        },
        market=market,
        account=account,
    )

    assert preview["runtime_id"] == "dashboard-testnet-runtime"
    assert preview["release_sha"] == "a" * 40
    assert preview["transport_profile"] == "hyperliquid-testnet-position-protection"
    assert "private_key" not in str(preview)


def test_grid_preview_preserves_grid_geometry_and_is_non_authorizing(tmp_path: Path) -> None:
    preview = DashboardControlPlane(tmp_path).preview(
        venue_profile_id="binance.paper",
        instrument_id="XAUUSDT.BINANCE",
        strategy_family="grid",
        strategy={
            "direction": "neutral",
            "style": "steady",
            "range": {"low": 80, "high": 120, "scope": "full"},
            "grid": {
                "mode": "arithmetic",
                "count": 4,
                "notional_per_grid": 1_000,
                "notional_mode": "manual",
            },
        },
        market=_market(),
        account={"equity": 10_000},
    )

    assert preview["execution_ready"] is True
    assert preview["authorizing"] is False
    assert preview["preview"]["grid"]["count"] == 4
    assert preview["preview"]["grid"]["levels"]
    assert preview["preview"]["risk"]["estimated_margin"] > 0


def test_preview_digest_changes_when_strategy_configuration_changes(tmp_path: Path) -> None:
    plane = DashboardControlPlane(tmp_path)
    base = {
        "venue_profile_id": "binance.paper",
        "instrument_id": "XAUUSDT.BINANCE",
        "strategy_family": "dca",
        "strategy": {
            "direction": "short",
            "dca": {
                "entry_prices": [102, 104],
                "count": 2,
                "notional_per_entry": 1_000,
                "take_profit": 95,
                "stop_loss": 110,
            },
        },
        "market": _market(),
        "account": {"equity": 10_000},
    }

    first = plane.preview(**base)
    second = plane.preview(**{**base, "strategy": {**base["strategy"], "dca": {**base["strategy"]["dca"], "stop_loss": 112}}})

    assert first["preview_digest"] != second["preview_digest"]


def test_dashboard_preview_builder_keeps_execution_non_authorizing(monkeypatch, tmp_path: Path) -> None:
    from pipelines import dashboard_server

    result = dashboard_server.build_dashboard_control_preview_response(
        {
            "venue_profile_id": "binance.paper",
            "instrument_id": "XAUUSDT.BINANCE",
            "strategy_family": "dca",
            "strategy": {
                "direction": "long",
                "dca": {
                    "entry_prices": [98, 96],
                    "count": 2,
                    "notional_per_entry": 1_000,
                    "take_profit": 105,
                    "stop_loss": 90,
                },
            },
        },
        output_root=tmp_path,
        market=_market(),
        account={"equity": 10_000},
    )

    assert result["preview"]["authorizing"] is False
    assert result["safety"]["orders_submitted"] is False


def test_dashboard_preview_builder_does_not_trust_caller_supplied_testnet_facts(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from pipelines import dashboard_server

    monkeypatch.setattr(
        DashboardControlPlane,
        "resolve_runtime_facts",
        lambda self, *, venue_profile_id, instrument_id: (None, None),
    )
    result = dashboard_server.build_dashboard_control_preview_response(
        {
            "venue_profile_id": "hyperliquid.testnet",
            "instrument_id": "BTC-USD-PERP",
            "strategy_family": "dca",
            "strategy": {"direction": "long", "dca": {}},
            # These fields are intentionally ignored by the server boundary.
            "market": {"fresh": True, "execution_ready": True, "equity": 1_000},
            "account": {"fresh": True, "coherent": True, "equity": 1_000},
        },
        output_root=tmp_path,
    )

    assert result["preview"]["execution_ready"] is False
    assert "market_facts_required" in result["preview"]["blockers"]


def test_testnet_account_admission_is_identity_bound_and_requires_clean_state(tmp_path: Path) -> None:
    class Reader:
        def read(self, instrument_id=None):
            return {
                "broker_id": "hyperliquid",
                "environment": "testnet",
                "instrument_id": instrument_id,
                "account_fingerprint": "sha256:" + "a" * 64,
                "equity": 995.46,
                "positions": [],
                "open_orders": [],
                "fills": [],
                "fees": [],
                "fresh": True,
                "coherent": True,
                "source_cursor": "sha256:" + "b" * 64,
                "capabilities": {"protection": True},
            }

    result = DashboardControlPlane(tmp_path).account_admission(
        venue_profile_id="hyperliquid.testnet",
        instrument_id="BTC-USD-PERP",
        account_reader=Reader(),
    )

    assert result["ready"] is True
    assert result["clean_state"] is True
    assert result["account"]["account_fingerprint"].startswith("sha256:")
    assert result["safety"]["credentials_exposed"] is False


def test_testnet_account_admission_blocks_open_orders_and_unknown_reader(tmp_path: Path) -> None:
    class Reader:
        def read(self, instrument_id=None):
            return {
                "broker_id": "hyperliquid",
                "environment": "testnet",
                "instrument_id": instrument_id,
                "account_fingerprint": "sha256:" + "a" * 64,
                "equity": 995.46,
                "positions": [{"instrument_id": instrument_id, "signed_quantity": "0.01"}],
                "open_orders": [{"instrument_id": instrument_id, "oid": 7}],
                "fills": [],
                "fees": [],
                "fresh": True,
                "coherent": True,
                "source_cursor": "sha256:" + "b" * 64,
                "capabilities": {"protection": True},
            }

    result = DashboardControlPlane(tmp_path).account_admission(
        venue_profile_id="hyperliquid.testnet",
        instrument_id="BTC-USD-PERP",
        account_reader=Reader(),
    )

    assert result["ready"] is False
    assert result["clean_state"] is False
    assert "account_not_clean" in result["blockers"]


def test_dashboard_account_admission_builder_never_exposes_account_address(tmp_path: Path) -> None:
    from pipelines import dashboard_server

    class Reader:
        def read(self, instrument_id=None):
            return {
                "broker_id": "hyperliquid",
                "environment": "testnet",
                "instrument_id": instrument_id,
                "account_fingerprint": "sha256:" + "a" * 64,
                "equity": 995.46,
                "positions": [],
                "open_orders": [],
                "fills": [],
                "fees": [],
                "fresh": True,
                "coherent": True,
                "source_cursor": "sha256:" + "b" * 64,
                "capabilities": {"protection": True},
                "account_address": "0x" + "a" * 40,
            }

    result = dashboard_server.build_dashboard_control_account_admission_response(
        {"venue_profile_id": "hyperliquid.testnet", "instrument_id": "BTC-USD-PERP"},
        output_root=tmp_path,
        account_reader=Reader(),
    )

    assert result["admission"]["ready"] is True
    assert "account_address" not in result["admission"]["account"]
    assert result["safety"]["credentials_exposed"] is False


def _execution_market(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "source": "hyperliquid.external_testnet",
        "cursor": "cursor-1",
        "broker_id": "hyperliquid",
        "environment": "testnet",
        "instrument_id": "BTC-USD-PERP",
        "asset_index": 0,
        "mapping_revision": "mapping-v1",
        "universe_revision": "universe-v1",
        "connection_epoch": "epoch-1",
        "observed_at": "2026-08-29T01:00:00+00:00",
        "fresh": True,
        "execution_ready": True,
        "bid": 99,
        "ask": 101,
        "mid": 100,
        "mark": 100,
        "oracle": 100,
        "impact": 100,
        "depth_notional": 500,
        "max_slippage": 2,
        "max_oracle_deviation_bps": 100,
        "max_oracle_deviation_bps_source": "env",
    }
    value.update(overrides)
    return value


def _testnet_account(equity: float) -> dict[str, object]:
    return {
        "broker_id": "hyperliquid",
        "environment": "testnet",
        "account_fingerprint": "sha256:" + "a" * 64,
        "source_cursor": "sha256:" + "b" * 64,
        "fresh": True,
        "coherent": True,
        "equity": equity,
        "positions": [],
        "open_orders": [],
        "capabilities": {"protection": True},
    }


def test_execution_admission_scales_only_down_to_testnet_caps(tmp_path: Path) -> None:
    plane = DashboardControlPlane(tmp_path)
    result = plane.execution_admission(
        {
            "schema_version": "dashboard-strategy-preview-v1",
            "venue_profile_id": "hyperliquid.testnet",
            "instrument_id": "BTC-USD-PERP",
            "strategy_family": "dca",
            "blockers": [],
            "preview": {
                "dca": {"total_possible_notional": 300},
                "risk": {"maximum_loss_at_full_depth": 40},
            },
            "execution_ready": True,
        },
        market=_execution_market(),
        account=_testnet_account(1_000),
    )

    assert result["risk_gate"]["outcome"] == "scale"
    assert result["risk_gate"]["requested_notional"] == 300.0
    assert result["risk_gate"]["effective_notional"] == 100.0
    assert result["risk_gate"]["notional_cap"] == 100.0
    assert result["risk_gate"]["effective_notional"] <= result["risk_gate"]["requested_notional"]
    assert result["execution_ready"] is True
    assert result["market"]["max_oracle_deviation_bps"] == 100
    assert result["market"]["max_oracle_deviation_bps_source"] == "env"
    assert result["blockers"] == []


def test_execution_admission_blocks_thin_stale_and_oracle_dislocated_market(tmp_path: Path) -> None:
    plane = DashboardControlPlane(tmp_path)
    result = plane.execution_admission(
        {
            "schema_version": "dashboard-strategy-preview-v1",
            "venue_profile_id": "hyperliquid.testnet",
            "instrument_id": "BTC-USD-PERP",
            "strategy_family": "grid",
            "blockers": [],
            "preview": {"grid": {"notional_per_grid": 100}, "risk": {"max_loss": 5}},
            "execution_ready": True,
        },
        market=_execution_market(
            fresh=False,
            depth_notional=10,
            mark=104,
        ),
        account=_testnet_account(1_000),
    )

    assert result["execution_ready"] is False
    assert {"market_stale", "insufficient_depth", "oracle_dislocation"}.issubset(
        set(result["blockers"])
    )


def test_execution_admission_never_adds_exposure_or_overrides_blockers(tmp_path: Path) -> None:
    plane = DashboardControlPlane(tmp_path)
    result = plane.execution_admission(
        {
            "schema_version": "dashboard-strategy-preview-v1",
            "venue_profile_id": "hyperliquid.testnet",
            "instrument_id": "BTC-USD-PERP",
            "strategy_family": "dca",
            "blockers": ["testnet_account_unavailable"],
            "preview": {"dca": {"total_possible_notional": 1}, "risk": {"maximum_loss_at_full_depth": 1}},
            "execution_ready": False,
        },
        market=_execution_market(),
        account=_testnet_account(100_000),
    )

    assert result["execution_ready"] is False
    assert result["risk_gate"]["effective_notional"] == 1.0
    assert "testnet_account_unavailable" in result["blockers"]


def test_execution_admission_blocks_missing_preview_notional(tmp_path: Path) -> None:
    result = DashboardControlPlane(tmp_path).execution_admission(
        {
            "venue_profile_id": "hyperliquid.testnet",
            "instrument_id": "BTC-USD-PERP",
            "strategy_family": "dca",
            "blockers": [],
            "preview": {"risk": {"maximum_loss_at_full_depth": 1}},
            "execution_ready": True,
        },
        market=_execution_market(),
        account=_testnet_account(1_000),
    )

    assert result["execution_ready"] is False
    assert "preview_notional_missing" in result["blockers"]
    assert result["risk_gate"]["outcome"] == "reject"


def test_execution_admission_blocks_stale_or_incoherent_account_facts(tmp_path: Path) -> None:
    account = _testnet_account(1_000)
    account["fresh"] = False
    account["coherent"] = False
    result = DashboardControlPlane(tmp_path).execution_admission(
        {
            "venue_profile_id": "hyperliquid.testnet",
            "instrument_id": "BTC-USD-PERP",
            "strategy_family": "dca",
            "blockers": [],
            "preview": {"dca": {"total_possible_notional": 10}, "risk": {"maximum_loss_at_full_depth": 1}},
            "execution_ready": True,
        },
        market=_execution_market(),
        account=account,
    )

    assert result["execution_ready"] is False
    assert {"testnet_account_stale", "testnet_account_incoherent"}.issubset(
        set(result["blockers"])
    )


def _eligible_catalog_loader(profile_id: str):
    if profile_id == "hyperliquid.testnet":
        return [{
            "instrument_id": "BTC-USD-PERP",
            "asset": "BTC",
            "eligibility": "eligible",
            "market_fresh": True,
        }]
    return []


def _confirmable_preview() -> dict[str, object]:
    risk_gate = {
        "outcome": "allow",
        "requested_notional": 100.0,
        "effective_notional": 100.0,
        "requested_max_loss": 5.0,
        "effective_max_loss": 5.0,
        "notional_cap": 100.0,
        "loss_cap": 50.0,
        "subtractive_only": True,
    }
    preview: dict[str, object] = {
        "schema_version": "dashboard-strategy-preview-v1",
        "venue_profile_id": "hyperliquid.testnet",
        "instrument_id": "BTC-USD-PERP",
        "strategy_family": "dca",
        "execution_ready": True,
        "authorizing": False,
        "blockers": [],
        "requested": {"direction": "long"},
        "preview": {"dca": {"total_possible_notional": 100}, "risk": {"maximum_loss_at_full_depth": 5}},
        "risk_gate": risk_gate,
        "account": {
            "account_fingerprint": "sha256:" + "a" * 64,
            "source_cursor": "sha256:" + "b" * 64,
            "broker_id": "hyperliquid",
            "environment": "testnet",
            "fresh": True,
            "coherent": True,
            "equity": 995.46,
            "capabilities": {"protection": True},
        },
        "transport_profile": "hyperliquid-testnet-position-protection",
        "runtime_id": "runtime-dashboard-testnet",
        "release_sha": "e" * 40,
        "capability_revision": "hyperliquid-testnet-position-protection-runtime-v1",
    }
    catalog_plane = DashboardControlPlane(Path("/tmp/dashboard-confirmable"), catalog_loader=_eligible_catalog_loader)
    _, catalog_revision = catalog_plane._catalog_entry("hyperliquid.testnet", "BTC-USD-PERP")
    preview["catalog_revision"] = catalog_revision
    preview["instrument_eligibility"] = "eligible"
    preview["risk_gate_digest"] = canonical_risk_gate_digest(risk_gate)
    preview["preview_digest"] = canonical_preview_digest(preview)
    return preview


def test_confirm_and_run_creates_one_identity_bound_activation(tmp_path: Path) -> None:
    class Coordinator:
        def __init__(self):
            self.calls = []

        def activate(self, activation, *, command_id=None, now=None):
            self.calls.append((dict(activation), command_id, now))
            return {"status": "activated", "execution_enabled": False, "activation_id": "activation-1"}

    coordinator = Coordinator()
    plane = DashboardControlPlane(tmp_path, catalog_loader=_eligible_catalog_loader)
    preview = _confirmable_preview()
    plane.persist_preview(preview)
    result = plane.confirm_and_run(
        preview,
        confirmation={"preview_digest": preview["preview_digest"], "operator_id": "park", "acknowledged": True},
        coordinator=coordinator,
        now="2026-08-29T02:00:00+00:00",
    )

    assert result["status"] == "confirmed"
    assert result["execution_mutation"] is False
    assert result["environment"] == "testnet"
    assert result["instrument_id"] == "BTC-USD-PERP"
    assert result["confirmation_digest"].startswith("sha256:")
    assert len(coordinator.calls) == 1
    activation = coordinator.calls[0][0]
    assert activation["environment"] == "testnet"
    assert activation["account_fingerprint"].startswith("sha256:")
    assert activation["requested_notional"] == "100.0"
    assert activation["effective_notional"] == "100.0"
    assert activation["effective_max_loss"] == "5.0"
    assert activation["risk_gate_digest"].startswith("sha256:")
    assert "private_key" not in activation
    persisted = json.loads(
        (tmp_path / "dashboard_control_plane" / "confirmations.json").read_text(encoding="utf-8")
    )[-1]
    assert persisted["acknowledged"] is True
    assert persisted["operator_id"] == "park"


def test_confirm_and_run_rejects_stale_digest_and_duplicate_identity(tmp_path: Path) -> None:
    plane = DashboardControlPlane(tmp_path, catalog_loader=_eligible_catalog_loader)
    preview = _confirmable_preview()
    plane.persist_preview(preview)
    first = plane.confirm_and_run(
        preview,
        confirmation={"preview_digest": preview["preview_digest"], "operator_id": "park", "acknowledged": True},
        coordinator=None,
    )
    replay = plane.confirm_and_run(
        preview,
        confirmation={"preview_digest": preview["preview_digest"], "operator_id": "park", "acknowledged": True},
        coordinator=None,
    )

    assert first == replay
    blocked = plane.confirm_and_run(
        {**preview, "preview_digest": "sha256:" + "f" * 64},
        confirmation={"preview_digest": preview["preview_digest"], "operator_id": "park", "acknowledged": True},
        coordinator=None,
    )
    assert blocked["status"] == "blocked"
    assert "confirmation_digest_mismatch" in blocked["blockers"]


def test_confirm_stop_reconcile_then_confirm_new_plan_closes_ledger(tmp_path: Path) -> None:
    from services.testnet_automation_coordinator import TestnetAutomationCoordinator

    plane = DashboardControlPlane(tmp_path, catalog_loader=_eligible_catalog_loader)
    coordinator = TestnetAutomationCoordinator(tmp_path, clock=lambda: "2026-09-08T03:00:00+00:00")
    first_preview = _confirmable_preview()
    plane.persist_preview(first_preview)
    first = plane.confirm_and_run(
        first_preview,
        confirmation={"preview_digest": first_preview["preview_digest"], "operator_id": "park", "acknowledged": True},
        coordinator=coordinator,
    )

    coordinator._record({
        **coordinator.status(),
        "status": "paper_execution_ready",
        "execution_profile": "standard-broker-paper",
        "execution_enabled": True,
        "execution_ready": True,
        "execution_mutation": False,
        "network_operation_invoked": False,
        "canonical_order_count": 0,
        "execution_receipts": [],
    })
    coordinator.command("stop", {"reason": "operator_stop"}, command_id="stop-1171")
    stopped = json.loads(
        (tmp_path / "dashboard_control_plane" / "confirmations.json").read_text()
    )[-1]
    assert stopped["event"] == "plan_closed"
    assert stopped["reason"] == "stop"
    reconciled = coordinator.command(
        "reconcile_stop", {"reason": "zero_orders"}, command_id="reconcile-1171"
    )
    assert reconciled["status"] == "idle"

    closed = json.loads(
        (tmp_path / "dashboard_control_plane" / "confirmations.json").read_text()
    )[-1]
    assert closed["event"] == "plan_closed"
    assert closed["reason"] == "reconcile_stop"
    assert closed["activation_id"] == first["activation_id"]

    second_preview = {**first_preview, "requested": {**first_preview["requested"], "direction": "short"}}
    second_preview["preview_digest"] = canonical_preview_digest(second_preview)
    plane.persist_preview(second_preview)
    second = plane.confirm_and_run(
        second_preview,
        confirmation={"preview_digest": second_preview["preview_digest"], "operator_id": "park", "acknowledged": True},
        coordinator=coordinator,
    )
    assert second["status"] == "confirmed"
    assert second["activation_id"] != first["activation_id"]


def test_two_unclosed_confirmed_plans_remain_in_conflict(tmp_path: Path) -> None:
    plane = DashboardControlPlane(tmp_path, catalog_loader=_eligible_catalog_loader)
    first_preview = _confirmable_preview()
    plane.persist_preview(first_preview)
    first = plane.confirm_and_run(
        first_preview,
        confirmation={"preview_digest": first_preview["preview_digest"], "operator_id": "park", "acknowledged": True},
        coordinator=None,
    )
    second_preview = {**first_preview, "requested": {**first_preview["requested"], "direction": "short"}}
    second_preview["preview_digest"] = canonical_preview_digest(second_preview)
    plane.persist_preview(second_preview)

    class ActiveCoordinator:
        def status(self):
            return {"status": "activated", "activation_id": first["activation_id"]}

    blocked = plane.confirm_and_run(
        second_preview,
        confirmation={"preview_digest": second_preview["preview_digest"], "operator_id": "park", "acknowledged": True},
        coordinator=ActiveCoordinator(),
    )
    assert blocked["status"] == "blocked"
    assert blocked["blockers"] == ["active_plan_conflict"]


def test_idle_coordinator_migrates_historical_confirmation_before_new_plan(tmp_path: Path) -> None:
    plane = DashboardControlPlane(tmp_path, catalog_loader=_eligible_catalog_loader)
    first_preview = _confirmable_preview()
    plane.persist_preview(first_preview)
    first = plane.confirm_and_run(
        first_preview,
        confirmation={"preview_digest": first_preview["preview_digest"], "operator_id": "park", "acknowledged": True},
        coordinator=None,
    )
    second_preview = {**first_preview, "requested": {**first_preview["requested"], "direction": "short"}}
    second_preview["preview_digest"] = canonical_preview_digest(second_preview)
    plane.persist_preview(second_preview)

    class IdleCoordinator:
        def status(self):
            return {"status": "idle", "activation_id": None}

        def activate(self, activation, *, command_id=None, now=None):
            return {"status": "activated", "activation_id": "new-activation"}

    result = plane.confirm_and_run(
        second_preview,
        confirmation={"preview_digest": second_preview["preview_digest"], "operator_id": "park", "acknowledged": True},
        coordinator=IdleCoordinator(),
        now="2026-09-08T03:30:00+00:00",
    )
    rows = json.loads((tmp_path / "dashboard_control_plane" / "confirmations.json").read_text())
    migration = rows[1]
    assert migration["reason"] == "reconciled_idle"
    assert migration["activation_id"] == first["activation_id"]
    assert result["status"] == "confirmed"


def test_confirm_and_run_rejects_mainnet_and_secret_fields(tmp_path: Path) -> None:
    plane = DashboardControlPlane(tmp_path, catalog_loader=_eligible_catalog_loader)
    preview = _confirmable_preview()
    plane.persist_preview(preview)

    mainnet = plane.confirm_and_run(
        {**preview, "environment": "mainnet"},
        confirmation={"preview_digest": preview["preview_digest"], "operator_id": "park", "acknowledged": True},
        coordinator=None,
    )
    secret = plane.confirm_and_run(
        {**preview, "private_key": "should-never-cross"},
        confirmation={"preview_digest": preview["preview_digest"], "operator_id": "park", "acknowledged": True},
        coordinator=None,
    )

    assert mainnet["status"] == "blocked"
    assert "testnet_only" in mainnet["blockers"]
    assert secret["status"] == "blocked"
    assert "secret_field_forbidden" in secret["blockers"]


def test_confirm_and_run_rejects_tampered_payload_with_old_digest(tmp_path: Path) -> None:
    plane = DashboardControlPlane(tmp_path, catalog_loader=_eligible_catalog_loader)
    preview = _confirmable_preview()
    plane.persist_preview(preview)
    tampered = {**preview, "market": {"latest_close": 999}}

    result = plane.confirm_and_run(
        tampered,
        confirmation={"preview_digest": preview["preview_digest"], "operator_id": "park", "acknowledged": True},
        coordinator=None,
    )

    assert result["status"] == "blocked"
    assert "preview_digest_invalid" in result["blockers"]


def test_confirm_and_run_blocks_when_current_market_is_not_fresh(tmp_path: Path) -> None:
    plane = DashboardControlPlane(
        tmp_path,
        catalog_loader=lambda profile_id: [{
            "instrument_id": "BTC-USD-PERP",
            "asset": "BTC",
            "eligibility": "eligible",
            "market_fresh": False,
        }] if profile_id == "hyperliquid.testnet" else [],
    )
    preview = _confirmable_preview()
    plane.persist_preview(preview)

    result = plane.confirm_and_run(
        preview,
        confirmation={"preview_digest": preview["preview_digest"], "operator_id": "park", "acknowledged": True},
        coordinator=None,
    )

    assert result["status"] == "blocked"
    assert "market_freshness_unavailable" in result["blockers"]


def test_confirm_and_run_rejects_catalog_revision_or_eligibility_drift(tmp_path: Path) -> None:
    plane = DashboardControlPlane(tmp_path, catalog_loader=lambda profile_id: [])
    preview = _confirmable_preview()
    plane.persist_preview(preview)

    result = plane.confirm_and_run(
        preview,
        confirmation={"preview_digest": preview["preview_digest"], "operator_id": "park", "acknowledged": True},
        coordinator=None,
    )

    assert result["status"] == "blocked"
    assert "instrument_not_in_catalog" in result["blockers"]


def test_catalog_revision_ignores_market_quality_and_freshness(tmp_path: Path) -> None:
    rows = [{
        "instrument_id": "BTC-USD-PERP",
        "asset": "BTC",
        "eligibility": "eligible",
        "market_fresh": True,
        "market_quality": {"bid": 100, "ask": 101, "mid": 100.5},
    }]
    changed = [{**rows[0], "market_fresh": False, "market_quality": {"bid": 200, "ask": 201, "mid": 200.5}}]
    first = DashboardControlPlane(tmp_path, catalog_loader=lambda _: rows).catalog()
    second = DashboardControlPlane(tmp_path, catalog_loader=lambda _: changed).catalog()
    assert first["venue_profiles"][1]["catalog_revision"] == second["venue_profiles"][1]["catalog_revision"]


def test_dashboard_confirmation_builder_keeps_coordinator_activation_non_mutating(tmp_path: Path) -> None:
    from pipelines import dashboard_server

    class Coordinator:
        def activate(self, activation, *, command_id=None, now=None):
            return {
                "status": "activated",
                "activation_id": "sha256:" + "c" * 64,
                "execution_mutation": False,
                "command_id": command_id,
                "occurred_at": now,
                "instrument_id": activation["instrument_id"],
            }

    preview = _confirmable_preview()
    # The server builder accepts a test-only catalog loader so this test does
    # not depend on a live public metadata response.
    DashboardControlPlane(tmp_path, catalog_loader=_eligible_catalog_loader).persist_preview(preview)
    result = dashboard_server.build_dashboard_control_confirmation_response(
        {
            "preview_digest": preview["preview_digest"],
            "confirmation": {
                "operator_id": "park",
                "acknowledged": True,
            },
        },
        output_root=tmp_path,
        coordinator=Coordinator(),
        catalog_loader=_eligible_catalog_loader,
    )

    assert result["confirmation"]["status"] == "confirmed"
    assert result["confirmation"]["coordinator_invoked"] is True
    assert result["safety"]["orders_submitted"] is False


def test_runtime_control_preserves_pause_stop_and_flatten_semantics(tmp_path: Path) -> None:
    class Coordinator:
        def __init__(self):
            self.calls = []

        def command(self, action, payload=None, *, command_id=None, now=None):
            self.calls.append((action, dict(payload or {}), command_id, now))
            return {
                "status": "paused" if action == "pause" else f"{action}_requested",
                "execution_mutation": False,
                "next_action": "await_reconcile",
            }

    coordinator = Coordinator()
    plane = DashboardControlPlane(tmp_path)
    paused = plane.control("pause", coordinator=coordinator, reason="operator_pause", now="2026-08-29T03:00:00+00:00")
    stopped = plane.control("stop", coordinator=coordinator, reason="operator_stop", now="2026-08-29T03:01:00+00:00")
    flattened = plane.control("flatten", coordinator=coordinator, reason="emergency", now="2026-08-29T03:02:00+00:00")

    assert paused["status"] == "paused"
    assert stopped["status"] == "stop_requested"
    assert flattened["status"] == "flatten_requested"
    assert all(row["execution_mutation"] is False for row in (paused, stopped, flattened))
    assert [row[0] for row in coordinator.calls] == ["pause", "stop", "flatten"]


def test_runtime_control_exposes_reconcile_stop(tmp_path: Path) -> None:
    class Coordinator:
        def command(self, action, payload=None, *, command_id=None, now=None):
            return {"status": "idle", "action": action, "next_action": "await_activation"}

    result = DashboardControlPlane(tmp_path).control(
        "reconcile_stop", coordinator=Coordinator(), reason="zero_orders", now="2026-09-08T03:00:00+00:00"
    )

    assert result["status"] == "idle"
    assert result["action"] == "reconcile_stop"
    assert result["next_action"] == "await_activation"


def test_runtime_terminal_notification_waits_for_operator_and_never_opens_next_plan(tmp_path: Path) -> None:
    plane = DashboardControlPlane(tmp_path)

    result = plane.record_notification(
        event="take_profit",
        strategy_family="dca",
        instrument_id="BTC-USD-PERP",
        plan_digest="sha256:" + "d" * 64,
        message="DCA aggregate take profit reached",
        now="2026-08-29T03:10:00+00:00",
    )

    assert result["status"] == "AWAITING_OPERATOR"
    assert result["event"] == "take_profit"
    assert result["next_action"] == "await_operator_next_plan"
    assert result["automatic_next_plan"] is False
    assert result["notification_id"].startswith("dashboard-notification:")
    assert result["channels"] == {"dashboard": "persisted", "telegram": "queued"}
    assert (tmp_path / "dashboard_control_plane" / "telegram_outbox.json").exists()


def test_runtime_unknown_is_query_first_and_not_a_retry_authorization(tmp_path: Path) -> None:
    class Coordinator:
        def command(self, *_args, **_kwargs):
            raise AssertionError("unknown must not issue a coordinator mutation")

    result = DashboardControlPlane(tmp_path).handle_unknown(
        operation="submit",
        plan_digest="sha256:" + "d" * 64,
        instrument_id="BTC-USD-PERP",
        coordinator=Coordinator(),
        now="2026-08-29T03:20:00+00:00",
    )

    assert result["status"] == "blocked"
    assert result["retry_authorized"] is False
    assert result["reconcile_required"] is True
    assert result["next_action"] == "reconcile_identity_bound"


def test_dashboard_runtime_status_builder_projects_authoritative_coordinator_state(tmp_path: Path) -> None:
    from pipelines import dashboard_server

    class Coordinator:
        def status(self):
            return {
                "status": "activated",
                "strategy_family": "dca",
                "instrument_id": "BTC-USD-PERP",
                "execution_enabled": False,
                "execution_blocker": "capability_gap:execution",
                "next_action": "await_execution_capability",
                "execution": {
                    "orders": [],
                    "open_orders": [],
                    "fills": [],
                    "fees": [],
                    "positions": [],
                    "protection": {"status": "blocked"},
                    "reconciliation": {"status": "unknown"},
                },
            }

    result = dashboard_server.build_dashboard_control_runtime_status_response(
        output_root=tmp_path,
        coordinator=Coordinator(),
    )

    assert result["runtime"]["status"] == "activated"
    assert result["runtime"]["execution_mutation"] is False
    assert result["runtime"]["reconciliation"]["status"] == "unknown"
    assert result["safety"]["orders_submitted"] is False
