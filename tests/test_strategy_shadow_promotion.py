from __future__ import annotations

from services.strategy_shadow_promotion import evaluate_grid_shadow_promotion


def _row(cycle: str, variant: str, *, trades: int = 50, pnl: float = 10, drawdown: float = 3, cost: float = 1, contract: str = "same", market_input: str | None = None) -> dict:
    return {"cycle_id": cycle, "variant_id": variant, "status": "pass", "scenario_id": f"{cycle}-{variant}", "review": {"evaluation_started_at": f"{cycle}T00:00:00+00:00", "evaluation_ended_at": f"{cycle}T12:00:00+00:00"}, "scenario": {"contracts": {"execution_contract_hash": contract, "fee_contract_hash": contract}, "hashes": {"market_event_hash": market_input or f"market-{cycle}"}}, "metrics": {"trade_count": trades, "realized_pnl": pnl, "max_drawdown": drawdown, "cost": cost}}


def test_grid_shadow_promotion_requires_100_comparable_trades_and_persistence() -> None:
    rows = [_row("A", "production", pnl=5), _row("A", "candidate", pnl=8), _row("B", "production", pnl=5), _row("B", "candidate", pnl=9)]
    result = evaluate_grid_shadow_promotion(rows)
    candidate = result["candidates"][0]
    assert result["status"] == "proposal_ready"
    assert candidate["comparable_trade_count"] == 100
    assert candidate["comparable_period_count"] == 2
    assert candidate["status"] == "proposal_ready"
    assert candidate["metrics"]["max_drawdown_delta"] == 0
    assert candidate["metrics"]["cost_delta"] == 0
    assert [row["cycle_id"] for row in candidate["evidence"]] == ["A", "B"]
    assert candidate["evidence"][0]["contracts"]["fee_contract_hash"] == "same"
    assert candidate["evidence"][0]["input_lineage"] == {
        "baseline_market_event_hash": "market-A",
        "candidate_market_event_hash": "market-A",
    }
    assert candidate["human_confirmation"]["required"] is True
    assert candidate["human_confirmation"]["submits_orders"] is False
    assert result["safety"]["changes_production_plan"] is False


def test_grid_shadow_promotion_rejects_thin_or_incomparable_evidence() -> None:
    rows = [_row("A", "production", contract="base"), _row("A", "candidate", trades=99, pnl=8, contract="other")]
    candidate = evaluate_grid_shadow_promotion(rows)["candidates"][0]
    assert candidate["status"] == "not_comparable"
    assert "A:execution_or_fee_contract_mismatch" in candidate["blockers"]
    assert "cross_period_persistence_insufficient" in candidate["blockers"]
    assert "comparable_trade_sample_insufficient" in candidate["blockers"]


def test_grid_shadow_promotion_rejects_different_market_input_even_with_same_window_and_contract() -> None:
    rows = [
        _row("A", "production", market_input="market-A"),
        _row("A", "candidate", trades=100, pnl=20, market_input="market-B"),
        _row("B", "production", market_input="market-B"),
        _row("B", "candidate", trades=100, pnl=20, market_input="market-C"),
    ]

    candidate = evaluate_grid_shadow_promotion(rows)["candidates"][0]

    assert candidate["status"] == "not_comparable"
    assert candidate["comparable_period_count"] == 0
    assert candidate["metrics"]["realized_pnl_delta"] == 0
    assert "A:market_input_hash_mismatch" in candidate["blockers"]
    assert "B:market_input_hash_mismatch" in candidate["blockers"]
