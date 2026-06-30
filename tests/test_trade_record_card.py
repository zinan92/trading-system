from pathlib import Path

from services.dashboard_state import DashboardState
from services.journal_store import write_json
from services.trade_record_card import TRADE_RECORD_CARD_VERSION, TradeRecordCardBuilder


def _strategy_config() -> dict:
    return {
        "trader_id": "trader_breakout",
        "portfolio_id": "pm_gold_breakout",
        "strategy_variant": "range_breakout_v1",
        "classification": {"family": "breakout", "style": "range_breakout"},
    }


def _complete_closed_trade() -> dict:
    return {
        "trade_id": "trade_ok",
        "order_id": "paper_ok",
        "ticket_id": "ticket_ok",
        "signal_id": "sig_ok",
        "strategy_id": "gold_1m_breakout",
        "symbol": "GOLD",
        "side": "long",
        "status": "closed",
        "quantity": 2,
        "entry_price": 100,
        "requested_entry_price": 100,
        "stop_loss": 95,
        "target": 110,
        "opened_at": "2026-06-23T00:00:00+00:00",
        "closed_at": "2026-06-23T01:00:00+00:00",
        "exit_price": 110,
        "exit_reason": "target",
        "realized_pnl": 20,
        "gross_realized_pnl": 20,
        "entry_reason": "range breakout confirmed",
        "source_artifacts": ["signals/2026-06-23.json"],
    }


def test_trade_record_card_is_complete_for_reviewable_closed_trade():
    card = TradeRecordCardBuilder(
        run_date="2026-06-23",
        strategy_id="gold_1m_breakout",
        strategy_config=_strategy_config(),
    ).build(_complete_closed_trade())

    assert card["schema_version"] == TRADE_RECORD_CARD_VERSION
    assert card["trader_id"] == "trader_breakout"
    assert card["portfolio_id"] == "pm_gold_breakout"
    assert card["strategy_family"] == "breakout"
    assert card["entry"]["reason"] == "range breakout confirmed"
    assert card["exit"]["reason"] == "target"
    assert card["protection"]["status"] == "protected"
    assert card["pnl"]["realized_pnl"] == 20
    assert card["pnl"]["r_multiple"] == 2.0
    assert card["pnl"]["hand_check"]["status"] == "pass"
    assert card["pnl"]["hand_check"]["formula"] == "(exit_price 110.0 - entry 100.0) * 1 * qty 2.0 - cost 0.0"
    assert card["pnl"]["hand_check"]["expected_net_pnl"] == 20.0
    assert card["pnl"]["hand_check"]["reported_net_pnl"] == 20.0
    assert card["display"]["pnl_hand_check"] == "PNL math reconciles"
    assert card["compliance"]["verdict"] == "pass"
    assert card["audit"]["status"] == "complete"
    assert card["audit"]["red_line_passed"] is True


def test_open_trade_card_uses_holding_exit_and_unrealized_pnl():
    trade = {
        **_complete_closed_trade(),
        "trade_id": "trade_open",
        "status": "open",
        "closed_at": "",
        "exit_price": None,
        "exit_reason": "",
        "realized_pnl": None,
        "entry_total_cost": 0.5,
        "total_cost": 2.0,
    }
    card = TradeRecordCardBuilder(
        run_date="2026-06-23",
        strategy_id="gold_1m_breakout",
        strategy_config=_strategy_config(),
        latest_price=104,
    ).build(trade)

    assert card["exit"]["reason"] == "holding_open_position"
    assert card["pnl"]["status"] == "unrealized"
    assert card["pnl"]["unrealized_pnl"] == 7.5
    assert card["pnl"]["hand_check"]["status"] == "pass"
    assert card["pnl"]["hand_check"]["reference_price_source"] == "latest_price"
    assert card["pnl"]["hand_check"]["cost"] == 0.5
    assert card["pnl"]["hand_check"]["expected_net_pnl"] == 7.5
    assert card["protection"]["status"] == "protected"
    assert card["audit"]["status"] == "complete"


def test_trade_record_card_fails_when_pnl_hand_check_mismatches():
    trade = {**_complete_closed_trade(), "realized_pnl": 999}
    card = TradeRecordCardBuilder(
        run_date="2026-06-23",
        strategy_id="gold_1m_breakout",
        strategy_config=_strategy_config(),
    ).build(trade)

    assert card["pnl"]["hand_check"]["status"] == "fail"
    assert card["pnl"]["hand_check"]["expected_net_pnl"] == 20.0
    assert card["pnl"]["hand_check"]["reported_net_pnl"] == 999.0
    assert card["compliance"]["verdict"] == "fail"
    assert "pnl_hand_check" in card["audit"]["missing_fields"]


def test_destructive_protection_missing_is_red_not_blank():
    trade = {
        **_complete_closed_trade(),
        "trade_id": "trade_naked",
        "status": "open",
        "stop_loss": None,
        "target": None,
        "closed_at": "",
        "exit_reason": "",
        "realized_pnl": None,
        "protective_order_missing": True,
        "quality_flags": ["protective_order_missing"],
    }
    card = TradeRecordCardBuilder(
        run_date="2026-06-23",
        strategy_id="gold_1m_breakout",
        strategy_config=_strategy_config(),
        latest_price=101,
    ).build(trade)

    assert card["protection"]["status"] == "missing"
    assert card["protection"]["alert_required"] is True
    assert "protection_missing" in card["protection"]["summary"]
    assert card["compliance"]["verdict"] == "fail"
    assert card["audit"]["status"] == "incomplete"
    assert "protection_present" in card["audit"]["missing_fields"]
    assert card["display"]["protection"]


def test_card_exposes_missing_entry_reason_as_audit_failure_not_unknown():
    trade = _complete_closed_trade()
    trade.pop("entry_reason")
    trade.pop("signal_regime", None)
    card = TradeRecordCardBuilder(
        run_date="2026-06-23",
        strategy_id="gold_1m_breakout",
        strategy_config=_strategy_config(),
    ).build(trade)

    assert card["entry"]["reason"] == "missing_entry_reason"
    assert card["audit"]["status"] == "incomplete"
    assert "entry_reason" in card["audit"]["missing_fields"]


def test_dashboard_strategy_detail_exposes_trade_record_cards(tmp_path: Path):
    root = tmp_path / "outputs" / "strategies" / "gold_1m_breakout"
    run_date = "2026-06-23"
    write_json(
        root / "clean_bars" / run_date / "GOLD_1m.json",
        [
            {"timestamp": "2026-06-23T00:00:00+00:00", "open": 100, "high": 100, "low": 100, "close": 100},
            {"timestamp": "2026-06-23T00:01:00+00:00", "open": 101, "high": 101, "low": 101, "close": 101},
        ],
    )
    write_json(root / "paper_trades" / "current.json", [{**_complete_closed_trade(), "status": "open", "closed_at": "", "exit_price": None, "exit_reason": ""}])

    state = DashboardState(output_root=root, market_db=tmp_path / "missing.db").snapshot(run_date)
    detail = state["strategy_detail"]

    assert detail["trade_record_audit"]["trade_count"] == 1
    assert detail["trade_record_cards"][0]["schema_version"] == TRADE_RECORD_CARD_VERSION
    assert detail["trades"][0]["record_card"]["trade_id"] == "trade_ok"
    assert detail["strategy_book"]["schema_version"] == "strategy-book-v1"
    assert detail["strategy_book"]["identity"]["strategy_id"] == "gold_1m_breakout"
