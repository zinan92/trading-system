from pathlib import Path

from services.journal_store import load_json, write_json
from services.strategy_guardrails import StrategyGuardrails


def test_strategy_guardrails_warns_while_collecting_paper_evidence(tmp_path: Path):
    root = tmp_path / "outputs"
    run_date = "2026-05-26"
    write_json(root / "data_source_preflight" / "current.json", [{"ready_for_paper": True, "latest_provider": "gold-api.com"}])
    write_json(root / "performance" / f"{run_date}.json", [{"summary": {"open_trade_count": 1, "open_unrealized_r": -0.2, "net_pnl_marked": -5}}])
    write_json(root / "learning_ledger" / "current.json", [{"closed_trade_count": 4, "learning_state": "collect_more_paper_trades", "risk_block_count": 0, "review_days": 3}])
    write_json(root / "strategy_change_proposals" / "current.json", [{"status": "hold_parameters"}])
    write_json(root / "paper_trades" / "current.json", [{"trade_id": "t1", "status": "open"}])
    write_json(root / "risk_blocks" / f"{run_date}.json", [])

    result = StrategyGuardrails(root).run(run_date)

    assert result["status"] == "warn"
    assert result["allow_new_paper_order"] is True
    assert any(item["name"] == "learning_state" and item["status"] == "warn" for item in result["checks"])
    assert load_json(root / "strategy_guardrails" / "current.json")[0]["run_date"] == run_date
    assert (root / "strategy_guardrails" / f"{run_date}.md").exists()


def test_strategy_guardrails_blocks_new_exposure_on_deep_open_drawdown(tmp_path: Path):
    root = tmp_path / "outputs"
    run_date = "2026-05-26"
    write_json(root / "data_source_preflight" / "current.json", [{"ready_for_paper": True, "latest_provider": "gold-api.com"}])
    write_json(root / "performance" / f"{run_date}.json", [{"summary": {"open_trade_count": 1, "open_unrealized_r": -1.2, "net_pnl_marked": -80}}])
    write_json(root / "learning_ledger" / "current.json", [{"closed_trade_count": 10, "learning_state": "collect_more_paper_trades", "risk_block_count": 0, "review_days": 5}])
    write_json(root / "strategy_change_proposals" / "current.json", [{"status": "hold_parameters"}])
    write_json(root / "paper_trades" / "current.json", [{"trade_id": "t1", "status": "open"}])
    write_json(root / "risk_blocks" / f"{run_date}.json", [])

    allowed, result = StrategyGuardrails(root).allows_new_paper_order(run_date)

    assert allowed is False
    assert result["status"] == "block"
    assert "Open paper exposure" in result["summary"]["block_reasons"][0]


def test_strategy_guardrails_warns_when_learning_requires_review_but_allows_paper_sampling(tmp_path: Path):
    root = tmp_path / "outputs"
    run_date = "2026-05-26"
    write_json(root / "data_source_preflight" / "current.json", [{"ready_for_paper": True, "latest_provider": "gold-api.com"}])
    write_json(root / "performance" / f"{run_date}.json", [{"summary": {"open_trade_count": 0, "open_unrealized_r": 0, "net_pnl_marked": -20}}])
    write_json(root / "learning_ledger" / "current.json", [{"closed_trade_count": 22, "learning_state": "review_losing_regimes_before_parameter_changes", "risk_block_count": 0, "review_days": 10}])
    write_json(root / "strategy_change_proposals" / "current.json", [{"status": "review_required"}])
    write_json(root / "paper_trades" / "current.json", [])
    write_json(root / "risk_blocks" / f"{run_date}.json", [])

    result = StrategyGuardrails(root).run(run_date)

    assert result["status"] == "warn"
    assert result["allow_new_paper_order"] is True
    assert any(item["name"] == "learning_state" and item["status"] == "warn" for item in result["checks"])
    learning = next(item for item in result["checks"] if item["name"] == "learning_state")
    assert learning["evidence"]["blocks_paper_sampling"] is False


def test_strategy_guardrails_warns_on_non_safety_candidate_blocks(tmp_path: Path):
    root = tmp_path / "outputs"
    run_date = "2026-05-26"
    write_json(root / "data_source_preflight" / "current.json", [{"ready_for_paper": True, "latest_provider": "binance_usdm"}])
    write_json(root / "performance" / f"{run_date}.json", [{"summary": {"open_trade_count": 0, "open_unrealized_r": 0, "net_pnl_marked": 0}}])
    write_json(root / "learning_ledger" / "current.json", [{"closed_trade_count": 4, "learning_state": "collect_more_paper_trades", "risk_block_count": 0, "review_days": 3}])
    write_json(root / "strategy_change_proposals" / "current.json", [{"status": "hold_parameters"}])
    write_json(root / "paper_trades" / "current.json", [])
    write_json(
        root / "risk_blocks" / f"{run_date}.json",
        [
            {"reason": "signal threshold gate blocked trading"},
            {"reason": "trade quality gate blocked trading"},
        ],
    )

    result = StrategyGuardrails(root).run(run_date)

    assert result["status"] == "warn"
    assert result["allow_new_paper_order"] is True
    risk_check = next(item for item in result["checks"] if item["name"] == "risk_blocks")
    assert risk_check["status"] == "warn"
    assert risk_check["evidence"]["hard_safety_blocks"] == 0
