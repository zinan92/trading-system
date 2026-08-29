from __future__ import annotations

from pathlib import Path

from services.dashboard_control_plane import DashboardControlPlane


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
    plane = DashboardControlPlane(tmp_path)

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
    ):
        assert f'id="{element_id}"' in html
    assert "/api/dashboard-control/catalog" in html
    assert "/api/dashboard-control/selection" in html
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
            "market": _market(),
            "account": {"equity": 10_000},
        },
        output_root=tmp_path,
    )

    assert result["preview"]["authorizing"] is False
    assert result["safety"]["orders_submitted"] is False


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
    }
    value.update(overrides)
    return value


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
        account={"equity": 1_000, "positions": [], "open_orders": []},
    )

    assert result["risk_gate"]["outcome"] == "scale"
    assert result["risk_gate"]["requested_notional"] == 300.0
    assert result["risk_gate"]["effective_notional"] == 100.0
    assert result["risk_gate"]["notional_cap"] == 100.0
    assert result["risk_gate"]["effective_notional"] <= result["risk_gate"]["requested_notional"]
    assert result["execution_ready"] is True
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
        account={"equity": 1_000, "positions": [], "open_orders": []},
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
        account={"equity": 100_000, "positions": [], "open_orders": []},
    )

    assert result["execution_ready"] is False
    assert result["risk_gate"]["effective_notional"] == 1.0
    assert "testnet_account_unavailable" in result["blockers"]
