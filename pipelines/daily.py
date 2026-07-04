from __future__ import annotations

import argparse
import os
from datetime import date, datetime, timezone
from pathlib import Path
from services.run_date import utc_run_date

from schemas.journal import JournalPending
from schemas.market_data import Bar, CleanDatasetManifest
from services.backtest_client import BacktestClient
from services.broker_feed_bridge import BrokerFeedBridge
from services.config_loader import ROOT, load_assets, load_pipeline_config, load_risk_rules, load_strategy_config
from services.copilot_client import CopilotClient
from services.data_cleaner import DataCleaner
from services.data_quality_gate import DataQualityGate
from services.decision_snapshot import DecisionSnapshotBuilder
from services.direction_bias_gate import DirectionBiasGate
from services.intel_client import IntelClient
from services.kline_client import KlineClient
from services.live_env import apply_live_env
from services.market_view_obsidian import MarketViewObsidianSync
from services.pending_entry_guard import build_pending_entry_block, evaluate_pending_entry_state, reject_expired_pending_entries
from services.portfolio_risk import PortfolioRiskState
from services.position_map import GoldPositionMap
from services.reporting import ReportBuilder
from services.risk_engine import RiskEngine
from services.signal_engine import SignalEngine
from services.strategy_registry import StrategyRegistry
from services.strategy_guardrails import StrategyGuardrails
from services.writers import write_outputs


def run_daily_pipeline(run_date: str, strategy=None, output_root=None) -> dict[str, Path]:
    """Run the analysis + ticket pipeline.

    `strategy` (a `Strategy` from the registry) and `output_root` are optional:
    when both are given the pipeline runs that single strategy into that scoped
    namespace (used by MultiStrategyRunner for isolated per-strategy accounts).
    With no args it behaves exactly as before (default strategy, global root).
    """
    pipeline_config = load_pipeline_config()
    if os.getenv("TRADING_ORCHESTRATOR_DISABLE_DATA_QUALITY_GATE"):
        pipeline_config = {**pipeline_config, "data_quality_gate": {**pipeline_config.get("data_quality_gate", {}), "enabled": False}}
    strategy_config = load_strategy_config()
    assets = load_assets()
    env_local_db = os.getenv("TRADING_ORCHESTRATOR_MARKET_DB")
    local_db = env_local_db or pipeline_config.get("local_market_db")
    local_db_path = Path(local_db) if local_db else None
    if local_db_path and not local_db_path.is_absolute() and not env_local_db:
        local_db_path = ROOT / local_db_path
    if output_root is None:
        output_root = Path(os.getenv("TRADING_ORCHESTRATOR_OUTPUT_ROOT", str(ROOT / pipeline_config.get("output_root", "outputs"))))
    else:
        output_root = Path(output_root)
    _sync_market_view_from_obsidian(run_date, output_root)
    BrokerFeedBridge(output_root=output_root, market_db=local_db_path).import_pending(run_date)
    kline = KlineClient(
        base_url=pipeline_config["kline_base_url"],
        fallback_to_mock=bool(pipeline_config.get("fallback_to_mock", True)),
        local_db_path=local_db_path,
        allow_synthetic_seed=bool(pipeline_config.get("allow_synthetic_seed", False)),
        allow_public_snapshot_bar_for_paper=bool(pipeline_config.get("allow_public_snapshot_bar_for_paper", False)),
        gold_backfill=pipeline_config.get("gold_5m_backfill", {}),
    )
    intel = IntelClient(
        base_url=pipeline_config["intel_base_url"],
        fallback_to_mock=bool(pipeline_config.get("intel_fallback_to_mock", True)),
    )
    # Route through the strategy registry. With one enabled strategy this is
    # identical to the legacy single-engine path; it is the seam Phase 3 widens
    # to run every enabled strategy in parallel.
    active_strategy = strategy
    if active_strategy is not None:
        signal_engine = active_strategy.signal_engine()
    else:
        active_strategy = StrategyRegistry(strategy_config).default()
        signal_engine = active_strategy.signal_engine() if active_strategy else SignalEngine(strategy_config)
    risk_engine = RiskEngine(load_risk_rules())
    copilot = CopilotClient(
        base_url=pipeline_config["copilot_base_url"],
        fallback_to_mock=bool(pipeline_config.get("analysis_fallback_to_mock", True)),
    )
    backtest_client = BacktestClient(
        base_url=pipeline_config["backtest_base_url"],
        fallback_to_mock=bool(pipeline_config.get("analysis_fallback_to_mock", True)),
        local_enabled=bool(pipeline_config.get("local_backtest_enabled", True)),
        strategy_config=strategy_config,
    )
    cleaner = DataCleaner()
    data_quality_gate = DataQualityGate(pipeline_config.get("data_quality_gate", {}))
    strategy_guardrails = StrategyGuardrails(output_root)
    position_map_service = GoldPositionMap(output_root)
    direction_bias_gate = DirectionBiasGate(output_root)
    decision_snapshot_builder = DecisionSnapshotBuilder(output_root)
    timeframes = list(pipeline_config.get("timeframes", ["5m"]))
    factor_timeframes = list(pipeline_config.get("factor_timeframes", ["1d"]))
    # Per-strategy timeframe: when a strategy is supplied (multi-strategy runner),
    # the tradable asset is fetched + signalled on THAT strategy's timeframe
    # (chan → 1m), not the global default. With no strategy (legacy global cycle)
    # this is None and behaviour is exactly as before (global `timeframes`).
    strategy_timeframe = str(strategy.timeframe) if strategy is not None and getattr(strategy, "timeframe", None) else None
    tradable_timeframes = [strategy_timeframe] if strategy_timeframe else timeframes
    strategy_signal_cfg = (strategy.params.get("signal", {}) if strategy is not None else {}) or {}
    fetch_bars_override = strategy_signal_cfg.get("fetch_bars")

    signals = []
    analyses = []
    backtests = []
    tickets = []
    journals = []
    risk_blocks = []
    raw_snapshots = {}
    market_events = {}
    clean_bars = {}
    manifests = []
    data_quality = {}
    preserved_pending_ticket_ids: set[str] = set()

    for asset in assets:
        raw_snapshots[asset.symbol] = {}
        clean_bars[asset.symbol] = {}
        asset_timeframes = tradable_timeframes if asset.tradable else factor_timeframes
        for timeframe in asset_timeframes:
            # Fetch enough bars to fill the chart's display window. Signal
            # generation and backtest only ever use their own trailing slices
            # (signal_engine reads candles[-ma_long_bars:]; backtest slices
            # candles[-backtest_lookback_bars:]), so a larger fetch widens the
            # chart without changing any trading behavior. A strategy may set
            # `signal.fetch_bars` to pull deeper history (chan needs days of 1m
            # to surface enough segment-level 二买/类二买).
            if asset.tradable and fetch_bars_override:
                limit = int(fetch_bars_override)
            elif asset.tradable:
                limit = max(
                    int(pipeline_config.get("backtest_lookback_bars", 1000)),
                    int(pipeline_config.get("chart_display_bars", 1000)),
                )
            else:
                limit = 60
            raw = kline.fetch(asset, timeframe=timeframe, limit=limit)
            raw_snapshots[asset.symbol][timeframe] = raw
            cleaned, manifest = cleaner.clean(
                asset=asset,
                timeframe=timeframe,
                bars=raw,
                source_file=f"raw_snapshots/{run_date}/{asset.symbol}_{timeframe}.json",
            )
            clean_bars[asset.symbol][timeframe] = cleaned
            manifests.append(manifest)
            if asset.tradable:
                data_quality[asset.symbol] = data_quality_gate.evaluate(manifest, cleaned)
        events = intel.fetch(asset)
        market_events[asset.symbol] = events

    for asset in assets:
        if asset.tradable:
            _ensure_derived_15m_bars(asset, run_date, clean_bars, manifests)

    factor_context = {
        asset.symbol: {
            "bars": clean_bars.get(asset.symbol, {}).get(factor_timeframes[0], []),
            "events": market_events.get(asset.symbol, []),
            "timeframe": factor_timeframes[0],
        }
        for asset in assets
        if not asset.tradable
    }

    for asset in assets:
        if not asset.tradable:
            continue
        signal_timeframe = strategy_timeframe or ("5m" if "5m" in clean_bars[asset.symbol] else timeframes[0])
        candles = clean_bars[asset.symbol][signal_timeframe]
        pending_entry_state = evaluate_pending_entry_state(output_root, run_date, candles, asset.symbol)
        expired_pending_resolution = reject_expired_pending_entries(output_root, run_date, pending_entry_state)
        if expired_pending_resolution.get("closed") or expired_pending_resolution.get("errors"):
            risk_blocks.append(
                {
                    "asset": asset.symbol,
                    "ticket_id": "",
                    "signal_id": "",
                    "reason": "pending_entry_expired",
                    "pending_entry_expiration": expired_pending_resolution,
                }
            )
        for pending_item in pending_entry_state.get("active_pending", []):
            ticket_id = str(pending_item.get("ticket_id") or "")
            if not ticket_id or ticket_id in preserved_pending_ticket_ids:
                continue
            journal_row = {key: value for key, value in pending_item.items() if key not in {"source", "expiry"}}
            journals.append(JournalPending.from_dict(journal_row))
            preserved_pending_ticket_ids.add(ticket_id)
        events = market_events[asset.symbol]
        signal = signal_engine.generate(asset, candles, events, run_date, factor_context=factor_context)
        quality = data_quality.get(asset.symbol, {})
        if quality and not quality.get("allows_trading", True):
            signal = signal.__class__(
                **{
                    **signal.to_dict(),
                    "status": "no_signal",
                    "direction": "watch",
                    "regime": "data_quality_block",
                    "thesis": f"{asset.symbol} trading blocked by data quality gate.",
                    "evidence": signal.evidence + [f"Data quality block: {'; '.join(quality.get('reasons', []))}"],
                }
            )
        position_gate = {}
        if _position_gate_enabled(active_strategy, asset):
            position_map = position_map_service.build_from_timeframes(
                run_date,
                _strategy_id(active_strategy),
                clean_bars.get(asset.symbol, {}),
            )
            signal, position_gate = _apply_position_gate(signal, position_map, position_map_service, output_root, run_date)
            if position_gate.get("raw_direction") in {"long", "short"} and not position_gate.get("allow_candidate", False):
                risk_blocks.append(
                    {
                        "asset": asset.symbol,
                        "ticket_id": "",
                        "signal_id": signal.signal_id,
                        "reason": "position map gate blocked trading",
                        "position_gate": position_gate,
                        "position_map": str(output_root / "position_maps" / f"{run_date}.json"),
                    }
                )
        raw_direction_before_bias = signal.direction
        current_price = float(candles[-1].close) if candles else None
        signal, direction_bias_decision = direction_bias_gate.apply(
            run_date,
            signal,
            current_price=current_price,
            as_of=signal.generated_at,
        )
        if raw_direction_before_bias in {"long", "short"} and direction_bias_decision.get("action") == "block":
            risk_blocks.append(
                {
                    "asset": asset.symbol,
                    "ticket_id": "",
                    "signal_id": signal.signal_id,
                    "reason": "direction bias gate blocked trading",
                    "direction_bias": direction_bias_decision,
                    "market_view": str(output_root / "market_views" / f"{run_date}.json"),
                }
            )
        analysis = copilot.analyze(signal)
        backtest_bars = candles[-int(pipeline_config.get("backtest_lookback_bars", len(candles))) :]
        backtest = backtest_client.evaluate(signal, analysis, backtest_bars)
        signal = signal.__class__(
            **{
                **signal.to_dict(),
                "methods": analysis.methods,
                "backtest_verdict": backtest.verdict,
            }
        )
        signals.append(signal)
        analyses.append(analysis)
        backtests.append(backtest)
        if pending_entry_state.get("has_active") and signal.direction in {"long", "short"}:
            block = build_pending_entry_block(asset.symbol, signal.signal_id, pending_entry_state)
            risk_blocks.append(block)
            decision_snapshot_builder.record(
                run_date,
                _strategy_id(active_strategy),
                signal_timeframe,
                candles,
                signal,
                direction_bias=direction_bias_decision,
                position_gate=position_gate,
                final_decision="no_go",
                no_go_reason="pending_entry_exists",
                risk_block=block,
            )
            continue
        if quality and not quality.get("allows_trading", True):
            block = {
                "asset": asset.symbol,
                "ticket_id": "",
                "signal_id": signal.signal_id,
                "reason": "data quality gate blocked trading",
                "data_quality": quality,
            }
            risk_blocks.append(block)
            decision_snapshot_builder.record(
                run_date,
                _strategy_id(active_strategy),
                signal_timeframe,
                candles,
                signal,
                direction_bias=direction_bias_decision,
                position_gate=position_gate,
                final_decision="no_go",
                no_go_reason="data quality gate blocked trading",
                risk_block=block,
            )
            continue
        ticket = risk_engine.generate_ticket(signal, candles, analysis, backtest)
        if not ticket and risk_engine.last_rejection:
            risk_blocks.append(risk_engine.last_rejection)
        if not ticket:
            reason = _no_ticket_reason(signal, direction_bias_decision, risk_engine.last_rejection)
            decision_snapshot_builder.record(
                run_date,
                _strategy_id(active_strategy),
                signal_timeframe,
                candles,
                signal,
                direction_bias=direction_bias_decision,
                position_gate=position_gate,
                final_decision="no_go",
                no_go_reason=reason,
                risk_block=risk_engine.last_rejection,
            )
        if ticket:
            guardrail_allowed, guardrail_state = strategy_guardrails.allows_new_paper_order(run_date)
            if not guardrail_allowed:
                block = {
                    "asset": ticket.asset,
                    "ticket_id": ticket.ticket_id,
                    "signal_id": ticket.signal_id,
                    "reason": "strategy guardrails blocked new paper exposure",
                    "strategy_guardrails": guardrail_state,
                }
                risk_blocks.append(block)
                decision_snapshot_builder.record(
                    run_date,
                    _strategy_id(active_strategy),
                    signal_timeframe,
                    candles,
                    signal,
                    direction_bias=direction_bias_decision,
                    position_gate=position_gate,
                    ticket=ticket,
                    final_decision="no_go",
                    no_go_reason="strategy guardrails blocked new paper exposure",
                    risk_block=block,
                )
                continue
            allowed, portfolio_risk = PortfolioRiskState(output_root).should_allow_ticket(run_date, ticket.to_dict())
            if not allowed:
                block = {
                    "asset": ticket.asset,
                    "ticket_id": ticket.ticket_id,
                    "signal_id": ticket.signal_id,
                    "reason": portfolio_risk["block_reason"],
                    "portfolio_risk": portfolio_risk,
                }
                risk_blocks.append(block)
                decision_snapshot_builder.record(
                    run_date,
                    _strategy_id(active_strategy),
                    signal_timeframe,
                    candles,
                    signal,
                    direction_bias=direction_bias_decision,
                    position_gate=position_gate,
                    ticket=ticket,
                    final_decision="no_go",
                    no_go_reason=portfolio_risk["block_reason"],
                    risk_block=block,
                )
                continue
            tickets.append(ticket)
            journals.append(JournalPending.from_ticket(ticket, signal.generated_at))
            decision_snapshot_builder.record(
                run_date,
                _strategy_id(active_strategy),
                signal_timeframe,
                candles,
                signal,
                direction_bias=direction_bias_decision,
                position_gate=position_gate,
                ticket=ticket,
                final_decision="go",
                no_go_reason="",
            )

    paths = write_outputs(
        output_root,
        run_date,
        signals,
        tickets,
        journals,
        analyses,
        backtests,
        raw_snapshots=raw_snapshots,
        market_events=market_events,
        clean_bars=clean_bars,
        manifests=manifests,
        risk_blocks=risk_blocks,
        data_quality=data_quality,
    )
    report_path = ReportBuilder(output_root).build_daily_report(run_date)
    paths["report"] = report_path
    paths["review_notes"] = output_root / "review_notes" / f"{run_date}.md"
    paths["journal"] = output_root / "journals" / f"{run_date}.md"
    return paths


def _strategy_id(strategy) -> str:
    return str(getattr(strategy, "strategy_id", "") or "gold_5m_v1")


def _no_ticket_reason(signal, direction_bias_decision: dict, risk_rejection: dict) -> str:
    if direction_bias_decision.get("action") == "block":
        return "direction bias gate blocked trading"
    if signal.regime == "position_map_block":
        return "position map gate blocked trading"
    if risk_rejection:
        return str(risk_rejection.get("reason", "risk engine rejected candidate"))
    if signal.direction not in {"long", "short"}:
        return "signal is not directional"
    return "signal did not meet risk engine thresholds"


def _position_gate_enabled(strategy, asset) -> bool:
    if os.getenv("TRADING_ORCHESTRATOR_DISABLE_POSITION_GATE"):
        return False
    if not getattr(asset, "tradable", False) or getattr(asset, "symbol", "") != "GOLD":
        return False
    params = getattr(strategy, "params", {}) or {}
    gate = params.get("position_gate", {})
    if isinstance(gate, dict) and "enabled" in gate:
        return bool(gate.get("enabled"))
    return False


def _sync_market_view_from_obsidian(run_date: str, output_root: Path) -> dict:
    apply_live_env()
    obsidian_root = os.getenv("TRADING_ORCHESTRATOR_OBSIDIAN_ROOT", "").strip()
    if not obsidian_root:
        return {"status": "skipped", "reason": "obsidian root not configured"}
    sync = MarketViewObsidianSync(output_root, Path(obsidian_root))
    note_path = sync.note_path(run_date)
    if not note_path.exists():
        return {"status": "skipped", "reason": "obsidian note not found", "note_path": str(note_path)}
    payload = sync.sync(run_date)
    return {"status": "synced", "note_path": str(note_path), "direction_bias": payload.get("direction_bias", "")}


def _ensure_derived_15m_bars(asset, run_date: str, clean_bars: dict, manifests: list) -> None:
    symbol_rows = clean_bars.get(asset.symbol, {})
    if "15m" in symbol_rows:
        return
    source_timeframe = "1m" if symbol_rows.get("1m") else ("5m" if symbol_rows.get("5m") else "")
    if not source_timeframe:
        return
    derived = _aggregate_15m(symbol_rows[source_timeframe])
    if not derived:
        return
    symbol_rows["15m"] = derived
    manifests.append(
        CleanDatasetManifest(
            symbol=asset.symbol,
            timeframe="15m",
            source_files=[f"clean_bars/{run_date}/{asset.symbol}_{source_timeframe}.json"],
            raw_rows=len(symbol_rows[source_timeframe]),
            clean_rows=len(derived),
            missing_bars=0,
            spike_flags=0,
            duplicate_rows=0,
            timezone=asset.timezone,
            generated_at=datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        )
    )


def _aggregate_15m(rows: list[Bar]) -> list[Bar]:
    buckets: dict[str, dict] = {}
    for bar in rows:
        timestamp = _parse_timestamp(bar.timestamp)
        if not timestamp:
            continue
        bucket_start = timestamp.replace(minute=(timestamp.minute // 15) * 15, second=0, microsecond=0)
        key = bucket_start.isoformat()
        bucket = buckets.setdefault(
            key,
            {
                "symbol": bar.symbol,
                "timeframe": "15m",
                "timestamp": key,
                "open": bar.open,
                "high": bar.high,
                "low": bar.low,
                "close": bar.close,
                "volume": 0.0,
                "provider": f"derived_from_{bar.timeframe}",
                "quality_flags": {"derived_timeframe", f"source_timeframe:{bar.timeframe}"},
            },
        )
        bucket["high"] = max(float(bucket["high"]), float(bar.high))
        bucket["low"] = min(float(bucket["low"]), float(bar.low))
        bucket["close"] = bar.close
        bucket["volume"] = float(bucket["volume"]) + float(bar.volume)
        bucket["quality_flags"].update(str(flag) for flag in bar.quality_flags)
    return [
        Bar(
            symbol=str(item["symbol"]),
            timeframe="15m",
            timestamp=str(item["timestamp"]),
            open=float(item["open"]),
            high=float(item["high"]),
            low=float(item["low"]),
            close=float(item["close"]),
            volume=float(item["volume"]),
            provider=str(item["provider"]),
            quality_flags=sorted(item["quality_flags"]),
        )
        for item in sorted(buckets.values(), key=lambda row: row["timestamp"])
    ]


def _parse_timestamp(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _apply_position_gate(signal, position_map: dict, position_map_service: GoldPositionMap, output_root: Path, run_date: str):
    gate = position_map_service.gate_signal(signal.to_dict(), position_map)
    artifact = str(output_root / "position_maps" / f"{run_date}.json")
    source_artifacts = [*list(signal.source_artifacts or [])]
    if artifact not in source_artifacts:
        source_artifacts.append(artifact)
    evidence = [*list(signal.evidence or []), f"Position gate: {gate.get('effective_direction')} / {gate.get('reason', '')}"]
    if signal.direction not in {"long", "short"} or gate.get("allow_candidate", False):
        return signal.__class__(**{**signal.to_dict(), "evidence": evidence, "source_artifacts": source_artifacts}), gate
    reason = gate.get("reason", "position gate blocked trading")
    return signal.__class__(
        **{
            **signal.to_dict(),
            "direction": "watch",
            "status": "no_signal",
            "regime": "position_map_block",
            "strength": 0,
            "confidence": min(signal.confidence, 40),
            "thesis": f"Position gate blocked {signal.direction} signal: {reason}",
            "evidence": evidence,
            "source_artifacts": source_artifacts,
            "backtest_verdict": "no_trade",
        }
    ), gate


def main() -> None:
    parser = argparse.ArgumentParser(description="Run Trading OS daily pipeline.")
    parser.add_argument("--date", default=utc_run_date(), help="Run date in YYYY-MM-DD format.")
    args = parser.parse_args()

    paths = run_daily_pipeline(args.date)
    print("Trading OS daily pipeline completed.")
    for name, path in paths.items():
        print(f"{name}: {path}")


if __name__ == "__main__":
    main()
