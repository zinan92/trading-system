from pathlib import Path

from services.journal_store import load_json, write_json
from services.paper_reconciliation import PaperReconciliation


def _write_reconciled_fixture(root: Path, run_date: str) -> None:
    write_json(root / "paper_orders" / f"{run_date}.json", [{"order_id": "p1", "ticket_id": "t1", "status": "filled", "fill_price": 100, "requested_price": 100, "quantity": 2}])
    write_json(root / "paper_trades" / "current.json", [{"trade_id": "tr1", "order_id": "p1", "ticket_id": "t1", "symbol": "GOLD", "side": "long", "status": "open", "quantity": 2, "entry_price": 100}])
    write_json(root / "paper_trades" / "closed" / f"{run_date}.json", [])
    (root / "paper_positions").mkdir(parents=True, exist_ok=True)
    (root / "paper_positions" / "current.json").write_text('{"GOLD": {"symbol": "GOLD", "side": "long", "quantity": 2, "avg_price": 100, "unrealized_pnl": 0}}\n', encoding="utf-8")
    write_json(root / "journal_decisions" / f"{run_date}.json", [{"ticket_id": "t1", "decision_status": "executed_paper", "paper_order": {"order_id": "p1"}}])


def test_paper_reconciliation_passes_when_orders_trades_positions_match(tmp_path: Path):
    root = tmp_path / "outputs"
    run_date = "2026-05-26"
    _write_reconciled_fixture(root, run_date)

    result = PaperReconciliation(root).run(run_date)

    assert result["status"] == "pass"
    assert result["computed_positions"]["GOLD"]["quantity"] == 2
    assert load_json(root / "paper_reconciliation" / "current.json")[0]["run_date"] == run_date
    assert (root / "paper_reconciliation" / f"{run_date}.md").exists()


def test_paper_reconciliation_fails_when_filled_order_has_no_trade(tmp_path: Path):
    root = tmp_path / "outputs"
    run_date = "2026-05-26"
    _write_reconciled_fixture(root, run_date)
    write_json(root / "paper_trades" / "current.json", [])

    result = PaperReconciliation(root).run(run_date)

    assert result["status"] == "fail"
    check = next(item for item in result["checks"] if item["name"] == "filled_orders_have_trades")
    assert check["status"] == "fail"
    assert check["evidence"]["missing_order_ids"] == ["p1"]


def _write_accounting_fixture(root: Path, run_date: str, current_equity: float) -> None:
    # Canonical: equity == starting + realized + unrealized (costs already netted
    # into realized/unrealized). starting 10000, realized -300, unrealized -50.
    write_json(root / "performance" / "current.json", [{
        "summary": {
            "realized_pnl_all": -300.0,
            "unrealized_pnl": -50.0,
            "open_costs": 1.0,
            "closed_costs": 2.0,
            "total_execution_costs": 3.0,
        },
    }])
    write_json(root / "equity_curve" / "current.json", [{
        "starting_equity": 10000.0,
        "current_equity": current_equity,
    }])


def test_accounting_invariant_passes_when_equity_reconciles(tmp_path: Path):
    root = tmp_path / "outputs"
    run_date = "2026-05-26"
    _write_accounting_fixture(root, run_date, current_equity=9650.0)  # 10000 - 300 - 50

    result = PaperReconciliation(root).run(run_date)
    check = next(item for item in result["checks"] if item["name"] == "accounting_invariant")

    assert check["status"] == "pass"
    assert check["evidence"]["expected_equity"] == 9650.0
    assert check["evidence"]["equity_drift"] == 0.0
    assert check["evidence"]["cost_drift"] == 0.0


def test_accounting_invariant_fails_on_double_counted_cost(tmp_path: Path):
    root = tmp_path / "outputs"
    run_date = "2026-05-26"
    # Equity that double-subtracts the 3.0 execution cost (the exact class of bug
    # the dashboard NAV curve had): 9650 - 3 = 9647.
    _write_accounting_fixture(root, run_date, current_equity=9647.0)

    result = PaperReconciliation(root).run(run_date)
    check = next(item for item in result["checks"] if item["name"] == "accounting_invariant")

    assert check["status"] == "fail"
    assert check["evidence"]["expected_equity"] == 9650.0
    assert check["evidence"]["actual_equity"] == 9647.0
    assert check["evidence"]["equity_drift"] == -3.0
    assert result["status"] == "fail"


def test_accounting_invariant_absent_without_paper_data(tmp_path: Path):
    # Structural-only reconciliation (no performance/equity artifacts) must not
    # invent an accounting check — keeps it backward compatible.
    root = tmp_path / "outputs"
    run_date = "2026-05-26"
    _write_reconciled_fixture(root, run_date)

    result = PaperReconciliation(root).run(run_date)

    assert all(item["name"] != "accounting_invariant" for item in result["checks"])
    assert result["status"] == "pass"


def test_paper_reconciliation_fails_when_position_quantity_mismatches(tmp_path: Path):
    root = tmp_path / "outputs"
    run_date = "2026-05-26"
    _write_reconciled_fixture(root, run_date)
    (root / "paper_positions" / "current.json").write_text('{"GOLD": {"symbol": "GOLD", "side": "long", "quantity": 1, "avg_price": 100}}\n', encoding="utf-8")

    result = PaperReconciliation(root).run(run_date)

    assert result["status"] == "fail"
    check = next(item for item in result["checks"] if item["name"] == "positions_match_open_trades")
    assert check["status"] == "fail"
    assert check["evidence"]["mismatches"][0]["field"] == "quantity"


def test_paper_reconciliation_accepts_open_trade_from_prior_order_date(tmp_path: Path):
    root = tmp_path / "outputs"
    run_date = "2026-05-27"
    write_json(root / "paper_orders" / "2026-05-26.json", [{"order_id": "p1", "ticket_id": "t1", "status": "filled", "fill_price": 100, "requested_price": 100, "quantity": 2}])
    write_json(root / "paper_orders" / f"{run_date}.json", [])
    write_json(root / "paper_trades" / "current.json", [{"trade_id": "tr1", "order_id": "p1", "ticket_id": "t1", "symbol": "GOLD", "side": "long", "status": "open", "quantity": 2, "entry_price": 100}])
    write_json(root / "paper_trades" / "closed" / f"{run_date}.json", [])
    (root / "paper_positions").mkdir(parents=True, exist_ok=True)
    (root / "paper_positions" / "current.json").write_text('{"GOLD": {"symbol": "GOLD", "side": "long", "quantity": 2, "avg_price": 100}}\n', encoding="utf-8")

    result = PaperReconciliation(root).run(run_date)
    check = next(item for item in result["checks"] if item["name"] == "trades_have_orders")

    assert check["status"] == "pass"
    assert result["summary"]["known_orders"] == 1
    assert result["status"] == "pass"


def test_paper_reconciliation_passes_with_mixed_position_model(tmp_path: Path):
    root = tmp_path / "outputs"
    run_date = "2026-05-27"
    write_json(root / "paper_orders" / "2026-05-26.json", [
        {"order_id": "p1", "ticket_id": "t1", "status": "filled", "fill_price": 100, "requested_price": 100, "quantity": 2},
        {"order_id": "p2", "ticket_id": "t2", "status": "filled", "fill_price": 110, "requested_price": 110, "quantity": 1},
    ])
    write_json(root / "paper_orders" / f"{run_date}.json", [])
    write_json(root / "paper_trades" / "current.json", [
        {"trade_id": "tr1", "order_id": "p1", "ticket_id": "t1", "symbol": "GOLD", "side": "long", "status": "open", "quantity": 2, "entry_price": 100},
        {"trade_id": "tr2", "order_id": "p2", "ticket_id": "t2", "symbol": "GOLD", "side": "short", "status": "open", "quantity": 1, "entry_price": 110},
    ])
    write_json(root / "paper_trades" / "closed" / f"{run_date}.json", [])
    (root / "paper_positions").mkdir(parents=True, exist_ok=True)
    (root / "paper_positions" / "current.json").write_text('{"GOLD": {"symbol": "GOLD", "side": "mixed", "quantity": 3, "avg_price": 103.3333, "net_quantity": 1, "net_side": "long"}}\n', encoding="utf-8")

    result = PaperReconciliation(root).run(run_date)
    check = next(item for item in result["checks"] if item["name"] == "positions_match_open_trades")

    assert check["status"] == "pass"
    expected = check["evidence"]["computed_positions"]["GOLD"]
    assert expected["side"] == "mixed"
    assert expected["sides"] == ["long", "short"]
    assert result["status"] == "pass"
