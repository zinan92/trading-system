"""The 交易回看 read model (#1306): one timeline per run, read-only, and it says what is missing."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from trading_desk import review_timeline as rt

SESSION = "session-abc"
SEAL = "2026-09-14T09:19:35+00:00"
DONE = "2026-09-14T16:21:19+00:00"


def fill(ts: str, side: str, event: str, price: float) -> dict:
    return {"ts": ts, "side": side, "event": event, "price": price, "quantity": 0.2, "cost": 0.1, "order_id": f"o-{ts}-{event}"}


@pytest.fixture
def paper(tmp_path: Path) -> Path:
    root = tmp_path / "out"
    base = root / "dualtrack" / "nautilus_authoritative"
    (base / "fills").mkdir(parents=True)
    (base / "inputs").mkdir()
    (root / "park_strategy").mkdir()
    (root / "dualtrack" / "grid_testnet_lifecycle").mkdir()
    (base / "fills" / f"park-{SESSION}.json").write_text(json.dumps([
        fill("2026-09-14T01:00:00+00:00", "buy", "entry", 100.0),
        fill("2026-09-14T02:00:00+00:00", "buy", "entry", 90.0),
        fill(SEAL, "sell", "stop", 80.0),
        fill(DONE, "sell", "stop", 85.0),
    ]))
    (base / "inputs" / f"park-{SESSION}.json").write_text(json.dumps([{"market_events": [
        {"event_started_at": "2026-09-14T01:00:00+00:00", "price": 100, "open": 101, "high": 102, "low": 99, "provider": "binance_usdm_futures", "instrument_id": "XAUUSDT.BINANCE"},
        {"event_started_at": "2026-09-14T01:30:00+00:00", "price": 95, "open": 100, "high": 100, "low": 94},
        {"event_started_at": "2026-09-14T02:00:00+00:00", "price": 90, "open": 95, "high": 95, "low": 89},
    ]}]))
    write_rows(root / "park_strategy" / "identity.jsonl", [{"event": "session_started", "observed_at": "2026-09-14T00:50:00+00:00", "strategy_session_id": SESSION}])
    write_rows(root / "park_strategy" / "lifecycle.jsonl", [
        {"event": "plan_activated", "strategy_session_id": SESSION, "plan_digest": "sha256:p", "upper_price_boundary": 110, "lower_price_boundary": 80, "stop_price": 70},
    ])
    write_rows(root / "park_strategy" / "plans.jsonl", [{"event": "plan_proposed", "plan_digest": "sha256:p", "created_at": "2026-09-14T00:50:00+00:00"}])
    write_rows(root / "park_strategy" / "executions.jsonl", [
        {"event": "terminal_paused", "strategy_session_id": SESSION, "recorded_at": SEAL, "terminal_key": f"{SESSION}:revision-1:lower_boundary_invalidated",
         "result": {"reconciliation": {"status": "ok", "counts": {"open_positions": 2}}}},
        {"event": "terminal_close_completed", "recorded_at": DONE, "terminal_key": f"{SESSION}:revision-1:lower_boundary_invalidated", "exits": 2},
    ])
    return root


def write_rows(path: Path, rows: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")


def no_bars(_coin: str, _start: str, _end: str) -> dict:
    raise AssertionError("a Paper run uses its own recorded market events")


def test_paper_run_is_listed_with_its_real_start(paper: Path) -> None:
    [run] = rt.runs(paper)
    assert run["id"] == f"paper:{SESSION}" and run["fill_count"] == 4
    assert run["started_at"] == "2026-09-14T00:50:00+00:00"


def test_timeline_keeps_the_ledger_timestamps_and_says_it_ended_open(paper: Path) -> None:
    data = rt.timeline(paper, f"paper:{SESSION}", bars=no_bars, judgments=[])
    assert [e["kind"] for e in data["events"]] == ["plan", "start", "seal", "closed"]
    assert data["events"][2]["ts"] == SEAL and data["events"][3]["ts"] == DONE
    assert "当时还有 2 笔未平" in data["events"][2]["title"]
    assert data["result"]["ended_flat"] is False
    assert data["run"]["reason"] == "价格跌破区间下沿"
    assert [line["price"] for line in data["lines"]] == [110, 80, 70]
    assert data["missing"] == []


def test_exit_stamped_at_the_seal_is_shown_when_it_settled(paper: Path) -> None:
    fills = rt.timeline(paper, f"paper:{SESSION}", bars=no_bars, judgments=[])["fills"]
    backdated = [f for f in fills if f["backdated"]]
    assert len(backdated) == 1
    assert backdated[0]["ts"] == SEAL and backdated[0]["shown_ts"] == DONE and backdated[0]["price"] == 80.0


def test_net_result_counts_fees_only_on_a_flat_book(paper: Path) -> None:
    result = rt.timeline(paper, f"paper:{SESSION}", bars=no_bars, judgments=[])["result"]
    assert (result["gross"], result["fees"], result["net"]) == (-5.0, 0.4, -5.4)
    assert result["excludes_funding"] is True
    open_book = rt._result([fill("2026-09-14T01:00:00+00:00", "buy", "entry", 100.0)])
    assert open_book["flat"] is False and open_book["net"] is None and open_book["gross"] is None


def test_chart_bars_come_from_the_recorded_events(paper: Path) -> None:
    market = rt.timeline(paper, f"paper:{SESSION}", bars=no_bars, judgments=[])["market"]
    assert market["source"] == "binance_usdm_futures" and market["instrument"] == "XAUUSDT.BINANCE"
    assert [(b["open"], b["high"], b["low"], b["close"]) for b in market["bars"]] == [(101.0, 102.0, 94.0, 95.0), (95.0, 95.0, 89.0, 90.0)]


@pytest.mark.parametrize(("name", "label"), [("lifecycle.jsonl", "park_strategy/lifecycle.jsonl"), ("executions.jsonl", "park_strategy/executions.jsonl")])
def test_a_missing_ledger_is_named_not_guessed(paper: Path, name: str, label: str) -> None:
    (paper / "park_strategy" / name).unlink()
    data = rt.timeline(paper, f"paper:{SESSION}", bars=no_bars, judgments=[])
    assert label in data["missing"]
    assert data["fills"]
    if name == "executions.jsonl":
        assert data["result"]["ended_flat"] is None


def test_only_judgments_inside_the_run_are_drawn(paper: Path) -> None:
    rows = [{"id": 1, "created_at": "2026-09-14T03:00:00+00:00", "author": "ai", "direction": "long", "outcome": "hit"},
            {"id": 2, "created_at": "2026-09-20T03:00:00+00:00", "author": "ai", "direction": "short", "outcome": "miss"}]
    assert [j["id"] for j in rt.timeline(paper, f"paper:{SESSION}", bars=no_bars, judgments=rows)["judgments"]] == [1]


def test_testnet_run_reads_its_lifecycle_and_names_missing_bars(paper: Path) -> None:
    state = {"strategy_plan_id": "dashboard-plan:x", "direction": "long", "status": "terminal", "terminal_reason": "hard_stop", "created_at": "2026-09-09T03:00:00+00:00",
             "instrument_id": "BTC-USD-PERP", "lower_boundary": 74000, "upper_boundary": 78000,
             "fills": [{"event": "entry", "price": 77000, "quantity": 0.001, "timestamp": "2026-09-09T04:00:00+00:00", "order_id": "a"},
                       {"event": "hard_stop", "price": 76000, "quantity": 0.001, "timestamp": "2026-09-09T05:00:00+00:00", "order_id": "b"}],
             "events": [{"event": "lifecycle_started", "timestamp": "2026-09-09T03:00:00+00:00"}, {"event": "fill_received", "timestamp": "2026-09-09T04:00:00+00:00"},
                        {"event": "lifecycle_blocked", "timestamp": "2026-09-09T04:30:00+00:00"}, {"event": "lifecycle_blocked", "timestamp": "2026-09-09T04:31:00+00:00"}]}
    (paper / "dualtrack" / "grid_testnet_lifecycle" / "dashboard-plan:x.json").write_text(json.dumps([state]))
    run = next(r for r in rt.runs(paper) if r["environment"] == "testnet")
    assert run["id"] == "testnet:dashboard-plan:x" and run["asset"] == "BTC"
    data = rt.timeline(paper, run["id"], bars=lambda coin, _s, _e: {"ok": coin != "BTC", "bars": []}, judgments=[])
    assert [f["side"] for f in data["fills"]] == ["buy", "sell"]
    assert [e["title"] for e in data["events"]] == ["启动", "被门禁拦下"]
    assert data["run"]["reason"] == "触发硬止损"
    assert data["result"]["gross"] == -1.0 and data["result"]["net"] is None
    assert data["missing"] == ["行情记录"]


def test_unknown_or_unsafe_run_id_returns_nothing(paper: Path) -> None:
    for run_id in ("paper:nope", "paper:../../etc", "other:x", "paper:"):
        assert rt.timeline(paper, run_id, bars=no_bars, judgments=[]) is None


def test_scoreboard_keeps_a_zero_author_visible() -> None:
    board = rt.scoreboard([{"author": "ai", "outcome": "hit"}, {"author": "ai", "outcome": None}, {"author": "ai", "outcome": "miss"}])
    assert board["ai"] == {"total": 3, "hit": 1, "miss": 1, "even": 0, "unverifiable": 0, "pending": 1}
    assert board["park"]["total"] == 0


def test_reading_writes_nothing(paper: Path) -> None:
    before = {p: (p.stat().st_mtime_ns, p.read_bytes()) for p in paper.rglob("*") if p.is_file()}
    rt.runs(paper)
    rt.timeline(paper, f"paper:{SESSION}", bars=no_bars, judgments=[])
    assert {p: (p.stat().st_mtime_ns, p.read_bytes()) for p in paper.rglob("*") if p.is_file()} == before
