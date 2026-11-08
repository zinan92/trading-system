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

    sources.fetch = slow
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
