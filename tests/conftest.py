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
        key = body["type"] if body else url
        for prefix, value in self.routes.items():
            if key == prefix or (not body and prefix in url):
                if isinstance(value, Exception):
                    raise value
                return value
        raise ConnectionError(f"no route for {key}")


@pytest.fixture
def paper_output(tmp_path: Path) -> Path:
    folder = tmp_path / "out" / "dualtrack" / "grid_testnet_lifecycle"
    folder.mkdir(parents=True)
    (folder / "dashboard-plan:abc.json").write_text(json.dumps([{
        "status": "paused_above_range", "direction": "long", "lower_boundary": 74000.0, "upper_boundary": 76816.5,
        "hard_stop": 72000.0, "last_market_price": 77862.5, "updated_at": "2026-09-14T07:39:03+00:00",
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
        "strategy": {"state": "ACTIVE_LOCKED", "direction": "long", "lower_price_boundary": 4300.0, "upper_price_boundary": 4500.0,
                     "hard_stop": 4250.0, "grid_rung_prices": [4318.18, 4336.36], "theoretical_max_loss": 300.0},
        "execution": {"counts": {"fills": 45, "closed_positions": 18}, "orders": [{"state": "accepted"}],
                      "positions": [{"status": "open", "side": "long", "remaining_units": 0.2, "entry_price": 4440.52, "tp": 4500.0, "sl": 4250.0}],
                      "account": {"equity": 9921.78, "starting_cash": 10000.0}, "pnl": {"realized": 64.39, "unrealized": -142.61},
                      "reconciliation": {"status": "ok"}},
    }
    bars = [{"timestamp": f"2026-09-{d:02d}T00:00:00+00:00", "open": 100.0 + d, "high": 102.0 + d, "low": 99.0 + d, "close": 101.0 + d} for d in range(1, 15)]
    f.routes["/api/dashboard-control/market-bars"] = {"status": "ready", "fresh": True, "bars": bars}
    f.routes["/api/dualtrack/market/bars"] = {"status": "ready", "fresh": True, "bars": bars}
    f.routes["/api/articles/search"] = [
        {"id": 1, "title": "周一「开门黑」，比特币跌破7.7万", "source": "blockbeats_newsflash", "collected_at": "2099-01-01T00:00:00", "triage": {"bucket": "watch"}},
        {"id": 2, "title": "汇丰：预计美联储9月加息25个基点", "source": "cls_telegraph", "collected_at": "2099-01-01T00:00:00", "triage": {"bucket": "high_impact"}},
        {"id": 3, "title": "Reddit chatter", "source": "reddit", "collected_at": "2099-01-01T00:00:00", "triage": {"bucket": "high_impact"}},
    ]
    return f


@pytest.fixture
def config(tmp_path: Path, paper_output: Path) -> Config:
    return Config(paper_output=paper_output, db_path=tmp_path / "desk.db", intel_url="http://intel", dashboard_url="http://dash", hyperliquid_info_url="http://hl")


@pytest.fixture
def sources(config: Config, fetch: FakeFetch) -> Sources:
    return Sources(config, fetch=fetch)


@pytest.fixture
def store(config: Config) -> Store:
    return Store(config.db_path)
