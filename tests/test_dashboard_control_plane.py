from __future__ import annotations

from pathlib import Path

from services.dashboard_control_plane import DashboardControlPlane


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
