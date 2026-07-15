"""Trader and ops dashboard payload compaction builders."""

from __future__ import annotations

from services.contracts.strategy_payload import _compact_bar


def compact_trader_payload(payload: dict) -> dict:
    """Return only the reader-facing data needed for dashboard-v3 first paint.

    The full `/api/dashboard` response remains unchanged for OPS and debugging.
    """
    keep_keys = [
        "contract",
        "run_date",
        "strategy_id",
        "bar_timeframe",
        "latest",
        "latest_quote",
        "data_provenance",
        "market_view_status",
        "ohlc_quality",
        "market_data_gate",
        "performance_board",
        "review_loop",
        "dashboard_health",
        "system_vitals",
        "backend_maturity",
        "strategy_frequency",
        "strategy_daily_reviews",
        "daily_trade_samples",
        "trade_reviews",
        "strategy_promotion_gate",
        "live_reconciliation",
        "legacy_live_reconciliation",
        "live_submission_safety",
        "alerts",
        "operation_runbook",
        "source_contracts",
    ]
    compact = {key: payload.get(key) for key in keep_keys if key in payload}
    if isinstance(payload.get("backend_maturity"), dict):
        compact["backend_maturity"] = _compact_backend_maturity(payload["backend_maturity"])
    if isinstance(payload.get("strategy_frequency"), dict):
        compact["strategy_frequency"] = _compact_strategy_frequency(payload["strategy_frequency"])
    if isinstance(payload.get("strategy_daily_reviews"), dict):
        compact["strategy_daily_reviews"] = _compact_strategy_daily_reviews(payload["strategy_daily_reviews"])
    if isinstance(payload.get("daily_trade_samples"), dict):
        compact["daily_trade_samples"] = _compact_daily_trade_samples(payload["daily_trade_samples"])
    if isinstance(payload.get("trade_reviews"), dict):
        compact["trade_reviews"] = _compact_trade_reviews(payload["trade_reviews"])
    if isinstance(payload.get("strategy_promotion_gate"), dict):
        compact["strategy_promotion_gate"] = _compact_strategy_promotion_gate(payload["strategy_promotion_gate"])
    return compact


def _compact_backend_maturity(maturity: dict) -> dict:
    checks = []
    for check in maturity.get("checks", []) or []:
        if not isinstance(check, dict):
            continue
        checks.append(
            {
                "name": check.get("name", ""),
                "status": check.get("status", ""),
                "summary": check.get("summary", ""),
                "evidence": _trim_maturity_evidence(check.get("evidence", {})),
            }
        )
    return {
        "schema_version": maturity.get("schema_version", ""),
        "run_date": maturity.get("run_date", ""),
        "generated_at": maturity.get("generated_at", ""),
        "status": maturity.get("status", ""),
        "summary": maturity.get("summary", {}),
        "checks": checks,
    }


def _trim_maturity_evidence(evidence: object) -> object:
    if not isinstance(evidence, dict):
        return evidence
    trimmed = dict(evidence)
    if isinstance(trimmed.get("board"), dict):
        board = trimmed["board"]
        trimmed["board"] = {
            "stage_counts": board.get("stage_counts", {}),
            "effective_strategy_count": board.get("effective_strategy_count"),
            "needs_attention_count": board.get("needs_attention_count"),
            "bottlenecks": list(board.get("bottlenecks", []) or [])[:8],
        }
    if isinstance(trimmed.get("labels"), dict):
        labels = trimmed["labels"]
        trimmed["labels"] = dict(list(labels.items())[:16])
    return trimmed


def _compact_strategy_frequency(frequency: dict) -> dict:
    strategies = []
    for row in frequency.get("strategies", []) or []:
        if not isinstance(row, dict):
            continue
        classification = row.get("classification", {}) if isinstance(row.get("classification"), dict) else {}
        attribution = row.get("attribution", {}) if isinstance(row.get("attribution"), dict) else {}
        strategies.append(
            {
                "strategy_id": row.get("strategy_id", ""),
                "timeframe": row.get("timeframe", ""),
                "stage": row.get("stage", ""),
                "stage_label": row.get("stage_label", ""),
                "reason": row.get("reason", ""),
                "primary_reason": row.get("primary_reason", ""),
                "limiting_reason": row.get("limiting_reason", ""),
                "executed_trade_count": row.get("executed_trade_count", 0),
                "min_daily_executed_trades": row.get("min_daily_executed_trades"),
                "signal_count": row.get("signal_count", 0),
                "candidate_count": row.get("candidate_count", 0),
                "ticket_count": row.get("ticket_count", 0),
                "sample_statuses": list(row.get("sample_statuses", []) or [])[:20],
                "recommendation": row.get("recommendation", {}) if isinstance(row.get("recommendation"), dict) else {},
                "classification": {
                    "family": classification.get("family", ""),
                    "family_label": classification.get("family_label", ""),
                    "style": classification.get("style", ""),
                    "style_label": classification.get("style_label", ""),
                    "expected_trades_per_day_min": classification.get("expected_trades_per_day_min"),
                    "expected_trades_per_day_max": classification.get("expected_trades_per_day_max"),
                    "role": classification.get("role", ""),
                },
                "attribution": {
                    "primary_reason": attribution.get("primary_reason", ""),
                    "limiting_reason": attribution.get("limiting_reason", ""),
                    "limiting_reason_label": attribution.get("limiting_reason_label", ""),
                    "reason_counts": attribution.get("reason_counts", {}),
                    "evidence": _compact_frequency_evidence(attribution.get("evidence", [])),
                },
            }
        )
    return {
        "schema_version": frequency.get("schema_version", ""),
        "run_date": frequency.get("run_date", ""),
        "generated_at": frequency.get("generated_at", ""),
        "status": frequency.get("status", ""),
        "summary": frequency.get("summary", {}),
        "strategies": strategies,
    }


def _compact_frequency_evidence(evidence: object) -> list[dict]:
    if not isinstance(evidence, list):
        return []
    rows = []
    for item in evidence:
        if not isinstance(item, dict):
            continue
        rows.append(
            {
                "reason": item.get("reason", ""),
                "reason_label": item.get("reason_label", ""),
                "detail": item.get("detail", ""),
                "sample_id": item.get("sample_id", ""),
                "signal_id": item.get("signal_id", ""),
                "signal_generated_at": item.get("signal_generated_at", ""),
                "decision_cursor": item.get("decision_cursor", ""),
                "ticket_id": item.get("ticket_id", ""),
                "execution_status": item.get("execution_status", ""),
            }
        )
        if len(rows) >= 5:
            break
    return rows


def _compact_daily_trade_samples(samples: dict) -> dict:
    summary = samples.get("summary", {}) if isinstance(samples.get("summary"), dict) else {}
    requirements = samples.get("sample_requirements", {}) if isinstance(samples.get("sample_requirements"), dict) else {}
    keep_summary = [
        "observation_count",
        "candidate_count",
        "ticket_count",
        "quality_pass_count",
        "executed_count",
        "executed_trade_sample_count",
        "no_signal_count",
        "blocked_count",
        "candidate_without_ticket_count",
        "pending_review_count",
        "paper_order_count",
        "demo_order_count",
        "live_order_count",
        "below_minimum",
        "target_range_met",
        "target_range",
        "minimum_executed_trades",
        "funnel",
    ]
    keep_requirements = [
        "effective_leverage",
        "min_target_equity_return_pct",
        "min_target_price_move_pct",
        "min_reward_to_risk",
        "daily_min_trade_samples",
        "daily_target_trade_samples_low",
        "daily_target_trade_samples_high",
    ]
    return {
        "run_date": samples.get("run_date", ""),
        "generated_at": samples.get("generated_at", ""),
        "status": samples.get("status", ""),
        "active_strategy_id": samples.get("active_strategy_id", ""),
        "summary": {key: summary.get(key) for key in keep_summary if key in summary},
        "sample_requirements": {key: requirements.get(key) for key in keep_requirements if key in requirements},
    }


def _compact_strategy_daily_reviews(reviews: dict) -> dict:
    rows = []
    for row in reviews.get("strategies", []) or []:
        if not isinstance(row, dict):
            continue
        rows.append(
            {
                "strategy_id": row.get("strategy_id", ""),
                "family": row.get("family", ""),
                "style": row.get("style", ""),
                "timeframe": row.get("timeframe", ""),
                "pm_verdict": row.get("pm_verdict", ""),
                "frequency": row.get("frequency", {}),
                "pnl": row.get("pnl", {}),
                "attribution": row.get("attribution", {}),
                "tp_sl": row.get("tp_sl", {}),
                "pm_action": row.get("pm_action", ""),
                "pm_summary": row.get("pm_summary", ""),
                "review_priority": row.get("review_priority", 0),
                "review_priority_label": row.get("review_priority_label", ""),
                "review_question": row.get("review_question", ""),
                "next_review_cursor": row.get("next_review_cursor", ""),
                "replay_context": row.get("replay_context", {}),
                "sample_quality": row.get("sample_quality", {}),
            }
        )
    return {
        "schema_version": reviews.get("schema_version", ""),
        "run_date": reviews.get("run_date", ""),
        "generated_at": reviews.get("generated_at", ""),
        "strategy_count": reviews.get("strategy_count", len(rows)),
        "status_counts": reviews.get("status_counts", {}),
        "strategies": rows,
    }


def _compact_trade_reviews(reviews: dict) -> dict:
    return {
        "run_date": reviews.get("run_date", ""),
        "generated_at": reviews.get("generated_at", ""),
        "active_strategy_id": reviews.get("active_strategy_id", ""),
        "status": reviews.get("status", ""),
        "summary": reviews.get("summary", {}),
    }


def _compact_strategy_promotion_gate(gate: dict) -> dict:
    return {
        "run_date": gate.get("run_date", ""),
        "generated_at": gate.get("generated_at", ""),
        "status": gate.get("status", ""),
        "strategy_id": gate.get("strategy_id", ""),
        "promotion_allowed": gate.get("promotion_allowed", False),
        "auto_apply": gate.get("auto_apply", False),
        "paper_only": gate.get("paper_only", True),
        "live_config_change_allowed": gate.get("live_config_change_allowed", False),
        "closed_trade_count": gate.get("closed_trade_count"),
        "official_rows": gate.get("official_rows"),
        "data_truth_level": gate.get("data_truth_level", ""),
        "candidate": gate.get("candidate", {}),
        "blockers": list(gate.get("blockers", []) or [])[:8],
    }


def compact_ops_payload(payload: dict) -> dict:
    """Return an OPS first-paint payload.

    The full snapshot is intentionally still available as `view=full`. The OPS
    console should not wait on multi-MB review/detail artifacts before it can
    answer whether local services, data gates, schedules, and public access are
    alive.
    """
    keep_keys = [
        "contract",
        "run_date",
        "strategy_id",
        "bar_timeframe",
        "latest",
        "latest_quote",
        "manifest",
        "signals",
        "backtests",
        "tickets",
        "orders",
        "positions",
        "decisions",
        "pending",
        "review",
        "journal",
        "risk",
        "risk_blocks",
        "open_trades",
        "closed_trades",
        "paper_execution_blocks",
        "performance",
        "paper_performance",
        "paper_exit_monitor",
        "paper_exit_decisions",
        "paper_risk_action_plan",
        "equity_curve",
        "paper_reconciliation",
        "paper_trade_attribution",
        "daily_review",
        "operation_runbook",
        "schedule",
        "schedule_status",
        "schedule_install_plan",
        "schedule_install",
        "schedule_rollback_plan",
        "schedule_rollback",
        "schedule_post_install_verify",
        "schedule_takeover_package",
        "schedule_takeover_package_check",
        "runner",
        "system_vitals",
        "bot_supervisor",
        "bot_checkpoint",
        "market_db",
        "data_provenance",
        "ohlc_quality",
        "market_data_gate",
        "strategy_config",
        "risk_rules",
        "broker_preflight",
        "data_source_preflight",
        "data_source_lineage",
        "data_trust",
        "official_feed_receipt",
        "official_feed_onboarding",
        "broker_feed_doctor",
        "broker_feed",
        "broker_feed_smoke",
        "oanda_feed",
        "oanda_account",
        "binance_usdm_feed",
        "broker_receipts",
        "broker_receipt_summary",
        "mt5_bridge_smoke",
        "live_order_requests",
        "mock_runtime",
        "mock_uat",
        "live_readiness",
        "live_env",
        "live_activation",
        "live_approval",
        "live_submission_safety",
        "live_broker_preflight",
        "live_dry_run_drill",
        "live_switch_plan",
        "live_cutover",
        "strategy_review",
        "strategy_snapshot",
        "learning_ledger",
        "strategy_change_proposal",
        "strategy_learning_actions",
        "strategy_experiments",
        "strategy_improvement_plan",
        "strategy_promotion_gate",
        "strategy_guardrails",
        "risk_monitor",
        "paper_auto_approval_gate",
        "data_quality",
        "data_gaps",
        "data_gap_repair",
        "data_archive",
        "data_integrity",
        "health",
        "audit",
        "doctor",
        "dashboard_health",
        "alerts",
        "live_reconciliation",
    ]
    compact = {key: payload.get(key) for key in keep_keys if key in payload}
    if isinstance(payload.get("data_health"), dict):
        compact["data_health"] = _compact_data_health(payload["data_health"])
    compact["collector_runs"] = _compact_collector_runs(payload.get("collector_runs", []))
    bars = [bar for bar in payload.get("bars", []) if isinstance(bar, dict)]
    compact["bars"] = [_compact_bar(bar) for bar in bars[-500:]]
    compact["ops_payload"] = {
        "mode": "compact",
        "bars_returned": len(compact["bars"]),
        "bars_total": len(bars),
        "truncated_sections": [
            "bars",
            "evening_review",
            "trading_plan",
            "strategy_detail",
            "nav_curve_intraday",
            "performance_board",
            "collector_runs",
            "data_health",
        ],
        "full_diagnostics_query": "/api/dashboard?view=full",
    }
    if isinstance(payload.get("evening_review"), dict):
        compact["evening_review"] = _compact_evening_review(payload["evening_review"])
    if isinstance(payload.get("trading_plan"), dict):
        compact["trading_plan"] = _compact_trading_plan(payload["trading_plan"])
    return compact


def _compact_data_health(data_health: dict) -> dict:
    keep = (
        "run_date",
        "checked_at",
        "symbol",
        "timeframe",
        "status",
        "summary",
        "providers",
        "degenerate",
        "misaligned",
        "provider_conflicts",
        "recommendations",
    )
    compact = {key: data_health.get(key) for key in keep if key in data_health}
    compact["gaps"] = list(data_health.get("gaps", []) or [])[:6]
    compact["issues"] = list(data_health.get("issues", []) or [])[:8]
    compact["suspicious_price_jumps"] = list(data_health.get("suspicious_price_jumps", []) or [])[:8]
    return compact


def _compact_collector_runs(runs: list) -> dict:
    rows = [row for row in (runs or []) if isinstance(row, dict)]
    latest_by_key: dict[tuple[str, str], dict] = {}
    status_counts: dict[str, int] = {}
    stale_count = 0
    for row in rows:
        status = str(row.get("fetch_status") or "unknown")
        status_counts[status] = status_counts.get(status, 0) + 1
        if status == "stale":
            stale_count += 1
        key = (str(row.get("symbol") or ""), str(row.get("timeframe") or ""))
        current = latest_by_key.get(key)
        if not current or str(row.get("collected_at") or "") > str(current.get("collected_at") or ""):
            latest_by_key[key] = row
    latest = sorted(
        latest_by_key.values(),
        key=lambda row: str(row.get("collected_at") or ""),
        reverse=True,
    )
    return {
        "total_runs": len(rows),
        "latest_count": len(latest),
        "stale_count": stale_count,
        "status_counts": status_counts,
        "latest": [_compact_collector_run(row) for row in latest[:12]],
    }


def _compact_collector_run(row: dict) -> dict:
    keep = (
        "collected_at",
        "symbol",
        "timeframe",
        "timestamp",
        "close",
        "provider",
        "quality_flags",
        "record_type",
        "fetch_status",
        "bar_age_seconds",
        "stored_rows_seen",
    )
    return {key: row.get(key) for key in keep if key in row}


def _compact_evening_review(review: dict) -> dict:
    keep = (
        "run_date",
        "generated_at",
        "status",
        "active_strategy_id",
        "adherence",
        "attribution",
        "performance",
        "notes",
    )
    compact = {key: review.get(key) for key in keep if key in review}
    compact["improvement_queue"] = list(review.get("improvement_queue", []) or [])[:6]
    compact["trade_reviews"] = list(review.get("trade_reviews", []) or [])[:6]
    compact["hypotheses"] = list(review.get("hypotheses", []) or [])[:6]
    return compact


def _compact_trading_plan(plan: dict) -> dict:
    keep = (
        "run_date",
        "generated_at",
        "plan_type",
        "active_strategy_id",
        "timeframe",
        "status",
        "mode",
        "decision",
        "allowed_to_trade",
        "blockers",
        "strategy_profile",
        "broker_plan",
        "data_plan",
        "risk_envelope",
        "trade_plan",
        "today_focus",
    )
    compact = {key: plan.get(key) for key in keep if key in plan}
    compact["signals"] = list(plan.get("signals", []) or [])[:8]
    compact["tickets"] = list(plan.get("tickets", []) or [])[:8]
    return compact
