from __future__ import annotations

import json
from pathlib import Path

import pytest

from trading_desk.config import Config
from trading_desk.sources import Sources
from trading_desk.store import Store


class FakeFetch:
    def __init__(self) -> None:
        self.routes: dict[str, object] = {}
        self.calls: list[tuple[str, object]] = []

    def __call__(self, url: str, body):
        self.calls.append((url, body))
        typed = body.get("type") if isinstance(body, dict) else None
        for prefix, value in self.routes.items():
            if (typed and typed == prefix) or (not typed and prefix in url):
                if isinstance(value, Exception):
                    raise value
                return value
        raise ConnectionError(f"no route for {typed or url}")


@pytest.fixture
def paper_output(tmp_path: Path) -> Path:
    folder = tmp_path / "out" / "dualtrack" / "grid_testnet_lifecycle"
    folder.mkdir(parents=True)
    (folder / "dashboard-plan:abc.json").write_text(json.dumps([{
        "status": "paused_above_range", "direction": "long", "lower_boundary": 74000.0, "upper_boundary": 76816.5,
        "hard_stop": 72000.0, "last_market_price": 77862.5, "updated_at": "2026-09-14T07:39:03+00:00",
        "instrument_id": "BTC-USD-PERP",
        "rungs": [{"price": p, "quantity": 0.00025} for p in (74000.0, 74563.0, 75127.0, 75690.0, 76253.0)],
        "orders": [{"state": "accepted"}] * 5, "fills": [],
    }]))
    return tmp_path / "out"


@pytest.fixture
def fetch() -> FakeFetch:
    f = FakeFetch()
    f.routes["clearinghouseState"] = {"marginSummary": {"accountValue": "995.31"}, "assetPositions": []}
    f.routes["userFills"] = [{"coin": "BTC", "closedPnl": "0.04416", "fee": "0.01"}, {"coin": "PAXG", "closedPnl": "-2", "fee": "0"}]
    f.routes["/api/park-paper/read-model"] = {
        "generated_at": "2026-09-14T07:40:54+00:00", "market": {"price": 4322.16},
        "strategy": {"active": True, "state": "ACTIVE_LOCKED", "direction": "long", "lower_price_boundary": 4300.0, "upper_price_boundary": 4500.0,
                     "hard_stop": 4250.0, "grid_rung_prices": [4318.18, 4336.36], "theoretical_max_loss": 300.0},
        "execution": {"counts": {"fills": 45, "closed_positions": 18}, "orders": [{"state": "accepted"}],
                      "positions": [{"status": "open", "side": "long", "remaining_units": 0.2, "entry_price": 4440.52, "tp": 4500.0, "sl": 4250.0}],
                      "account": {"equity": 9921.78, "starting_cash": 10000.0}, "pnl": {"realized": 64.39, "unrealized": -142.61},
                      "reconciliation": {"status": "ok"}},
    }
    bars = [{"timestamp": f"2026-09-{d:02d}T00:00:00+00:00", "open": 100.0 + d, "high": 102.0 + d, "low": 99.0 + d, "close": 101.0 + d} for d in range(1, 15)]
    f.routes["/api/dualtrack/market/bars"] = {"status": "ready", "fresh": True, "bars": bars}
    f.routes["candleSnapshot"] = [{"t": 1788998400000 + d * 86_400_000, "o": str(100 + d), "h": str(102 + d), "l": str(99 + d), "c": str(101 + d)} for d in range(14)]
    f.routes["/api/dashboard-control/catalog"] = {"venue_profiles": [{"id": "hyperliquid.testnet", "instruments": [
        {"asset": "BTC", "instrument_id": "BTC-USD-PERP", "eligibility": "eligible"}, {"asset": "ETH", "instrument_id": "ETH-USD-PERP", "eligibility": "unknown"}]}]}
    f.routes["/api/ui/realtime"] = {"items": [
        {"id": 11, "title": "美联储9月降息预期升温", "source": "cls_telegraph", "collected_at": "2099-01-01T08:00:00Z", "triage": {"bucket": "high_impact"}, "exposure_assets": ["bitcoin", "gold"]},
        {"id": 12, "title": "比特币跌破7.7万", "source": "blockbeats_newsflash", "collected_at": "2099-01-01T09:30:00Z", "triage": {"bucket": "watch"}, "exposure_assets": ["bitcoin"]},
        {"id": 13, "title": "苹果推出Siri人工智能", "source": "eastmoney_global_news", "collected_at": "2099-01-01T09:40:00Z", "triage": {"bucket": "watch"}, "exposure_assets": ["nasdaq"]},
        {"id": 14, "title": "比特币跌破7.7万", "source": "cls_telegraph", "collected_at": "2099-01-01T09:29:00Z", "triage": {"bucket": "noise"}, "exposure_assets": ["bitcoin"]},
        {"id": 15, "title": "Reddit chatter about BTC", "source": "reddit", "collected_at": "2099-01-01T09:50:00Z", "triage": {"bucket": "high_impact"}, "exposure_assets": ["bitcoin"]},
    ]}
    f.routes["/api/articles/search"] = [
        {"id": 1, "title": "周一「开门黑」，比特币跌破7.7万", "source": "blockbeats_newsflash", "collected_at": "2099-01-01T00:00:00", "triage": {"bucket": "watch"}},
        {"id": 2, "title": "汇丰：预计美联储9月加息25个基点", "source": "cls_telegraph", "collected_at": "2099-01-01T00:00:00", "triage": {"bucket": "high_impact"}},
        {"id": 3, "title": "Reddit chatter", "source": "reddit", "collected_at": "2099-01-01T00:00:00", "triage": {"bucket": "high_impact"}},
    ]
    return f


@pytest.fixture
def config(tmp_path: Path, paper_output: Path) -> Config:
    return Config(paper_output=paper_output, db_path=tmp_path / "desk.db", intel_url="http://intel", dashboard_url="http://dash", hyperliquid_info_url="http://hl",
                  kline_archive=tmp_path / "kline", morning_latest=tmp_path / "morning" / "latest.html", morning_archive=tmp_path / "morning",
                  kline_latest_html=tmp_path / "kline.html", weekly_latest_html=tmp_path / "weekly.html", paused_manifest=tmp_path / "paused.md",
                  watch_folder=tmp_path / "watch")


@pytest.fixture
def sources(config: Config, fetch: FakeFetch) -> Sources:
    return Sources(config, fetch=fetch)


@pytest.fixture
def store(config: Config) -> Store:
    return Store(config.db_path)


@pytest.fixture
def btc(store: Store) -> dict:
    return store.asset("BTC")


@pytest.fixture
def xau(store: Store) -> dict:
    return store.asset("XAU")
