from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from pipelines import dashboard_server
from schemas.market_data import Bar
from services.dualtrack_human import DualTrackHumanEngine
from services.dualtrack_machine import DualTrackMachineRunner
from services.dualtrack_scoring import DualTrackScorer
from services.dualtrack_store import DualTrackPlanStore
from services.journal_store import load_json
from tests.test_dualtrack_dt2_machine_runner import TEST_CONFIG


ROOT = Path(__file__).resolve().parents[1]


def read_replay_html() -> str:
    return (ROOT / "dashboard-dualtrack-replay.html").read_text(encoding="utf-8")


def _bar(ts: datetime, open_: float, close: float) -> Bar:
    return Bar(
        symbol="GOLD",
        timeframe="1m",
        timestamp=ts.isoformat(),
        open=open_,
        high=max(open_, close),
        low=min(open_, close),
        close=close,
        volume=1,
        provider="test",
    )


def _bars() -> list[Bar]:
    start = datetime(2026, 7, 5, 1, 0, tzinfo=timezone.utc)
    closes = [4000.0, 3990.0, 4005.0, 4020.0]
    previous = closes[0]
    rows = []
    for index, close in enumerate(closes):
        rows.append(_bar(start + timedelta(minutes=index), previous, close))
        previous = close
    return rows


def _plan(cycle_id: str) -> dict:
    return {
        "cycle_id": cycle_id,
        "direction": "long",
        "range": {"low": 3940.0, "high": 4050.0},
        "key_levels": [3992.0],
        "invalidation": [{"side": "below", "price": 3940.0, "confirm": "touch"}],
        "confidence": 7,
    }


def test_dualtrack_replay_page_uses_closed_attribution_only() -> None:
    html = read_replay_html()

    assert "<title>双轨同步回放 - Trading Orchestrator</title>" in html
    assert "D6-1 CLOSED CYCLES ONLY" in html
    assert "data/vendor/lightweight-charts.standalone.production.js" in html
    assert "packages/standard-kline/standard-kline.js" in html
    assert "StandardKline.StandardKlineChart" in html
    assert "window.dualtrackReplayKlines" in html
    assert "function buildTrackFillPriceLines(fills, color)" in html
    assert "function buildTrackFillMarkers(fills, candles, track, color)" in html
    assert '<svg id="humanChart"' not in html
    assert '<svg id="machineChart"' not in html
    assert 'api(`/api/dualtrack/attribution/${encodeURIComponent(state.cycleId)}`)' in html
    assert 'api("/api/dualtrack/market/bars?limit=720")' in html
    assert "/api/dualtrack/machine" not in html
    assert "/api/dualtrack/human" not in html
    assert "renderClosedOnly" in html
    assert "renderPane(\"human\"" in html
    assert "renderPane(\"machine\"" in html
    assert "selectFillFromEvent" in html
    assert "shared replay cursor" in html


def test_dualtrack_console_deep_links_to_dt6_replay() -> None:
    html = (ROOT / "dashboard-dualtrack-v5.html").read_text(encoding="utf-8")

    assert "dashboard-dualtrack-replay.html?layout=dualtrack&cycle=" in html
    assert "DT6 · 双轨同步回放" in html


def test_dashboard_server_disables_cache_for_dualtrack_replay() -> None:
    handler = object.__new__(dashboard_server.DashboardHandler)
    handler.path = "/dashboard-dualtrack-replay.html?layout=dualtrack&cycle=2026-07-05_DAY"

    assert handler._should_disable_static_cache() is True


def test_d6_1_open_cycle_replay_data_refuses_machine_fills(tmp_path: Path) -> None:
    output = tmp_path / "outputs"
    cycle_id = "2026-07-05_DAY"
    bars = _bars()
    store = DualTrackPlanStore(output, config=TEST_CONFIG)
    store.save_human_plan(_plan(cycle_id), now="2026-07-05T00:59:00+00:00")
    DualTrackMachineRunner(output, config=TEST_CONFIG).run_effective_plan(
        cycle_id,
        bars,
        prev_range=80.0,
        as_of="2026-07-05T01:00:00+00:00",
    )
    assert load_json(output / "dualtrack" / "fills" / f"{cycle_id}_machine.json")

    with pytest.raises(ValueError, match="only available after close"):
        dashboard_server.build_dualtrack_attribution_response(cycle_id, output_root=output)

    assert not (output / "dualtrack" / "attribution" / f"{cycle_id}.json").exists()


def test_closed_cycle_attribution_contains_both_track_fills_for_replay(tmp_path: Path) -> None:
    output = tmp_path / "outputs"
    cycle_id = "2026-07-05_DAY"
    bars = _bars()
    store = DualTrackPlanStore(output, config=TEST_CONFIG)
    store.save_human_plan(_plan(cycle_id), now="2026-07-05T00:59:00+00:00")
    DualTrackMachineRunner(output, config=TEST_CONFIG).run_effective_plan(
        cycle_id,
        bars,
        prev_range=80.0,
        as_of="2026-07-05T01:00:00+00:00",
    )
    DualTrackHumanEngine(output, config=TEST_CONFIG).submit_order(
        {
            "cycle_id": cycle_id,
            "ts": "2026-07-05T01:01:00+00:00",
            "side": "buy",
            "order_type": "market",
            "price": 4000.0,
            "notional": 1000.0,
        }
    )

    DualTrackScorer(output, config=TEST_CONFIG).close_cycle(cycle_id, bars)
    response = dashboard_server.build_dualtrack_attribution_response(cycle_id, output_root=output)

    assert response["status"] == "closed"
    assert response["fills"]["machine"]
    assert response["fills"]["human"]
    assert all(fill["track"] == "machine" for fill in response["fills"]["machine"])
    assert all(fill["track"] == "human" for fill in response["fills"]["human"])
