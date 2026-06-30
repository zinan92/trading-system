from pathlib import Path

from services.journal_store import load_json, write_json
from services.paper_exit_monitor import PaperExitMonitor


def test_paper_exit_monitor_tracks_open_trade_distances(tmp_path: Path):
    root = tmp_path / "outputs"
    run_date = "2026-05-26"
    write_json(root / "clean_bars" / run_date / "GOLD_5m.json", [{"timestamp": "2026-05-26T00:05:00+00:00", "close": 4530, "high": 4535, "low": 4525}])
    write_json(root / "paper_trades" / "current.json", [{"trade_id": "tr1", "ticket_id": "t1", "symbol": "GOLD", "side": "long", "status": "open", "quantity": 2, "entry_price": 4510, "stop_loss": 4490, "target": 4570}])

    result = PaperExitMonitor(root).run(run_date)

    assert result["status"] == "pass"
    assert result["summary"]["open_trades"] == 1
    assert result["summary"]["stop_touched"] == 0
    assert result["summary"]["target_touched"] == 0
    monitor = result["monitors"][0]
    assert monitor["trade_id"] == "tr1"
    assert monitor["latest_price"] == 4530
    assert monitor["distance_to_stop_pct"] > 0
    assert monitor["distance_to_target_pct"] > 0
    assert monitor["unrealized_pnl"] == 40
    assert load_json(root / "paper_exit_monitor" / "current.json")[0]["summary"]["open_trades"] == 1


def test_paper_exit_monitor_alerts_when_stop_touched(tmp_path: Path):
    root = tmp_path / "outputs"
    run_date = "2026-05-26"
    write_json(root / "clean_bars" / run_date / "GOLD_5m.json", [{"timestamp": "2026-05-26T00:05:00+00:00", "close": 4495, "high": 4520, "low": 4488}])
    write_json(root / "paper_trades" / "current.json", [{"trade_id": "tr1", "symbol": "GOLD", "side": "long", "status": "open", "quantity": 1, "entry_price": 4510, "stop_loss": 4490, "target": 4570}])

    result = PaperExitMonitor(root).run(run_date)

    assert result["status"] == "alert"
    assert result["summary"]["stop_touched"] == 1
    assert result["monitors"][0]["stop_touched"] is True


def test_paper_exit_monitor_empty_without_open_trades(tmp_path: Path):
    root = tmp_path / "outputs"
    run_date = "2026-05-26"
    write_json(root / "paper_trades" / "current.json", [])

    result = PaperExitMonitor(root).run(run_date)

    assert result["status"] == "empty"
    assert result["summary"]["open_trades"] == 0
