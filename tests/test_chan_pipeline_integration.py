"""End-to-end: a fresh chan 二买 (segment second-buy) on real 1m gold drives the
full per-strategy paper pipeline to a long ticket that executes and reconciles.

This is the deterministic, offline proof of the production behaviour ("二买/类二买
→ 开多"): it positions the committed real-1m-gold fixture so a BUY-type point lands
on the final bar, then runs MultiStrategyRunner exactly as the strategies job does
(minus the live feed refresh, which only the CLI performs). Skips on 3.9 (no chan)."""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from schemas.market_data import Bar
from services.market_store import MarketStore
from services.strategy_registry import StrategyRegistry

pytest.importorskip("pandas", reason="chan core needs pandas (runs under 3.13)")
if sys.version_info < (3, 11):
    pytest.skip("chan core needs Python >= 3.11", allow_module_level=True)

from services.chan_signal_engine import ChanSignalEngine  # noqa: E402
from services.multi_strategy_runner import MultiStrategyRunner  # noqa: E402

_FIXTURE = Path(__file__).parent / "fixtures" / "chan_gold_1m.csv"


def _classification(role: str = "test_chan") -> dict:
    return {
        "family": "chan",
        "style": "structure_reversal",
        "directionality": "long_short",
        "frequency_bucket": "low",
        "role": role,
        "holding_period": "intraday",
        "expected_trades_per_day_min": 1,
        "expected_trades_per_day_max": 3,
        "return_profile": "structure_reversal_high_r",
        "risk_profile": "medium",
    }


def _load_bars() -> list[Bar]:
    bars = []
    for line in _FIXTURE.read_text(encoding="utf-8").splitlines()[1:]:
        ts, o, h, l, c, v = line.split(",")
        iso = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).isoformat()
        bars.append(Bar("GOLD", "1m", iso, float(o), float(h), float(l), float(c), float(v), "binance_usdm", []))
    return bars


def _key(ts: str) -> str:
    return datetime.fromisoformat(ts).strftime("%Y%m%d%H%M")


def _find_fresh_buy_cut(bars: list[Bar], engine: ChanSignalEngine) -> list[Bar]:
    """Truncate the series so its FINAL bar is a BUY-type 二买/类二买 that the engine,
    re-running causally on the truncated series, still reports as a fresh long."""
    points = engine.detect_points(bars)
    buy_keys = ["".join(ch for ch in p["time"] if ch.isdigit())[:12] for p in points if p["is_buy"]]
    assert buy_keys, "fixture should contain at least one BUY-type segment second-buy"
    asset = type("A", (), {"symbol": "GOLD", "asset_class": "commodity"})()
    for key in reversed(buy_keys):  # prefer the latest; fall back to earlier buys if causally unstable
        cut = [b for b in bars if _key(b.timestamp) <= key]
        if len(cut) < 300:
            continue
        if engine.generate(asset, cut, [], "2026-05-31").direction == "long":
            return cut
    pytest.skip("no causally-stable fresh BUY cut found in fixture")


def _cut_at_chan_time(bars: list[Bar], chan_time: str) -> list[Bar]:
    key = "".join(ch for ch in chan_time if ch.isdigit())[:12]
    cut = [b for b in bars if _key(b.timestamp) <= key]
    assert cut, f"fixture should include cutoff {chan_time}"
    return cut


def _registry_for_gate_test(strategy_id: str, tmp_path: Path, enabled: bool = True) -> StrategyRegistry:
    return StrategyRegistry(
        {
            strategy_id: {
                "symbol": "GOLD",
                "engine": "chan",
                "timeframe": "1m",
                "enabled": True,
                "classification": _classification(),
                "position_gate": {"enabled": enabled},
                "signal": {
                    "fresh_bars": 3,
                    "min_bars": 300,
                    "fetch_bars": 7200,
                    "chan_data_dir": str(tmp_path / f"chan_{strategy_id}"),
                },
            }
        }
    )


def test_chan_second_buy_drives_long_ticket_through_paper_pipeline(monkeypatch, tmp_path: Path):
    root = tmp_path / "outputs"
    db = tmp_path / "market.db"
    monkeypatch.setenv("TRADING_ORCHESTRATOR_MARKET_DB", str(db))
    monkeypatch.setenv("TRADING_ORCHESTRATOR_OFFLINE_FACTOR_FIXTURES", "1")

    bars = _load_bars()
    engine = ChanSignalEngine({"symbol": "GOLD", "signal": {"chan_data_dir": str(tmp_path / "chan")}})
    cut = _find_fresh_buy_cut(bars, engine)
    run_date = datetime.fromisoformat(cut[-1].timestamp).date().isoformat()
    MarketStore(db).upsert_bars(cut)

    strategy_id = "gold_1m_chan_paper"
    registry = StrategyRegistry(
        {strategy_id: {"symbol": "GOLD", "engine": "chan", "timeframe": "1m", "enabled": True,
                       "classification": _classification(),
                       "position_gate": {"enabled": False},
                       "signal": {"fresh_bars": 3, "min_bars": 300, "fetch_bars": 7200}}}
    )
    summary = MultiStrategyRunner(output_root=root, registry=registry).run(run_date, paper_auto_approve=True)

    strat = next(s for s in summary["strategies"] if s["strategy_id"] == strategy_id)
    assert strat["status"] == "ok"
    assert strat["reconciliation_status"] == "pass"  # criterion: reconciliation green

    ns = root / "strategies" / strategy_id
    signal = json.loads((ns / "signals" / f"{run_date}.json").read_text())[0]
    assert signal["direction"] == "long"  # 二买/类二买 → 开多
    assert signal["regime"] == "chan_second_buy"

    tickets = json.loads((ns / "trade_tickets" / f"{run_date}.json").read_text())
    assert len(tickets) >= 1
    assert tickets[0]["action"] == "prepare_buy"  # long entry
    assert strat["executed_ticket"]  # auto-approved into the isolated paper account

    # A REAL paper fill — not just a journaled decision: a filled order, an open
    # position, and modelled execution costs (spread/slippage/commission).
    orders = json.loads((ns / "paper_orders" / f"{run_date}.json").read_text())
    assert any(o["status"] == "filled" for o in orders)
    position = json.loads((ns / "paper_positions" / "current.json").read_text())["GOLD"]
    assert position["side"] == "long" and position["quantity"] > 0
    assert position["total_costs"] > 0  # entry costs actually charged
    leader = next(s for s in summary["leaderboard"] if s["strategy_id"] == strategy_id)
    assert leader["open_trades"] >= 1  # position is on the leaderboard, not flat-zero

    recon = json.loads((ns / "paper_reconciliation" / f"{run_date}.json").read_text())[0]
    invariant = next(c for c in recon["checks"] if c["name"] == "accounting_invariant")
    assert invariant["status"] == "pass"

    # Mark-to-market must work on the 1m series (no GOLD_5m.json in this namespace).
    # Without the timeframe fallback this is a silent no-op and P&L stays flat forever.
    from services.paper_executor import PaperExecutor

    marked = PaperExecutor(ns).mark_to_market(run_date)
    assert marked["GOLD"]["last_price"] > 0  # priced off the 1m clean bars, not skipped


def test_chan_second_buy_position_gate_allows_directional_paper_loop(monkeypatch, tmp_path: Path):
    root = tmp_path / "outputs"
    db = tmp_path / "market.db"
    monkeypatch.setenv("TRADING_ORCHESTRATOR_MARKET_DB", str(db))
    monkeypatch.setenv("TRADING_ORCHESTRATOR_OFFLINE_FACTOR_FIXTURES", "1")

    bars = _load_bars()
    cut = _cut_at_chan_time(bars, "2026/05/28 04:55")
    run_date = datetime.fromisoformat(cut[-1].timestamp).date().isoformat()
    MarketStore(db).upsert_bars(cut)

    strategy_id = "gold_1m_chan_gate_allow"
    summary = MultiStrategyRunner(output_root=root, registry=_registry_for_gate_test(strategy_id, tmp_path)).run(
        run_date, paper_auto_approve=True
    )

    strat = next(s for s in summary["strategies"] if s["strategy_id"] == strategy_id)
    assert strat["status"] == "ok"
    assert strat["executed_ticket"]

    ns = root / "strategies" / strategy_id
    position_map = json.loads((ns / "position_maps" / f"{run_date}.json").read_text())[0]
    signal = json.loads((ns / "signals" / f"{run_date}.json").read_text())[0]
    tickets = json.loads((ns / "trade_tickets" / f"{run_date}.json").read_text())
    orders = json.loads((ns / "paper_orders" / f"{run_date}.json").read_text())

    assert position_map["primary_timeframe"] == "4h"
    assert position_map["trade_zone"]["near_key_level"] is True
    assert signal["direction"] == "long"
    assert signal["regime"] == "chan_second_buy"
    assert len(tickets) == 1
    assert any(o["status"] == "filled" for o in orders)


def test_chan_second_buy_position_gate_blocks_directional_paper_loop(monkeypatch, tmp_path: Path):
    root = tmp_path / "outputs"
    db = tmp_path / "market.db"
    monkeypatch.setenv("TRADING_ORCHESTRATOR_MARKET_DB", str(db))
    monkeypatch.setenv("TRADING_ORCHESTRATOR_OFFLINE_FACTOR_FIXTURES", "1")

    bars = _load_bars()
    cut = _cut_at_chan_time(bars, "2026/05/28 06:55")
    run_date = datetime.fromisoformat(cut[-1].timestamp).date().isoformat()
    MarketStore(db).upsert_bars(cut)

    strategy_id = "gold_1m_chan_gate_block"
    summary = MultiStrategyRunner(output_root=root, registry=_registry_for_gate_test(strategy_id, tmp_path)).run(
        run_date, paper_auto_approve=True
    )

    strat = next(s for s in summary["strategies"] if s["strategy_id"] == strategy_id)
    assert strat["status"] == "ok"
    assert strat["executed_ticket"] is None

    ns = root / "strategies" / strategy_id
    position_map = json.loads((ns / "position_maps" / f"{run_date}.json").read_text())[0]
    signal = json.loads((ns / "signals" / f"{run_date}.json").read_text())[0]
    tickets = json.loads((ns / "trade_tickets" / f"{run_date}.json").read_text())
    risk_blocks = json.loads((ns / "risk_blocks" / f"{run_date}.json").read_text())

    assert position_map["primary_timeframe"] == "4h"
    assert position_map["trade_zone"]["near_key_level"] is False
    assert signal["direction"] == "watch"
    assert signal["regime"] == "position_map_block"
    assert tickets == []
    assert any(item["reason"] == "position map gate blocked trading" for item in risk_blocks)
