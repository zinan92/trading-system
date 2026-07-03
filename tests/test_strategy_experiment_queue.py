from datetime import datetime, timedelta, timezone
from pathlib import Path

from schemas.market_data import Bar
from services.journal_store import load_json, write_json
from services.strategy_experiment_queue import StrategyExperimentQueue


def _write_bars(root: Path, run_date: str, count: int = 260) -> None:
    start = datetime(2026, 5, 26, tzinfo=timezone.utc)
    rows = []
    for index in range(count):
        price = 4500 + (index % 30) * 0.8 + index * 0.03
        rows.append(Bar("GOLD", "5m", (start + timedelta(minutes=5 * index)).isoformat(), price, price + 2, price - 2, price + 0.5, 10, "mt5_csv", ["csv_import"]).to_dict())
    write_json(root / "clean_bars" / run_date / "GOLD_5m.json", rows)


def test_strategy_experiment_queue_builds_paper_only_candidates(tmp_path: Path):
    root = tmp_path / "outputs"
    run_date = "2026-05-26"
    _write_bars(root, run_date)
    write_json(root / "signals" / f"{run_date}.json", [{"signal_id": "sig1", "asset": "GOLD", "asset_class": "commodity", "direction": "long", "strength": 65, "confidence": 60, "horizon": "5m", "thesis": "trend", "regime": "trend_following"}])
    write_json(root / "strategy_reviews" / f"{run_date}.json", [{"summary": "stable"}])
    write_json(root / "learning_ledger" / f"{run_date}.json", [{"learning_state": "stable_keep_parameters", "closed_trade_count": 25}])
    write_json(root / "strategy_change_proposals" / f"{run_date}.json", [{"status": "eligible_for_small_experiment"}])
    write_json(root / "data_source_lineage" / f"{run_date}.json", [{"truth_level": "official_broker", "provider_groups": {"official": {"rows": 260}}}])
    write_json(
        root / "strategy_hypotheses" / f"{run_date}.json",
        [{
            "hypothesis_id": "hyp_20260526_1_position_filter_blocked",
            "strategy_id": "gold_1m_chan",
            "category": "position_filter_blocked",
            "thesis": "Track rejected Chan triggers away from key levels.",
            "metrics_required": ["sample_size", "pnl"],
            "promotion_rule": "explicit gate",
            "auto_apply": False,
        }],
    )

    result = StrategyExperimentQueue(root).build(run_date)

    assert result["status"] == "experiment_ready"
    assert result["strategy_id"] == "gold_1m_macd"
    assert result["paper_only"] is True
    assert result["auto_apply"] is False
    assert result["sample_bars"] == 260
    assert len(result["experiments"]) == 4
    assert result["review_hypotheses"][0]["hypothesis_id"] == "hyp_20260526_1_position_filter_blocked"
    assert result["shadow_hypotheses"][0]["status"] == "queued_shadow"
    assert result["shadow_hypotheses"][0]["auto_apply"] is False
    assert result["best_candidate"]["variant_id"]
    assert result["blockers"] == []
    assert load_json(root / "strategy_experiments" / "current.json")[0]["status"] == "experiment_ready"
    assert (root / "strategy_experiments" / f"{run_date}.md").exists()


def test_strategy_experiment_queue_blocks_promotion_without_trade_sample_or_execution_grade_data(tmp_path: Path):
    root = tmp_path / "outputs"
    run_date = "2026-05-26"
    _write_bars(root, run_date, count=120)
    write_json(root / "signals" / f"{run_date}.json", [{"signal_id": "sig1", "asset": "GOLD", "asset_class": "commodity", "direction": "watch", "strength": 40, "confidence": 50, "horizon": "5m", "thesis": "wait", "regime": "no_trade"}])
    write_json(root / "learning_ledger" / f"{run_date}.json", [{"learning_state": "collect_more_paper_trades", "closed_trade_count": 2}])
    write_json(root / "strategy_change_proposals" / f"{run_date}.json", [{"status": "hold_parameters"}])
    write_json(root / "data_source_lineage" / f"{run_date}.json", [{"truth_level": "public_snapshot", "provider_groups": {"official": {"rows": 0}}}])

    result = StrategyExperimentQueue(root).build(run_date)

    assert result["status"] == "blocked"
    assert result["auto_apply"] is False
    assert {item["name"] for item in result["blockers"]} == {"sample_size", "closed_trade_sample", "execution_grade_data", "proposal_gate"}
