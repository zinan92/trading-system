from pathlib import Path

from services.decision_trace import DecisionTrace
from services.journal_store import load_json, write_json
from services.trade_ticket_notifier import TradeTicketNotifier


RUN_DATE = "2026-07-04"
STRATEGY_ID = "gold_1m_macd"
TRACE_GATE_NAMES = [
    "信号闸门",
    "数据质量闸门",
    "盈亏比 / 质量闸门",
    "账户风险闸门",
    "自动批准闸门",
]


def _signal(signal_id: str = "sig_gold_filtered", **overrides) -> dict:
    row = {
        "signal_id": signal_id,
        "asset": "GOLD",
        "asset_class": "commodity",
        "direction": "long",
        "strength": 66,
        "confidence": 69,
        "horizon": "intraday",
        "thesis": "test signal",
        "evidence": [],
        "methods": ["macd"],
        "regime": "macd_golden_cross",
        "factor_scores": {},
        "source_artifacts": ["clean_bars/2026-07-04/GOLD_1m.json"],
        "generated_at": "2026-07-04T08:15:00+00:00",
        "expires_at": "2026-07-04T08:20:00+00:00",
        "status": "new",
        "close": 4183.27,
    }
    row.update(overrides)
    return row


def _ticket(signal_id: str = "sig_gold_executed", ticket_id: str = "ticket_gold_executed") -> dict:
    return {
        "ticket_id": ticket_id,
        "signal_id": signal_id,
        "asset": "GOLD",
        "asset_class": "commodity",
        "action": "prepare_buy",
        "entry_zone": "4183.27-4183.27",
        "stop_loss": 4174.90,
        "targets": [4200.01],
        "position_size_pct": 5,
        "max_loss_pct": 0.5,
        "order_type": "limit",
        "time_in_force": "day",
        "paper_only": True,
        "signal_regime": "macd_golden_cross",
        "signal_strength": 66,
        "signal_confidence": 69,
        "manual_execution_required": True,
        "verdict": "approved",
        "trade_quality": {
            "passes": True,
            "reward_to_risk": 2.0,
            "target_price_move_pct": 0.4,
            "stop_price_move_pct": 0.2,
            "target_equity_return_pct": 4.0,
            "stop_equity_risk_pct": 2.0,
            "estimated_account_target_return_pct": 0.2,
            "estimated_account_stop_risk_pct": 0.1,
            "requirements": {"effective_leverage": 10.0},
            "reasons": [],
        },
        "backtest": {"verdict": "supportive", "win_rate": 0.62, "avg_r": 0.39, "sample_size": 58},
        "generated_at": "2026-07-04T08:15:00+00:00",
        "latest_price": 4183.27,
    }


def _context(strategy_root: Path, ticket: dict, decision: dict | None = None) -> dict:
    return {
        "pending": {},
        "decision": decision or {},
        "paper_order": {},
        "demo_request": {},
        "strategy_root": str(strategy_root),
    }


def _write_passing_data_quality(root: Path) -> None:
    write_json(
        root / "data_quality" / f"{RUN_DATE}.json",
        {
            "GOLD": {
                "allows_trading": True,
                "clean_rows": 305,
                "missing_ratio": 0,
                "synthetic_ratio": 0,
                "latest_provider": "paper_sim",
                "reasons": [],
            }
        },
    )


def _expected_trace_flow(expected_flow: list[dict], data_gate_detail: str) -> list[dict]:
    result = [dict(expected_flow[0], step=1, name=TRACE_GATE_NAMES[0])]
    result.append({"step": 2, "name": TRACE_GATE_NAMES[1], "passed": True, "detail": data_gate_detail})
    for gate in expected_flow[1:]:
        mapped = dict(gate)
        mapped["step"] = int(mapped["step"]) + 1
        mapped["name"] = TRACE_GATE_NAMES[mapped["step"] - 1]
        result.append(mapped)
    return result


def test_decision_trace_records_signal_gate_filtered_signal(tmp_path: Path):
    root = tmp_path / "outputs"
    signal = _signal(strength=52, confidence=50)
    _write_passing_data_quality(root)
    write_json(root / "signals" / f"{RUN_DATE}.json", [signal])
    write_json(
        root / "risk_blocks" / f"{RUN_DATE}.json",
        [
            {
                "asset": "GOLD",
                "ticket_id": "",
                "signal_id": signal["signal_id"],
                "reason": "signal threshold gate blocked trading",
                "signal_gate": {
                    "passes": False,
                    "direction": "long",
                    "strength": 52,
                    "confidence": 50,
                    "min_signal_strength": 60,
                    "min_confidence": 55,
                    "direction_passes": True,
                    "strength_passes": False,
                    "confidence_passes": False,
                    "reasons": ["signal strength 52 below minimum 60", "signal confidence 50 below minimum 55"],
                },
            }
        ],
    )

    rows = DecisionTrace(root).build(RUN_DATE, STRATEGY_ID)

    assert len(rows) == 1
    trace = rows[0]
    assert trace["outcome"] == "filtered"
    assert trace["outcome_reason"] == "signal threshold gate blocked trading"
    assert trace["first_fail_step"] == 1
    assert [gate["name"] for gate in trace["gates"]] == TRACE_GATE_NAMES
    assert [gate["passed"] for gate in trace["gates"]] == [False, None, None, None, None]
    assert "强度 52 ≥ 60" in trace["gates"][0]["detail"]
    assert "置信 50 ≥ 55" in trace["gates"][0]["detail"]
    assert trace["signal"] == {"regime": "macd_golden_cross", "strength": 52, "confidence": 50, "close": 4183.27}
    assert load_json(root / "decision_traces" / f"{RUN_DATE}.json") == rows
    assert load_json(root / "decision_traces" / "current.json") == rows


def test_decision_trace_records_data_quality_blocked_signal(tmp_path: Path):
    root = tmp_path / "outputs"
    signal = _signal(
        "sig_gold_data_quality_blocked",
        direction="watch",
        strength=0,
        confidence=0,
        regime="data_quality_block",
    )
    write_json(
        root / "data_quality" / f"{RUN_DATE}.json",
        {
            "GOLD": {
                "allows_trading": False,
                "clean_rows": 180,
                "missing_ratio": 0.08,
                "synthetic_ratio": 0.0,
                "latest_provider": "paper_sim",
                "reasons": ["clean rows 180 below minimum 200"],
            }
        },
    )
    write_json(root / "signals" / f"{RUN_DATE}.json", [signal])
    write_json(
        root / "risk_blocks" / f"{RUN_DATE}.json",
        [
            {
                "asset": "GOLD",
                "ticket_id": "",
                "signal_id": signal["signal_id"],
                "reason": "data quality gate blocked trading",
                "data_quality": {"allows_trading": False, "reasons": ["clean rows 180 below minimum 200"]},
            }
        ],
    )

    rows = DecisionTrace(root).build(RUN_DATE, STRATEGY_ID)

    assert len(rows) == 1
    trace = rows[0]
    assert trace["outcome"] == "filtered"
    assert trace["first_fail_step"] == 2
    assert [gate["name"] for gate in trace["gates"]] == TRACE_GATE_NAMES
    assert [gate["passed"] for gate in trace["gates"]] == [True, False, None, None, None]
    assert "clean rows 180 below minimum 200" in trace["gates"][1]["detail"]


def test_decision_trace_matches_ticket_card_flow_for_executed_ticket(tmp_path: Path):
    root = tmp_path / "outputs"
    signal = _signal("sig_gold_executed")
    ticket = _ticket(signal["signal_id"], "ticket_gold_executed")
    _write_passing_data_quality(root)
    decision = {
        "ticket_id": ticket["ticket_id"],
        "signal_id": signal["signal_id"],
        "decision_status": "executed_paper",
        "decided_at": "2026-07-04T08:16:00+00:00",
        "notes": "auto-approved paper execution",
    }
    write_json(root / "signals" / f"{RUN_DATE}.json", [signal])
    write_json(root / "trade_tickets" / f"{RUN_DATE}.json", [ticket])
    write_json(root / "journal_decisions" / f"{RUN_DATE}.json", [decision])

    rows = DecisionTrace(root).build(RUN_DATE, STRATEGY_ID)
    expected_flow = TradeTicketNotifier(root)._build_view(RUN_DATE, STRATEGY_ID, ticket, _context(root, ticket, decision), 1, 1)["flow"]

    assert len(rows) == 1
    assert rows[0]["outcome"] == "executed"
    assert rows[0]["first_fail_step"] is None
    assert rows[0]["gates"] == _expected_trace_flow(expected_flow, "305 根 K 线　·　缺失 0.0%　·　合成 0.0%　·　paper_sim")


def test_decision_trace_records_auto_rejected_ticket_at_auto_gate(tmp_path: Path):
    root = tmp_path / "outputs"
    signal = _signal("sig_gold_rejected")
    ticket = _ticket(signal["signal_id"], "ticket_gold_rejected")
    _write_passing_data_quality(root)
    decision = {
        "ticket_id": ticket["ticket_id"],
        "signal_id": signal["signal_id"],
        "decision_status": "rejected",
        "decided_at": "2026-07-04T08:16:00+00:00",
        "notes": "auto-rejected (safety): daily loss stop breached",
    }
    write_json(root / "signals" / f"{RUN_DATE}.json", [signal])
    write_json(root / "trade_tickets" / f"{RUN_DATE}.json", [ticket])
    write_json(root / "journal_decisions" / f"{RUN_DATE}.json", [decision])

    rows = DecisionTrace(root).build(RUN_DATE, STRATEGY_ID)
    expected_flow = TradeTicketNotifier(root)._build_view(RUN_DATE, STRATEGY_ID, ticket, _context(root, ticket, decision), 1, 1)["flow"]

    assert rows[0]["outcome"] == "rejected"
    assert rows[0]["outcome_reason"] == "auto-rejected (safety): daily loss stop breached"
    assert rows[0]["first_fail_step"] == 5
    assert rows[0]["gates"] == _expected_trace_flow(expected_flow, "305 根 K 线　·　缺失 0.0%　·　合成 0.0%　·　paper_sim")
    assert rows[0]["gates"][-1]["passed"] is False
    assert "daily loss stop breached" in rows[0]["gates"][-1]["detail"]


def test_decision_trace_keeps_unresolved_ticket_pending(tmp_path: Path):
    root = tmp_path / "outputs"
    signal = _signal("sig_gold_pending")
    ticket = _ticket(signal["signal_id"], "ticket_gold_pending")
    _write_passing_data_quality(root)
    write_json(root / "signals" / f"{RUN_DATE}.json", [signal])
    write_json(root / "trade_tickets" / f"{RUN_DATE}.json", [ticket])

    rows = DecisionTrace(root).build(RUN_DATE, STRATEGY_ID)

    assert rows[0]["outcome"] == "pending"
    assert rows[0]["outcome_reason"] == "ticket reached auto gate but has no executed or rejected decision evidence"
    assert rows[0]["first_fail_step"] is None
    assert rows[0]["gates"][-1]["passed"] is None
    assert rows[0]["gates"][-1]["detail"] == "等待自动清算"


def test_decision_trace_merges_same_day_records_without_clobbering(tmp_path: Path):
    root = tmp_path / "outputs"
    _write_passing_data_quality(root)
    existing = {
        "run_date": RUN_DATE,
        "strategy_id": STRATEGY_ID,
        "cycle_id": "old-cycle",
        "signal_id": "sig_old",
        "gates": [],
    }
    write_json(root / "decision_traces" / f"{RUN_DATE}.json", [existing])
    signal = _signal("sig_gold_executed")
    ticket = _ticket(signal["signal_id"], "ticket_gold_executed")
    decision = {"ticket_id": ticket["ticket_id"], "signal_id": signal["signal_id"], "decision_status": "executed_paper"}
    write_json(root / "signals" / f"{RUN_DATE}.json", [signal])
    write_json(root / "trade_tickets" / f"{RUN_DATE}.json", [ticket])
    write_json(root / "journal_decisions" / f"{RUN_DATE}.json", [decision])

    first = DecisionTrace(root).build(RUN_DATE, STRATEGY_ID)
    second = DecisionTrace(root).build(RUN_DATE, STRATEGY_ID)

    assert [row["signal_id"] for row in first] == ["sig_old", "sig_gold_executed"]
    assert [row["signal_id"] for row in second] == ["sig_old", "sig_gold_executed"]
    assert len(second) == 2
