from __future__ import annotations

from pathlib import Path

from services.edge_judgment import EDGE_JUDGMENT_VERSION, EdgeJudgment
from services.journal_store import load_json, write_json


def _closed_trade(i: int, pnl: float, r: float | None = None) -> dict:
    return {
        "trade_id": f"closed_{i}",
        "status": "closed",
        "symbol": "GOLD",
        "side": "long",
        "quantity": 1,
        "entry_price": 100,
        "stop_loss": 95,
        "realized_pnl": pnl,
        **({"realized_r": r} if r is not None else {}),
    }


def test_edge_judgment_labels_insufficient_sample_even_with_large_open_pnl(tmp_path: Path):
    root = tmp_path / "outputs" / "strategies" / "alpha"
    run_date = "2026-06-23"
    write_json(root / "paper_trades" / "closed" / f"{run_date}.json", [_closed_trade(1, 10, 2.0), _closed_trade(2, 12, 2.4)])
    write_json(root / "paper_trades" / "current.json", [{"trade_id": "open_big", "status": "open", "unrealized_pnl": 9999}])
    write_json(root / "performance" / "current.json", [{"summary": {"unrealized_pnl": 9999}}])

    result = EdgeJudgment(root, strategy_id="alpha", min_closed_trades=20).build(run_date)

    assert result["schema_version"] == EDGE_JUDGMENT_VERSION
    assert result["label"] == "insufficient_sample"
    assert result["rankable"] is False
    assert result["ranking_score"] is None
    assert result["metrics"]["open_unrealized_pnl_context"] == 9999
    assert result["audit"]["open_pnl_used_for_label"] is False
    assert result["audit"]["open_pnl_used_for_ranking"] is False


def test_edge_judgment_labels_realized_winner_only_after_threshold(tmp_path: Path):
    root = tmp_path / "outputs" / "strategies" / "winner"
    run_date = "2026-06-23"
    rows = [_closed_trade(i, 6, 1.2) for i in range(1, 22)]
    write_json(root / "paper_trades" / "closed" / f"{run_date}.json", rows)

    result = EdgeJudgment(root, strategy_id="winner", min_closed_trades=20).build(run_date)

    assert result["label"] == "winner"
    assert result["rankable"] is True
    assert result["ranking_score"] == 1.2
    assert result["metrics"]["closed_trade_count"] == 21
    assert load_json(root / "edge_judgment" / "current.json")[0]["label"] == "winner"


def test_edge_judgment_labels_realized_loser_after_threshold(tmp_path: Path):
    root = tmp_path / "outputs" / "strategies" / "loser"
    run_date = "2026-06-23"
    rows = [_closed_trade(i, -5, -1.0) for i in range(1, 21)]
    write_json(root / "paper_trades" / "closed" / f"{run_date}.json", rows)

    result = EdgeJudgment(root, strategy_id="loser", min_closed_trades=20).build(run_date)

    assert result["label"] == "loser"
    assert result["rankable"] is True
    assert result["metrics"]["profit_factor"] == 0.0


def test_edge_judgment_reports_corrupt_closed_trade_file(tmp_path: Path):
    root = tmp_path / "outputs" / "strategies" / "broken"
    run_date = "2026-06-23"
    path = root / "paper_trades" / "closed" / f"{run_date}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not valid json", encoding="utf-8")

    result = EdgeJudgment(root, strategy_id="broken", min_closed_trades=20).build(run_date)

    assert result["audit"]["status"] == "fail"
    assert result["audit"]["read_errors"][0]["path"].endswith(f"closed/{run_date}.json")
