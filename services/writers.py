from __future__ import annotations

import json
from pathlib import Path

from schemas.analysis import Analysis
from schemas.backtest import BacktestEvidence
from schemas.journal import JournalPending
from schemas.market_data import Bar, CleanDatasetManifest, MarketEvent
from schemas.signal import Signal
from schemas.trade_ticket import TradeTicket


def write_json(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rows, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def write_json_data(path: Path, payload: dict | list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _load_rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        # A truncated/corrupt file (e.g. SIGKILL mid-write — write_text is not
        # atomic) must not brick the merge forever. Self-heal by overwriting,
        # the way the pre-merge plain-write writer did.
        return []
    return data if isinstance(data, list) else []


def _merge_artifact_rows(path: Path, rows: list[dict], key: str) -> list[dict]:
    """Preserve same-day audit evidence across repeated strategy runs.

    The strategies job can run many times per day. A later no-signal run must
    not erase the actionable signal/ticket that produced an already-recorded
    paper order. Pending journal remains current-state-only and is not merged.
    """

    by_key: dict[str, dict] = {}
    out: list[dict] = []
    for item in [*_load_rows(path), *rows]:
        if not isinstance(item, dict):
            continue
        item_key = str(item.get(key) or "")
        if not item_key:
            out.append(item)
            continue
        if item_key in by_key:
            by_key[item_key].update(item)
            continue
        row = dict(item)
        by_key[item_key] = row
        out.append(row)
    return out


def write_daily_briefing(
    path: Path,
    run_date: str,
    signals: list[Signal],
    tickets: list[TradeTicket],
    analyses: list[Analysis],
    backtests: list[BacktestEvidence],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    actionable = [signal for signal in signals if signal.status == "new" and signal.direction in {"long", "short"}]
    watch = [signal for signal in signals if signal.status != "new"]
    analysis_by_signal = {item.signal_id: item for item in analyses}
    backtest_by_signal = {item.signal_id: item for item in backtests}

    lines = [
        f"# Trading OS Daily Briefing - {run_date}",
        "",
        "## Market Summary",
        "",
        f"- Assets scanned: {len(signals)}",
        f"- Actionable signals: {len(actionable)}",
        f"- Trade tickets generated: {len(tickets)}",
        f"- Watch/no-signal items: {len(watch)}",
        "",
        "## Signal Queue",
        "",
    ]

    for signal in sorted(signals, key=lambda item: item.strength, reverse=True):
        lines.extend(
            [
                f"### {signal.asset} - {signal.direction.upper()}",
                "",
                f"- Strength: {signal.strength}",
                f"- Confidence: {signal.confidence}",
                f"- Status: {signal.status}",
                f"- Regime: {signal.regime}",
                f"- Thesis: {signal.thesis}",
                f"- Methods: {', '.join(analysis_by_signal.get(signal.signal_id, Analysis('', '', '')).methods) or 'n/a'}",
                f"- Backtest verdict: {backtest_by_signal.get(signal.signal_id, BacktestEvidence('', '', '', 0, 0, 0, 0, 'not_checked')).verdict}",
                f"- Invalid if: {signal.invalid_if or 'n/a'}",
                f"- Factor scores: {signal.factor_scores}",
                "- Evidence:",
            ]
        )
        lines.extend(f"  - {item}" for item in signal.evidence)
        lines.append("")

    lines.extend(["## Trade Tickets", ""])
    if not tickets:
        lines.append("No approved trade tickets today.")
    for ticket in tickets:
        lines.extend(
            [
                f"### {ticket.asset} - {ticket.action}",
                "",
                f"- Entry zone: {ticket.entry_zone}",
                f"- Stop loss: {ticket.stop_loss}",
                f"- Targets: {', '.join(str(target) for target in ticket.targets)}",
                f"- Trade quality: passes={ticket.trade_quality.get('passes')} target_equity_return={ticket.trade_quality.get('target_equity_return_pct')}% reward/risk={ticket.trade_quality.get('reward_to_risk')}",
                f"- Position size: {ticket.position_size_pct}%",
                f"- Max loss: {ticket.max_loss_pct}%",
                f"- Methods: {', '.join(ticket.methods) or 'n/a'}",
                f"- Backtest: {ticket.backtest.get('verdict', 'n/a')} | win rate {ticket.backtest.get('win_rate', 'n/a')} | avg R {ticket.backtest.get('avg_r', 'n/a')}",
                f"- Order: {ticket.order_type} / {ticket.time_in_force} / paper_only={ticket.paper_only}",
                f"- Rationale: {ticket.rationale}",
                f"- Counter-rationale: {ticket.counter_rationale}",
                f"- Manual execution required: {ticket.manual_execution_required}",
                "",
            ]
        )

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_outputs(
    output_root: Path,
    run_date: str,
    signals: list[Signal],
    tickets: list[TradeTicket],
    journals: list[JournalPending],
    analyses: list[Analysis] | None = None,
    backtests: list[BacktestEvidence] | None = None,
    raw_snapshots: dict[str, dict[str, list[Bar]]] | None = None,
    market_events: dict[str, list[MarketEvent]] | None = None,
    clean_bars: dict[str, dict[str, list[Bar]]] | None = None,
    manifests: list[CleanDatasetManifest] | None = None,
    risk_blocks: list[dict] | None = None,
    data_quality: dict[str, dict] | None = None,
) -> dict[str, Path]:
    analyses = analyses or []
    backtests = backtests or []
    raw_snapshots = raw_snapshots or {}
    market_events = market_events or {}
    clean_bars = clean_bars or {}
    manifests = manifests or []
    risk_blocks = risk_blocks or []
    data_quality = data_quality or {}
    paths = {
        "briefing": output_root / "daily_briefings" / f"{run_date}.md",
        "signals": output_root / "signals" / f"{run_date}.json",
        "trade_tickets": output_root / "trade_tickets" / f"{run_date}.json",
        "journal_pending": output_root / "journal_pending" / f"{run_date}.json",
        "analyses": output_root / "analyses" / f"{run_date}.json",
        "backtests": output_root / "backtests" / f"{run_date}.json",
        "clean_manifest": output_root / "clean_bars" / run_date / "manifest.json",
        "risk_blocks": output_root / "risk_blocks" / f"{run_date}.json",
        "data_quality": output_root / "data_quality" / f"{run_date}.json",
    }
    for symbol, timeframe_rows in raw_snapshots.items():
        for timeframe, rows in timeframe_rows.items():
            write_json(output_root / "raw_snapshots" / run_date / f"{symbol}_{timeframe}.json", [bar.to_dict() for bar in rows])
    for symbol, rows in market_events.items():
        write_json(output_root / "raw_snapshots" / run_date / f"{symbol}_events.json", [event.to_dict() for event in rows])
    for symbol, timeframe_rows in clean_bars.items():
        for timeframe, rows in timeframe_rows.items():
            write_json(output_root / "clean_bars" / run_date / f"{symbol}_{timeframe}.json", [bar.to_dict() for bar in rows])
    write_json(paths["clean_manifest"], [manifest.to_dict() for manifest in manifests])
    write_daily_briefing(paths["briefing"], run_date, signals, tickets, analyses, backtests)
    signal_rows = _merge_artifact_rows(paths["signals"], [signal.to_dict() for signal in signals], "signal_id")
    ticket_rows = _merge_artifact_rows(paths["trade_tickets"], [ticket.to_dict() for ticket in tickets], "ticket_id")
    analysis_rows = _merge_artifact_rows(paths["analyses"], [analysis.to_dict() for analysis in analyses], "signal_id")
    backtest_rows = _merge_artifact_rows(paths["backtests"], [backtest.to_dict() for backtest in backtests], "signal_id")
    write_json(paths["signals"], signal_rows)
    write_json(paths["trade_tickets"], ticket_rows)
    write_json(paths["journal_pending"], [journal.to_dict() for journal in journals])
    write_json(paths["analyses"], analysis_rows)
    write_json(paths["backtests"], backtest_rows)
    write_json(paths["risk_blocks"], risk_blocks)
    write_json_data(paths["data_quality"], data_quality)
    return paths
