from pathlib import Path

from services.edge_judgment import EdgeJudgment
from services.journal_store import load_json, write_json
from services.strategy_objective import STRATEGY_OBJECTIVE_VERSION, StrategyObjective


def _closed_trade(i: int, pnl: float, r: float) -> dict:
    return {
        "trade_id": f"closed_{i}",
        "status": "closed",
        "symbol": "GOLD",
        "side": "long",
        "quantity": 1,
        "entry_price": 100,
        "stop_loss": 95,
        "realized_pnl": pnl,
        "realized_r": r,
    }


def test_objective_winner_passes_gate_and_scores_positive(tmp_path: Path):
    root = tmp_path / "outputs" / "strategies" / "winner"
    run_date = "2026-07-03"
    rows = [_closed_trade(i, 6, 1.2) for i in range(1, 22)]
    write_json(root / "paper_trades" / "closed" / f"{run_date}.json", rows)

    result = StrategyObjective(root, strategy_id="winner").build(run_date)

    assert result["schema_version"] == STRATEGY_OBJECTIVE_VERSION
    assert result["strategy_id"] == "winner"
    assert result["scoring_basis"] == "closed_trades_only"
    # J = 1.2 * min(1, 21/20) - 0.1 * |0| = 1.2
    assert result["objective_score"] == 1.2
    gate = result["winner_gate"]
    assert gate["passed"] is True
    assert gate["expectancy_r_positive"] is True
    assert gate["profit_factor_above_1"] is True
    assert gate["sample_sufficient"] is True
    assert result["rankable"] is True
    assert result["audit"]["status"] == "pass"
    assert result["audit"]["open_pnl_used_for_score"] is False


def test_objective_thin_positive_sample_is_damped_and_never_promoted(tmp_path: Path):
    """Red line: a hot streak on 5 trades must not pass the winner gate."""
    root = tmp_path / "outputs" / "strategies" / "thin"
    run_date = "2026-07-03"
    rows = [_closed_trade(i, 10, 2.0) for i in range(1, 6)]
    write_json(root / "paper_trades" / "closed" / f"{run_date}.json", rows)

    result = StrategyObjective(root, strategy_id="thin").build(run_date)

    # J = 2.0 * (5/20) - 0 = 0.5 : damped by sample fraction
    assert result["objective_score"] == 0.5
    assert result["metrics"]["sample_fraction"] == 0.25
    gate = result["winner_gate"]
    assert gate["expectancy_r_positive"] is True
    assert gate["profit_factor_above_1"] is True
    assert gate["sample_sufficient"] is False
    assert gate["passed"] is False
    assert result["rankable"] is False


def test_objective_negative_expectancy_with_drawdown_penalty(tmp_path: Path):
    root = tmp_path / "outputs" / "strategies" / "macdlike"
    run_date = "2026-07-03"
    rows = [_closed_trade(i, -16, -1.0) for i in range(1, 18)]
    rows += [_closed_trade(100 + i, 32, 2.0) for i in range(2)]
    write_json(root / "paper_trades" / "closed" / f"{run_date}.json", rows)
    write_json(root / "equity_curve" / "current.json", [{"max_drawdown_pct": -2.25}])

    result = StrategyObjective(root, strategy_id="macdlike").build(run_date)

    # expectancy = (17*-1 + 2*2)/19 = -0.6842 ; fraction = 19/20 = 0.95
    # penalty = 0.1 * 2.25 = 0.225 ; J = -0.6842*0.95 - 0.225 = -0.875
    assert result["objective_score"] == -0.875
    assert result["metrics"]["drawdown_penalty"] == 0.225
    assert result["winner_gate"]["passed"] is False
    assert result["rankable"] is False


def test_objective_drawdown_weight_is_configurable(tmp_path: Path):
    root = tmp_path / "outputs" / "strategies" / "weighted"
    run_date = "2026-07-03"
    rows = [_closed_trade(i, 6, 1.0) for i in range(1, 21)]
    write_json(root / "paper_trades" / "closed" / f"{run_date}.json", rows)
    write_json(root / "equity_curve" / "current.json", [{"max_drawdown_pct": -5.0}])

    result = StrategyObjective(root, strategy_id="weighted", drawdown_weight=0.2).build(run_date)

    # J = 1.0 * 1.0 - 0.2 * 5.0 = 0.0
    assert result["objective_score"] == 0.0
    assert result["metrics"]["drawdown_weight"] == 0.2
    assert result["metrics"]["drawdown_penalty"] == 1.0


def test_objective_corrupt_input_flags_audit_fail_without_raising(tmp_path: Path):
    root = tmp_path / "outputs" / "strategies" / "corrupt"
    run_date = "2026-07-03"
    rows = [_closed_trade(i, 6, 1.0) for i in range(1, 21)]
    write_json(root / "paper_trades" / "closed" / f"{run_date}.json", rows)
    (root / "equity_curve").mkdir(parents=True, exist_ok=True)
    (root / "equity_curve" / "current.json").write_text("{not json", encoding="utf-8")

    result = StrategyObjective(root, strategy_id="corrupt").build(run_date)

    assert result["audit"]["status"] == "fail"
    assert result["audit"]["read_errors"]
    # score still computed with drawdown defaulting to 0
    assert result["objective_score"] == 1.0


def test_objective_no_trades_scores_zero_and_fails_gate(tmp_path: Path):
    root = tmp_path / "outputs" / "strategies" / "empty"
    result = StrategyObjective(root, strategy_id="empty").build("2026-07-03")

    assert result["objective_score"] == 0.0
    assert result["winner_gate"]["passed"] is False
    assert result["metrics"]["closed_trade_count"] == 0


def test_objective_persists_current_and_dated_artifacts(tmp_path: Path):
    root = tmp_path / "outputs" / "strategies" / "persisted"
    run_date = "2026-07-03"
    write_json(root / "paper_trades" / "closed" / f"{run_date}.json", [_closed_trade(1, 6, 1.0)])

    payload = StrategyObjective(root, strategy_id="persisted").build(run_date)

    current = load_json(root / "strategy_objective" / "current.json")
    dated = load_json(root / "strategy_objective" / f"{run_date}.json")
    assert isinstance(current, list) and len(current) == 1
    assert current[0]["objective_score"] == payload["objective_score"]
    assert dated[0]["schema_version"] == STRATEGY_OBJECTIVE_VERSION


def test_objective_persist_false_writes_nothing(tmp_path: Path):
    root = tmp_path / "outputs" / "strategies" / "readonly"
    run_date = "2026-07-03"
    write_json(root / "paper_trades" / "closed" / f"{run_date}.json", [_closed_trade(1, 6, 1.0)])

    StrategyObjective(root, strategy_id="readonly").build(run_date, persist=False)

    assert not (root / "strategy_objective").exists()


def test_objective_reuses_prebuilt_edge_judgment_payload(tmp_path: Path):
    root = tmp_path / "outputs" / "strategies" / "reuse"
    run_date = "2026-07-03"
    rows = [_closed_trade(i, 4, 0.5) for i in range(1, 26)]
    write_json(root / "paper_trades" / "closed" / f"{run_date}.json", rows)
    write_json(root / "equity_curve" / "current.json", [{"max_drawdown_pct": -4.0}])
    edge = EdgeJudgment(root, strategy_id="reuse").build(run_date, persist=False)

    result = StrategyObjective(root, strategy_id="reuse").build(run_date, edge_judgment=edge)

    # J = 0.5 * 1.0 - 0.1 * 4.0 = 0.1
    assert result["objective_score"] == 0.1
    assert result["winner_gate"]["passed"] is True
    assert result["metrics"]["closed_trade_count"] == 25
