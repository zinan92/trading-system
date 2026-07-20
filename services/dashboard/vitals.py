"""Risk summary, intraday NAV curve, market-db summary, and data provenance summaries."""

from __future__ import annotations


from services.journal_store import load_json
from services.market_data_access import market_data_repository, uses_independent_datafeed
from services.official_market_data_gate import execution_venue_rows, is_official_broker_ohlc_ready
from services.portfolio_risk import PortfolioRiskState


class VitalsMixin:
    def _risk_summary(self, run_date: str, tickets: list[dict], risk_blocks: list[dict]) -> dict:
        next_ticket = next((item for item in tickets if item.get("asset") == "GOLD"), {})
        requested = float(next_ticket.get("max_loss_pct", 0))
        summary = PortfolioRiskState(self.output_root).summary(run_date, requested)
        if risk_blocks and not next_ticket:
            summary = {**summary, **risk_blocks[-1].get("portfolio_risk", {}), "block_reason": risk_blocks[-1].get("reason", "")}
        return {
            "daily_loss_stop_pct": summary["daily_loss_stop_pct"],
            "used_loss_pct": summary["used_loss_pct"],
            "next_ticket_loss_pct": requested,
            "allows_next_paper_order": bool(next_ticket) and bool(summary["allows_candidate"]),
            "block_reason": summary.get("block_reason", ""),
            "blocked_ticket_id": risk_blocks[-1].get("ticket_id", "") if risk_blocks and not next_ticket else "",
            "blocked_signal_id": risk_blocks[-1].get("signal_id", "") if risk_blocks and not next_ticket else "",
            "candidate_loss_pct": summary.get("candidate_loss_pct", requested),
            "projected_loss_pct": summary.get("projected_loss_pct", summary.get("used_loss_pct")),
            "open_trades": summary.get("open_trades", 0),
        }

    def _load_all_closed_trades(self) -> list[dict]:
        """All closed paper trades across history (paper_trades/closed/*.json),
        deduped by trade_id. Needed so the NAV curve can mark trades that closed
        on earlier days, not just today's run_date."""
        closed_dir = self.output_root / "paper_trades" / "closed"
        if not closed_dir.exists():
            return []
        out: list[dict] = []
        seen: set = set()
        for path in sorted(closed_dir.glob("*.json")):
            rows = load_json(path)
            for trade in rows if isinstance(rows, list) else []:
                tid = trade.get("trade_id") or f"_{len(out)}"
                if tid in seen:
                    continue
                seen.add(tid)
                out.append(trade)
        return out

    def _intraday_nav_curve(
        self,
        bars: list[dict],
        paper_performance: dict,
        open_trades: list[dict],
        paper_account: dict,
        equity_curve: dict,
        closed_trades: list[dict] | None = None,
    ) -> dict:
        starting_equity = float(paper_account.get("starting_equity", 10000) or 10000)
        performance_trades = paper_performance.get("open_trades", []) if paper_performance else []
        open_source = performance_trades or open_trades

        def _is_gold_position(item: dict) -> bool:
            return (
                item.get("symbol", "GOLD") in {"GOLD", "XAU", "XAUUSD"}
                and item.get("entry_price") is not None
                and item.get("quantity") is not None
            )

        open_positions = [t for t in open_source if _is_gold_position(t) and t.get("status", "open") == "open"]
        closed_positions = [t for t in (closed_trades or []) if _is_gold_position(t) and t.get("closed_at")]

        clean_bars = [
            item for item in bars
            if item.get("timestamp") and item.get("close") is not None
        ]
        if not clean_bars:
            return {
                "status": "warn",
                "reason": "no_bars",
                "starting_equity": starting_equity,
                "current_equity": equity_curve.get("current_equity", starting_equity),
                "point_count": 0,
                "points": [],
            }

        points = []
        peak = starting_equity
        max_drawdown = 0.0
        # Mark every trade to market on every bar so the equity curve is a true
        # path (full bar history already capped upstream by chart_display_bars):
        #   - before a trade opens: no contribution
        #   - while open: (bar_close - entry) * qty * direction  → tracks price
        #   - after it closes: its recorded realized_pnl (exact; do NOT recompute
        #     from exit-entry, and do NOT re-subtract execution cost)
        # This is what makes a held position show as a curve, not a flat step.
        for bar in clean_bars:
            close = float(bar.get("close", 0) or 0)
            timestamp = str(bar.get("timestamp", ""))
            t = self._parse_ts(timestamp)
            unrealized = 0.0
            realized = 0.0
            active_count = 0
            for trade in open_positions:
                opened = self._parse_ts(trade.get("opened_at"))
                if opened is not None and t is not None and t < opened:
                    continue
                direction = 1 if trade.get("side", "long") == "long" else -1
                entry = float(trade.get("entry_price", 0) or 0)
                quantity = float(trade.get("quantity", 0) or 0)
                unrealized += (close - entry) * quantity * direction
                active_count += 1
            for trade in closed_positions:
                opened = self._parse_ts(trade.get("opened_at"))
                closed = self._parse_ts(trade.get("closed_at"))
                if opened is not None and t is not None and t < opened:
                    continue
                if closed is not None and t is not None and t >= closed:
                    realized += float(trade.get("realized_pnl", 0) or 0)
                else:
                    direction = 1 if trade.get("side", "long") == "long" else -1
                    entry = float(trade.get("entry_price", 0) or 0)
                    quantity = float(trade.get("quantity", 0) or 0)
                    unrealized += (close - entry) * quantity * direction
                    active_count += 1
            equity = starting_equity + realized + unrealized
            peak = max(peak, equity)
            drawdown = ((equity - peak) / peak) * 100 if peak else 0.0
            max_drawdown = min(max_drawdown, drawdown)
            points.append({
                "timestamp": timestamp,
                "close": round(close, 4),
                "equity": round(equity, 4),
                "unrealized_pnl": round(unrealized, 4),
                "realized_pnl": round(realized, 4),
                "active_trade_count": active_count,
                "drawdown_pct": round(drawdown, 4),
            })
        current = points[-1] if points else {}
        return {
            "status": "pass" if points else "warn",
            "source": "5m_mark_to_market",
            "starting_equity": starting_equity,
            "current_equity": current.get("equity", equity_curve.get("current_equity", starting_equity)),
            "current_drawdown_pct": current.get("drawdown_pct", equity_curve.get("current_drawdown_pct", 0)),
            "max_drawdown_pct": round(max_drawdown, 4),
            "point_count": len(points),
            "points": points,
        }

    def _market_db_summary(self) -> dict:
        independent = uses_independent_datafeed(self.market_db)
        if not independent and not self.market_db.exists():
            return {"path": str(self.market_db), "exists": False, "bars": []}
        coverage = market_data_repository(self.market_db).coverage()
        source_config = self.config.get("market_data_sources", {}).get("gold_5m", {})
        official_providers = set(source_config.get("official_broker_providers", ["broker_csv", "mt5_csv", "ibkr", "oanda"]))
        public_providers = set(source_config.get("public_providers", ["gold-api.com", "yahoo_chart:GC=F"]))
        execution_venue_providers = set(source_config.get("execution_venue_providers", []))
        gold_5m = [item for item in coverage if item["symbol"] == "GOLD" and item["timeframe"] == "5m"]
        return {
            "path": "datafeed" if independent else str(self.market_db),
            "exists": True,
            "backend": "datafeed" if independent else "legacy_test_store",
            "bars": coverage,
            "official_providers": sorted(official_providers),
            "execution_venue_providers": sorted(execution_venue_providers),
            "public_providers": sorted(public_providers),
            "gold_5m_total_rows": sum(item["rows"] for item in gold_5m),
            "gold_5m_imported_rows": sum(
                item["rows"]
                for item in gold_5m
                if item["provider"] != "local_synthetic_seed"
            ),
            "gold_5m_official_rows": sum(item["rows"] for item in gold_5m if item["provider"] in official_providers),
            "gold_5m_execution_venue_rows": sum(item["rows"] for item in gold_5m if item["provider"] in execution_venue_providers),
            "gold_5m_public_rows": sum(item["rows"] for item in gold_5m if item["provider"] in public_providers),
            "gold_5m_synthetic_rows": sum(item["rows"] for item in gold_5m if item["provider"] == "local_synthetic_seed"),
        }

    def _data_provenance_summary(self, preflight: dict, market_db: dict, latest: dict, latest_quote: dict) -> dict:
        latest_price = latest_quote.get("close") if latest_quote else latest.get("close")
        ready_for_live = bool(preflight.get("ready_for_live"))
        ready_for_paper = bool(preflight.get("ready_for_paper"))
        official_rows = int(preflight.get("official_rows") or market_db.get("gold_5m_official_rows") or 0)
        execution_rows = int(preflight.get("execution_venue_rows") or market_db.get("gold_5m_execution_venue_rows") or 0)
        public_rows = int(preflight.get("public_rows") or market_db.get("gold_5m_public_rows") or 0)
        synthetic_rows = int(market_db.get("gold_5m_synthetic_rows") or 0)
        latest_bar_provider = latest.get("provider", "")
        latest_quote_provider = latest_quote.get("provider", "") if latest_quote else ""
        gate_payload = {
            **preflight,
            "official_rows": official_rows,
            "execution_venue_rows": execution_rows,
            "official_broker_providers": preflight.get("official_broker_providers") or market_db.get("official_providers") or [],
        }
        official_live_ready = is_official_broker_ohlc_ready(gate_payload)
        execution_venue_ready = bool(
            ready_for_live
            and not official_live_ready
            and (
                preflight.get("live_data_mode") == "execution_venue"
                or execution_venue_rows(gate_payload) > 0
            )
        )
        display_record = latest if (official_live_ready or execution_venue_ready) else (latest_quote or latest)
        latest_provider = latest_bar_provider if (official_live_ready or execution_venue_ready) else (latest_quote_provider or latest_bar_provider)
        latest_flags = display_record.get("quality_flags", []) if display_record else []
        latest_truth = self._latest_truth_level(latest_provider, latest_flags, preflight, market_db)
        if official_live_ready:
            mode = "LIVE_OFFICIAL"
            label = "official broker OHLC"
            allows_live = True
        elif execution_venue_ready:
            mode = "EXECUTION_VENUE"
            label = "execution venue OHLC"
            allows_live = True
        elif ready_for_paper:
            mode = "PAPER_PUBLIC"
            label = "public snapshot / local paper feed"
            allows_live = False
        else:
            mode = "DATA_BLOCKED"
            label = "market data unavailable"
            allows_live = False
        return {
            "mode": mode,
            "label": label,
            "latest_price": latest_price,
            "latest_provider": latest_provider or preflight.get("latest_provider", ""),
            "latest_bar_provider": latest_bar_provider,
            "latest_quote_provider": latest_quote_provider,
            "latest_timestamp": display_record.get("timestamp", "") if display_record else "",
            "latest_quality_flags": latest_flags,
            "latest_truth_level": latest_truth,
            "latest_is_mock": latest_truth == "mock",
            "latest_is_public": latest_truth == "public",
            "latest_is_official": latest_truth == "official",
            "latest_is_execution_venue": latest_truth == "execution_venue",
            "allows_paper": ready_for_paper,
            "allows_live": allows_live,
            "allows_execution_venue": execution_venue_ready,
            "official_rows": official_rows,
            "execution_venue_rows": execution_rows,
            "public_rows": public_rows,
            "synthetic_rows": synthetic_rows,
            "market_db": market_db.get("path", ""),
            "message": preflight.get("message", ""),
        }

    def _latest_truth_level(self, provider: str, quality_flags: list, preflight: dict, market_db: dict) -> str:
        provider = provider or preflight.get("latest_provider", "")
        official = set(preflight.get("official_broker_providers") or market_db.get("official_providers") or [])
        execution_venue = set(preflight.get("execution_venue_providers") or market_db.get("execution_venue_providers") or [])
        public = set(preflight.get("public_providers") or market_db.get("public_providers") or [])
        flags = set(quality_flags or [])
        if provider in official:
            return "official"
        if provider in execution_venue or "execution_venue_feed" in flags:
            return "execution_venue"
        if provider in {"mock_kline", "local_synthetic_seed"} or "mock" in flags or "synthetic_seed" in flags:
            return "mock"
        if provider in public or "paper_only_market_data" in flags or "live_snapshot" in flags:
            return "public"
        if provider:
            return "unknown"
        return "missing"

    def _performance_summary(self, open_trades: list[dict], closed_trades: list[dict]) -> dict:
        realized = sum(float(item.get("realized_pnl", 0)) for item in closed_trades)
        wins = sum(1 for item in closed_trades if float(item.get("realized_pnl", 0)) > 0)
        losses = sum(1 for item in closed_trades if float(item.get("realized_pnl", 0)) < 0)
        total = len(closed_trades)
        return {
            "open_trades": len(open_trades),
            "closed_trades": total,
            "wins": wins,
            "losses": losses,
            "win_rate": round(wins / total, 4) if total else 0,
            "realized_pnl": round(realized, 4),
        }
