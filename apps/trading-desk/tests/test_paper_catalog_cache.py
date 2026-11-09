"""GridMind's catalog is read once for gold, silver and crude, and kept (#1431).

GridMind's catalog takes 8-13 s in production (it reads Hyperliquid's public market each time). /api/assets used
to read it once per Paper asset, so the page waited ~29 s for 「今天」 and the asset buttons.
"""
from __future__ import annotations

import threading
import time

from fastapi.testclient import TestClient

from trading_desk.app import create_app

CATALOG = "/api/dashboard-control/catalog"


def catalog_reads(fetch) -> int:
    return sum(1 for url, _body in fetch.calls if CATALOG in url)


def test_the_asset_list_reads_the_catalog_once_for_every_paper_asset(config, sources, store, fetch):
    client = TestClient(create_app(config, sources, store))
    assets = client.get("/api/assets").json()["assets"]
    kinds = {a["kind"] for a in assets}
    assert {"xau_paper", "xag_paper", "cl_paper"} <= kinds  # gold, silver and crude all ask for ids
    assert catalog_reads(fetch) == 1
    client.get("/api/assets")
    assert catalog_reads(fetch) == 1  # served from the kept read
    gold = next(a for a in assets if a["kind"] == "xau_paper")
    assert (gold["venue_profile_id"], gold["instrument_id"]) == ("okx.paper", "XAU-USDT-SWAP.OKX")


def test_an_ageing_read_is_served_while_one_background_refresh_runs(sources, fetch, monkeypatch):
    assert sources.paper_gold()["venue_profile_id"] == "okx.paper"
    stamp, payload = sources._paper_catalog
    sources._paper_catalog = (stamp - sources.PAPER_CATALOG_REFRESH_SECONDS - 1, payload)
    started = []
    monkeypatch.setattr(threading.Thread, "start", lambda self: started.append(self.name))
    assert sources.paper_gold()["venue_profile_id"] == "okx.paper"  # no wait
    assert sources.paper_gold()["venue_profile_id"] == "okx.paper"
    assert started == ["paper-catalog"]  # one refresh, not one per caller
    assert catalog_reads(fetch) == 1


def test_a_failed_refresh_keeps_the_last_good_ids_until_they_expire(sources, fetch):
    assert sources.paper_gold()["instrument_id"] == "XAU-USDT-SWAP.OKX"
    good = fetch.routes.pop(CATALOG)
    sources._paper_catalog_refreshing = True
    sources._refresh_paper_catalog()  # the catalog is unreachable now
    assert sources.paper_gold()["instrument_id"] == "XAU-USDT-SWAP.OKX"
    assert sources._paper_catalog_refreshing is False
    stamp, payload = sources._paper_catalog
    sources._paper_catalog = (stamp - sources.PAPER_CATALOG_TTL_SECONDS - 1, payload)
    assert sources.paper_gold() is None  # expired and unreadable: unknown, never guessed
    fetch.routes[CATALOG] = good
    assert sources.paper_gold()["instrument_id"] == "XAU-USDT-SWAP.OKX"


def test_callers_that_arrive_together_share_one_read(sources, fetch):
    real = fetch.__call__

    def slow(url, body):
        if CATALOG in url:
            time.sleep(0.2)
        return real(url, body)

    sources.catalog_fetch = slow
    results: list[object] = []
    workers = [threading.Thread(target=lambda: results.append(sources.paper_gold())) for _ in range(4)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()
    assert len(results) == 4 and all(r and r["venue_profile_id"] == "okx.paper" for r in results)
    assert catalog_reads(fetch) == 1


def test_an_unreadable_catalog_is_not_kept(sources, fetch):
    good = fetch.routes.pop(CATALOG)
    assert sources.paper_gold() is None
    assert sources._paper_catalog is None
    fetch.routes[CATALOG] = good
    assert sources.paper_gold()["venue_profile_id"] == "okx.paper"


def test_the_catalog_read_waits_longer_than_the_default_cut_off(config, monkeypatch):
    """Production's catalog took up to 12.8 s; the default 12 s timeout left gold's venue unknown now and then."""
    from trading_desk import sources as module

    seen = []
    monkeypatch.setattr(module, "http_json", lambda url, body=None, timeout=12.0: seen.append((url, timeout)) or {"venue_profiles": []})
    module.Sources(config).paper_catalog()
    assert seen and seen[0][0].endswith("/api/dashboard-control/catalog?environment=paper") and seen[0][1] == 30


def test_paper_reads_ask_for_the_paper_profiles_only(sources, fetch):
    """The full catalog reads Hyperliquid testnet (~8 s); the Paper ids never wait on it (#1466)."""
    assert sources.paper_gold()["venue_profile_id"] == "okx.paper"
    urls = [url for url, _body in fetch.calls if CATALOG in url]
    assert urls and all(url.endswith(f"{CATALOG}?environment=paper") for url in urls)


def test_the_add_asset_list_still_reads_the_testnet_catalog(sources, fetch):
    assert sources.catalog()["venue"] == "Hyperliquid Testnet"
    assert [url for url, _body in fetch.calls if CATALOG in url][-1].endswith(CATALOG)
