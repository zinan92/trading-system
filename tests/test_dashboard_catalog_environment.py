"""The desk asks GridMind's catalog for the Paper profiles only (#1466).

The full catalog reads Hyperliquid testnet's public market (four calls, ~8 s from this host) on every request.
The desk only needs the Paper venue's ids, so ``?environment=paper`` must load the Paper catalogs and nothing
else. Selection, preview and control keep the full, live catalog.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from pipelines import dashboard_server
from services.dashboard_control_plane import DashboardControlPlane


@pytest.fixture(autouse=True)
def _no_real_datafeed(monkeypatch: pytest.MonkeyPatch) -> None:
    import services.dashboard_control_plane as plane

    def refuse(**_kwargs: object) -> dict:
        raise RuntimeError("tests never read a real datafeed")

    monkeypatch.setattr(plane, "_paper_swap_fetch", refuse)
    monkeypatch.setattr(plane, "_paper_swap_cache", {})


class RecordingLoader:
    def __init__(self) -> None:
        self.asked: list[str] = []

    def __call__(self, profile_id: str) -> list[dict]:
        self.asked.append(profile_id)
        if profile_id == "hyperliquid.testnet":
            return [{"instrument_id": "BTC-USD-PERP", "asset": "BTC", "eligibility": "eligible"}]
        return [{"instrument_id": "XAU-USDT-SWAP.OKX", "asset": "XAU", "eligibility": "eligible"}]


def test_the_paper_catalog_never_loads_another_profile(tmp_path: Path) -> None:
    loader = RecordingLoader()
    catalog = DashboardControlPlane(tmp_path, catalog_loader=loader).catalog(environment="paper")

    assert loader.asked == ["okx.paper"]
    assert [p["environment"] for p in catalog["venue_profiles"]] == ["paper"]
    assert catalog["environment_filter"] == "paper"
    assert catalog["venue_profiles"][0]["instruments"][0]["asset"] == "XAU"
    assert catalog["safety"]["orders_submitted"] is False


def test_the_full_catalog_is_unchanged(tmp_path: Path) -> None:
    loader = RecordingLoader()
    catalog = DashboardControlPlane(tmp_path, catalog_loader=loader).catalog()

    assert sorted(loader.asked) == ["hyperliquid.testnet", "okx.paper"]
    assert "environment_filter" not in catalog
    assert {p["id"] for p in catalog["venue_profiles"]} == {"okx.paper", "hyperliquid.testnet"}


def test_an_unknown_environment_is_refused_not_answered_empty(tmp_path: Path) -> None:
    loader = RecordingLoader()
    with pytest.raises(ValueError, match="venue_environment_unknown"):
        DashboardControlPlane(tmp_path, catalog_loader=loader).catalog(environment="mainnet")
    assert loader.asked == []


def test_selection_still_reads_the_full_live_catalog(tmp_path: Path) -> None:
    loader = RecordingLoader()
    plane = DashboardControlPlane(tmp_path, catalog_loader=loader)
    plane.select(venue_profile_id="hyperliquid.testnet", instrument_id="BTC-USD-PERP")
    assert "hyperliquid.testnet" in loader.asked


def _get(path: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[int, dict, RecordingLoader]:
    loader = RecordingLoader()
    monkeypatch.setattr(dashboard_server, "public_catalog_loader", loader)
    monkeypatch.setattr(dashboard_server, "_dualtrack_output_root", lambda _root=None: tmp_path)
    answer: dict = {}
    handler = object.__new__(dashboard_server.DashboardHandler)
    handler.path = path
    handler.headers = {"Host": "127.0.0.1:8765"}
    handler._write_json = lambda status, payload: answer.update(status=status, body=payload)
    handler._write_error = lambda status, code, message: answer.update(status=status, body={"error": code})
    handler.do_GET()
    return answer["status"], answer["body"], loader


def test_the_endpoint_filters_by_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    status, body, loader = _get("/api/dashboard-control/catalog?environment=paper", monkeypatch, tmp_path)
    assert status == 200 and loader.asked == ["okx.paper"]
    assert body["environment_filter"] == "paper"


def test_the_endpoint_without_a_filter_serves_every_profile(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    status, body, loader = _get("/api/dashboard-control/catalog", monkeypatch, tmp_path)
    assert status == 200 and sorted(loader.asked) == ["hyperliquid.testnet", "okx.paper"]
    assert "environment_filter" not in body


def test_the_endpoint_refuses_an_unknown_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    status, body, loader = _get("/api/dashboard-control/catalog?environment=live", monkeypatch, tmp_path)
    assert (status, body) == (400, {"error": "venue_environment_unknown"})
    assert loader.asked == []
