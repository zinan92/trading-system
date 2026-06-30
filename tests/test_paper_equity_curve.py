from pathlib import Path

from services.journal_store import load_json, write_json
from services.paper_equity_curve import PaperEquityCurve


def test_paper_equity_curve_builds_and_replaces_daily_point(tmp_path: Path):
    root = tmp_path / "outputs"
    write_json(root / "performance" / "2026-05-25.json", [{"generated_at": "2026-05-25T00:00:00+00:00", "summary": {"net_pnl_marked": 120.0, "open_unrealized_r": 0.5, "realized_pnl_all": 0, "unrealized_pnl": 120, "total_execution_costs": 1.5, "open_trade_count": 1, "closed_all_count": 0}}])
    write_json(root / "performance" / "2026-05-26.json", [{"generated_at": "2026-05-26T00:00:00+00:00", "summary": {"net_pnl_marked": -80.0, "open_unrealized_r": -0.3, "realized_pnl_all": 0, "unrealized_pnl": -80, "total_execution_costs": 2.0, "open_trade_count": 1, "closed_all_count": 0}}])

    curve = PaperEquityCurve(root, starting_equity=100_000)
    first = curve.build("2026-05-25")
    second = curve.build("2026-05-26")
    replacement = curve.build("2026-05-26", {"generated_at": "2026-05-26T01:00:00+00:00", "summary": {"net_pnl_marked": -100.0, "open_unrealized_r": -0.4}})

    assert first["current_equity"] == 100120.0
    assert second["point_count"] == 2
    assert replacement["point_count"] == 2
    assert replacement["points"][-1]["equity"] == 99900.0
    assert replacement["points"][-1]["day_start_equity"] == 100120.0
    assert replacement["points"][-1]["daily_pnl"] == -220.0
    assert replacement["daily_pnl_pct"] == -0.2197
    assert replacement["points"][-1]["drawdown_pct"] < 0
    assert replacement["max_drawdown_pct"] < 0
    assert load_json(root / "equity_curve" / "current.json")[0]["point_count"] == 2
    assert (root / "equity_curve" / "2026-05-26.md").exists()


def test_paper_equity_curve_warns_without_performance(tmp_path: Path):
    result = PaperEquityCurve(tmp_path / "outputs").build("2026-05-26")

    assert result["status"] == "warn"
    assert result["current_equity"] == 10000.0
    assert result["daily_pnl_pct"] == 0.0
