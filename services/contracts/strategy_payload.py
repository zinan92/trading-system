"""Strategy detail payload compaction builders for the dashboard API."""

from __future__ import annotations


def compact_strategy_payload(payload: dict) -> dict:
    """Return the trader-facing single-strategy replay payload.

    Full strategy snapshots carry OPS/debug artifacts that can push public
    responses past several MB. The replay view needs Gold OHLC, trades, orders,
    and concise evidence only; `/api/dashboard?strategy=<id>` remains full.
    """
    keep_keys = [
        "contract",
        "run_date",
        "strategy_id",
        "bar_timeframe",
        "latest",
        "latest_quote",
        "data_provenance",
        "ohlc_quality",
        "market_data_gate",
        "dashboard_health",
        "system_vitals",
    ]
    compact = {key: payload.get(key) for key in keep_keys if key in payload}
    detail = payload.get("strategy_detail")
    if isinstance(detail, dict):
        compact["strategy_detail"] = _compact_strategy_detail(detail)
    return compact


def _compact_strategy_detail(detail: dict) -> dict:
    return {
        "strategy_id": detail.get("strategy_id", ""),
        "timeframe": detail.get("timeframe", ""),
        "classification": detail.get("classification", {}),
        "summary": detail.get("summary", {}),
        "bars": [_compact_bar(bar) for bar in detail.get("bars", []) if isinstance(bar, dict)],
        "replay_ohlc": detail.get("replay_ohlc", {}),
        "ohlc_quality": detail.get("ohlc_quality", {}),
        "nav_points": detail.get("nav_points", []),
        "nav_quality": detail.get("nav_quality", {}),
        "nav_curve_intraday": _compact_nav_curve(detail.get("nav_curve_intraday", {})),
        "orders": [_compact_order(order) for order in detail.get("orders", []) if isinstance(order, dict)],
        "open_orders": [_compact_order(order) for order in detail.get("open_orders", []) if isinstance(order, dict)],
        "open_trades": [_compact_trade(trade) for trade in detail.get("open_trades", []) if isinstance(trade, dict)],
        "closed_trades": [_compact_trade(trade) for trade in detail.get("closed_trades", []) if isinstance(trade, dict)],
        "trades": [_compact_trade(trade) for trade in detail.get("trades", []) if isinstance(trade, dict)],
        "trade_record_cards": [
            _compact_trade_record_card(card)
            for card in detail.get("trade_record_cards", [])
            if isinstance(card, dict)
        ],
        "trade_record_audit": detail.get("trade_record_audit", {}),
        "strategy_book": detail.get("strategy_book", {}),
        "edge_judgment": detail.get("edge_judgment", {}),
        "explainability_status": detail.get("explainability_status", ""),
        "explainability_summary": _compact_explainability_summary(detail),
        "performance_confidence": detail.get("performance_confidence", {}),
        "unrealized_pnl": detail.get("unrealized_pnl", 0),
        "entry_reason": detail.get("entry_reason", ""),
        "exit_reason": detail.get("exit_reason", ""),
        "strategy_signal": _compact_signal(detail.get("strategy_signal", {})),
        "latest_decision_snapshot": _compact_decision_snapshot(detail.get("latest_decision_snapshot", {})),
        "latest_go_decision_snapshot": _compact_decision_snapshot(detail.get("latest_go_decision_snapshot", {})),
        "decision_snapshot_summary": detail.get("decision_snapshot_summary", {}),
        "risk_block": _compact_risk_block(detail.get("risk_block", {})),
    }


def _compact_decision_snapshot(snapshot: dict) -> dict:
    if not isinstance(snapshot, dict):
        return {}
    plan = snapshot.get("execution_plan") if isinstance(snapshot.get("execution_plan"), dict) else {}
    signal = snapshot.get("signal") if isinstance(snapshot.get("signal"), dict) else {}
    return {
        "strategy_id": snapshot.get("strategy_id", ""),
        "bar_timestamp": snapshot.get("bar_timestamp", ""),
        "generated_at": snapshot.get("generated_at", ""),
        "final_decision": snapshot.get("final_decision", ""),
        "signal": {
            "direction": signal.get("direction", ""),
            "confidence": signal.get("confidence", ""),
            "strength": signal.get("strength", ""),
            "regime": signal.get("regime", ""),
        },
        "execution_plan": {
            "ticket_id": plan.get("ticket_id", ""),
            "entry_zone": plan.get("entry_zone", ""),
            "take_profit": plan.get("take_profit"),
            "stop_loss": plan.get("stop_loss"),
            "target_equity_return_pct": plan.get("target_equity_return_pct"),
        },
        "no_go_reason": snapshot.get("no_go_reason", ""),
    }


def _compact_explainability_summary(detail: dict) -> dict:
    groups = detail.get("explainability_root_cause_groups") or detail.get("explainability_gap_groups") or []
    compact_groups = []
    total = 0
    warn_total = 0
    info_total = 0
    for group in groups if isinstance(groups, list) else []:
        if not isinstance(group, dict):
            continue
        count = int(group.get("count") or 0)
        warn_count = int(group.get("warn_count") or (count if group.get("severity") == "warn" else 0) or 0)
        info_count = int(group.get("info_count") or max(0, count - warn_count) or 0)
        total += count
        warn_total += warn_count
        info_total += info_count
        compact_groups.append({
            "root_cause": group.get("root_cause") or group.get("key") or "",
            "label": group.get("label") or group.get("key") or "",
            "severity": group.get("severity") or ("warn" if warn_count else "info"),
            "count": count,
            "warn_count": warn_count,
            "info_count": info_count,
            "next_action": group.get("next_action") or "",
            "gap_types": list(group.get("gap_types", []) or [])[:4],
            "sample_trade_ids": list(group.get("sample_trade_ids", []) or [])[:3],
            "sample_ticket_ids": list(group.get("sample_ticket_ids", []) or [])[:3],
            "sample_signal_ids": list(group.get("sample_signal_ids", []) or [])[:3],
        })
    compact_groups = sorted(
        compact_groups,
        key=lambda item: (0 if item["severity"] == "warn" else 1, -int(item["count"] or 0), item["root_cause"]),
    )[:4]
    status = detail.get("explainability_status") or ("warn" if warn_total else "ok")
    return {
        "status": status,
        "verdict": "not_promotion_ready" if warn_total else "reviewable",
        "gap_count": total,
        "warn_count": warn_total,
        "info_count": info_total,
        "root_cause_count": len(compact_groups),
        "groups": compact_groups,
    }


def _compact_nav_curve(curve: dict) -> dict:
    if not isinstance(curve, dict):
        return {}
    return {
        "status": curve.get("status", ""),
        "source": curve.get("source", ""),
        "reason": curve.get("reason", ""),
        "starting_equity": curve.get("starting_equity"),
        "current_equity": curve.get("current_equity"),
        "current_drawdown_pct": curve.get("current_drawdown_pct"),
        "max_drawdown_pct": curve.get("max_drawdown_pct"),
        "point_count": curve.get("point_count", 0),
        "points": [
            {
                key: point.get(key)
                for key in (
                    "timestamp",
                    "close",
                    "equity",
                    "unrealized_pnl",
                    "realized_pnl",
                    "active_trade_count",
                    "drawdown_pct",
                )
                if key in point
            }
            for point in curve.get("points", [])
            if isinstance(point, dict)
        ],
    }


def _compact_bar(bar: dict) -> dict:
    return {
        key: bar.get(key)
        for key in ("timestamp", "open", "high", "low", "close", "provider", "quality_flags")
        if key in bar
    }


def _compact_order(order: dict) -> dict:
    keep = (
        "order_id",
        "ticket_id",
        "status",
        "requested_price",
        "fill_price",
        "quantity",
        "filled_at",
        "rejection_reason",
        "total_cost",
    )
    return {key: order.get(key) for key in keep if key in order}


def _compact_trade(trade: dict) -> dict:
    keep = (
        "trade_id",
        "order_id",
        "ticket_id",
        "signal_id",
        "signal_regime",
        "signal_strength",
        "signal_confidence",
        "symbol",
        "side",
        "status",
        "quantity",
        "entry_price",
        "requested_entry_price",
        "stop_loss",
        "target",
        "opened_at",
        "closed_at",
        "exit_price",
        "realized_pnl",
        "unrealized_pnl",
        "quality_flags",
        "entry_reason",
        "exit_reason",
    )
    compact = {key: trade.get(key) for key in keep if key in trade}
    if isinstance(trade.get("strategy_signal"), dict):
        compact["strategy_signal"] = _compact_signal(trade["strategy_signal"])
    if isinstance(trade.get("risk_block"), dict):
        compact["risk_block"] = _compact_risk_block(trade["risk_block"])
    if isinstance(trade.get("decision"), dict):
        compact["decision"] = _compact_decision(trade["decision"])
    if isinstance(trade.get("exit_decision"), dict):
        compact["exit_decision"] = _compact_exit_decision(trade["exit_decision"])
    if isinstance(trade.get("record_card"), dict):
        compact["record_card"] = _compact_trade_record_card(trade["record_card"])
    return compact


def _compact_trade_record_card(card: dict) -> dict:
    return {
        "schema_version": card.get("schema_version", ""),
        "run_date": card.get("run_date", ""),
        "trade_id": card.get("trade_id", ""),
        "strategy_id": card.get("strategy_id", ""),
        "trader_id": card.get("trader_id", ""),
        "portfolio_id": card.get("portfolio_id", ""),
        "strategy_family": card.get("strategy_family", ""),
        "strategy_variant": card.get("strategy_variant", ""),
        "status": card.get("status", ""),
        "symbol": card.get("symbol", ""),
        "side": card.get("side", ""),
        "quantity": card.get("quantity"),
        "entry": card.get("entry", {}),
        "exit": card.get("exit", {}),
        "protection": card.get("protection", {}),
        "pnl": card.get("pnl", {}),
        "compliance": card.get("compliance", {}),
        "audit": card.get("audit", {}),
        "display": card.get("display", {}),
    }


def _compact_signal(signal: dict) -> dict:
    if not isinstance(signal, dict):
        return {}
    keep = (
        "signal_id",
        "asset",
        "direction",
        "strength",
        "confidence",
        "horizon",
        "thesis",
        "evidence",
        "methods",
        "regime",
        "factor_scores",
        "backtest_verdict",
        "invalid_if",
        "generated_at",
        "expires_at",
        "status",
        "artifact_provenance",
    )
    compact = {key: signal.get(key) for key in keep if key in signal and key != "artifact_provenance"}
    if isinstance(signal.get("artifact_provenance"), dict):
        compact["artifact_provenance"] = _compact_artifact_provenance(signal["artifact_provenance"])
    return compact


def _compact_artifact_provenance(provenance: dict) -> dict:
    keep = (
        "status",
        "repaired_at",
        "target_date",
        "source_trade_id",
        "source_order_id",
        "source_ticket_id",
        "source_signal_id",
        "warning",
    )
    return {key: provenance.get(key) for key in keep if key in provenance}


def _compact_risk_block(block: dict) -> dict:
    if not isinstance(block, dict):
        return {}
    keep = ("status", "blocked", "reason", "message", "checks", "generated_at")
    return {key: block.get(key) for key in keep if key in block}


def _compact_decision(decision: dict) -> dict:
    if not isinstance(decision, dict):
        return {}
    compact = {}
    if "risk_snapshot" in decision:
        compact["risk_snapshot"] = decision.get("risk_snapshot")
    if "decision" in decision:
        compact["decision"] = decision.get("decision")
    if "reason" in decision:
        compact["reason"] = decision.get("reason")
    return compact


def _compact_exit_decision(decision: dict) -> dict:
    if not isinstance(decision, dict):
        return {}
    keep = (
        "status",
        "required_user_action",
        "latest_price",
        "unrealized_pnl",
        "reason",
        "generated_at",
    )
    return {key: decision.get(key) for key in keep if key in decision}
