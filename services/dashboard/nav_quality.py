"""Gold NAV bar assembly, NAV/OHLC quality scoring, and the market data gate."""

from __future__ import annotations


from services.journal_store import load_json


class NavQualityMixin:
    def _load_primary_bars(self, run_date: str, strategy_id: str, strategy_config: dict) -> tuple[list[dict], str]:
        configured = "5m"
        if strategy_id:
            configured = str((strategy_config.get(strategy_id, {}) or {}).get("timeframe", "5m"))
        candidates = [configured, "5m", "1m", "15m", "1h", "4h", "1d"]
        seen: set[str] = set()
        for timeframe in candidates:
            if timeframe in seen:
                continue
            seen.add(timeframe)
            rows = load_json(self.output_root / "clean_bars" / run_date / f"GOLD_{timeframe}.json")
            if rows:
                return rows, timeframe
        return [], configured

    def _gold_nav_bars_for_strategy_window(
        self,
        *,
        run_date: str,
        bars: list[dict],
        strategy_rows: list[dict],
    ) -> list[dict]:
        start_ts = None
        for row in strategy_rows:
            for point in row.get("nav_points") or []:
                ts = self._parse_ts(point.get("timestamp") if isinstance(point, dict) else None)
                if ts is not None and (start_ts is None or ts < start_ts):
                    start_ts = ts
        if start_ts is None:
            return bars

        merged: dict[str, dict] = {}
        clean_root = self.output_root / "clean_bars"
        if clean_root.exists():
            for path in sorted(clean_root.glob("*/GOLD_5m.json")):
                if path.parent.name > run_date:
                    continue
                rows = load_json(path)
                if not isinstance(rows, list):
                    continue
                for item in rows:
                    if not isinstance(item, dict):
                        continue
                    timestamp = item.get("timestamp")
                    if not timestamp:
                        continue
                    ts = self._parse_ts(timestamp)
                    if ts is None or ts < start_ts:
                        continue
                    merged[str(timestamp)] = item

        if not merged:
            return bars
        return sorted(merged.values(), key=lambda item: self._parse_ts(item.get("timestamp")) or 0)

    def _gold_nav_from_bars(self, bars: list[dict]) -> dict:
        points = []
        first_close = None
        for item in bars:
            if not isinstance(item, dict) or not item.get("timestamp") or item.get("close") is None:
                continue
            close = float(item.get("close") or 0)
            if close <= 0:
                continue
            if first_close is None:
                first_close = close
            points.append({
                "timestamp": item["timestamp"],
                "close": round(close, 4),
                "nav": round((close / first_close) * 100, 4) if first_close else 100.0,
            })
        current = points[-1] if points else {}
        return {
            "baseline": "gold_buy_and_hold",
            "point_count": len(points),
            "start_close": round(first_close, 4) if first_close is not None else None,
            "current_close": current.get("close"),
            "return_pct": round(current.get("nav", 100) - 100, 4) if points else None,
            "points": points,
        }

    def _nav_quality(
        self,
        points: list,
        bars: list[dict],
        *,
        closed_trade_count: int = 0,
        open_trade_count: int = 0,
    ) -> dict:
        nav_points = self._clean_nav_quality_points(points)
        gold_times = sorted(
            ts
            for ts in (self._parse_ts(item.get("timestamp")) for item in bars if isinstance(item, dict))
            if ts is not None
        )
        overlap_points = nav_points
        clipped_to_gold_window = False
        if gold_times and nav_points:
            start = gold_times[0]
            end = gold_times[-1]
            overlap_points = [item for item in nav_points if start <= item["ts"] <= end]
            clipped_to_gold_window = len(overlap_points) != len(nav_points)
        gaps = [
            nav_points[index]["ts"] - nav_points[index - 1]["ts"]
            for index in range(1, len(nav_points))
            if nav_points[index]["ts"] > nav_points[index - 1]["ts"]
        ]
        median_gap_minutes = None
        cadence = "missing"
        if len(nav_points) == 1:
            cadence = "single_checkpoint"
        elif gaps:
            sorted_gaps = sorted(gaps)
            median_gap_minutes = round(sorted_gaps[len(sorted_gaps) // 2] / 60, 2)
            if median_gap_minutes >= 18 * 60:
                cadence = "daily_checkpoint"
            elif median_gap_minutes >= 45:
                cadence = "sparse_intraday_checkpoint"
            else:
                cadence = "intraday_checkpoint"

        overlap_count = len(overlap_points)
        point_count = len(nav_points)
        can_compare = overlap_count >= 2
        trend_ready = overlap_count >= 4
        if point_count == 0:
            status = "missing_nav"
            tone = "warn"
            confidence = "missing"
            reason = "missing_equity_curve"
        elif not can_compare:
            status = "not_comparable"
            tone = "warn"
            confidence = "low"
            reason = "needs_two_overlap_checkpoints"
        elif not trend_ready:
            status = "low_confidence"
            tone = "warn"
            confidence = "low"
            reason = "fewer_than_four_overlap_checkpoints"
        elif open_trade_count > 0 and closed_trade_count < 1:
            status = "open_pnl_driven"
            tone = "warn"
            confidence = "medium"
            reason = "ranking_depends_on_unrealized_pnl"
        else:
            status = "comparable"
            tone = "ok"
            confidence = "medium" if cadence != "intraday_checkpoint" else "high"
            reason = "same_window_edge_ready"

        return {
            "source": "paper_equity_curve_checkpoints",
            "is_intraday_curve": cadence == "intraday_checkpoint",
            "cadence": cadence,
            "median_gap_minutes": median_gap_minutes,
            "point_count": point_count,
            "overlap_point_count": overlap_count,
            "gold_point_count": len(gold_times),
            "clipped_to_gold_window": clipped_to_gold_window,
            "can_compare": can_compare,
            "trend_ready": trend_ready,
            "recommended_view": "edge_checkpoints",
            "status": status,
            "tone": tone,
            "confidence": confidence,
            "reason": reason,
            "window_start": self._format_epoch(overlap_points[0]["ts"]) if overlap_points else "",
            "window_end": self._format_epoch(overlap_points[-1]["ts"]) if overlap_points else "",
        }

    def _nav_quality_summary(self, strategy_rows: list[dict], bars: list[dict]) -> dict:
        qualities = [row.get("nav_quality", {}) for row in strategy_rows if isinstance(row, dict)]
        gold_times = sorted(
            ts
            for ts in (self._parse_ts(item.get("timestamp")) for item in bars if isinstance(item, dict))
            if ts is not None
        )
        gold_gap_minutes = None
        if len(gold_times) >= 2:
            gaps = [gold_times[index] - gold_times[index - 1] for index in range(1, len(gold_times)) if gold_times[index] > gold_times[index - 1]]
            if gaps:
                sorted_gaps = sorted(gaps)
                gold_gap_minutes = round(sorted_gaps[len(sorted_gaps) // 2] / 60, 2)
        with_nav = [item for item in qualities if item.get("point_count", 0) > 0]
        comparable = [item for item in qualities if item.get("trend_ready")]
        low_confidence = [item for item in qualities if item.get("point_count", 0) > 0 and not item.get("trend_ready")]
        open_pnl_driven = [item for item in qualities if item.get("status") == "open_pnl_driven"]
        return {
            "source": "paper_equity_curve_checkpoints",
            "strategy_count": len(strategy_rows),
            "with_nav_strategy_count": len(with_nav),
            "comparable_strategy_count": len(comparable),
            "low_confidence_strategy_count": len(low_confidence),
            "missing_nav_strategy_count": max(0, len(strategy_rows) - len(with_nav)),
            "open_pnl_driven_strategy_count": len(open_pnl_driven),
            "gold_point_count": len(gold_times),
            "gold_median_gap_minutes": gold_gap_minutes,
            "is_intraday_nav_available": any(item.get("is_intraday_curve") for item in qualities),
            "default_read": "same_window_edge_checkpoints",
            "trader_warning": "strategy_nav_points_are_equity_checkpoints_not_minute_curve",
        }

    def _clean_nav_quality_points(self, points: list) -> list[dict]:
        clean = []
        for item in points or []:
            if not isinstance(item, dict):
                continue
            ts = self._parse_ts(item.get("timestamp"))
            equity = self._safe_float(item.get("equity"))
            if ts is None or equity is None:
                continue
            clean.append({"ts": ts, "timestamp": item.get("timestamp"), "equity": equity})
        return sorted(clean, key=lambda item: item["ts"])

    def _ohlc_quality(
        self,
        run_date: str,
        bars: list[dict],
        data_trust: dict,
        official_feed_receipt: dict,
        data_source_preflight: dict,
        market_db: dict,
        oanda_feed: dict,
        broker_feed_doctor: dict,
    ) -> dict:
        source_config = self.config.get("market_data_sources", {}).get("gold_5m", {})
        official_providers = set(source_config.get("official_broker_providers", ["broker_csv", "mt5_csv", "ibkr", "oanda"]))
        public_providers = set(source_config.get("public_providers", ["gold-api.com", "yahoo_chart:GC=F"]))
        execution_venue_providers = set(source_config.get("execution_venue_providers", ["binance_usdm"]))
        if not bars:
            return {
                "status": "warn",
                "tone": "warn",
                "provider": "",
                "truth_level": "missing",
                "trust_label": "No OHLC / 无K线",
                "action": "Load GOLD OHLC bars before replay or review. / 先加载黄金K线再回放或复盘。",
                "replay_ready": False,
                "promotion_ready": False,
                "bar_count": 0,
                "wide_bar_count": 0,
                "official_rows": self._safe_int(official_feed_receipt.get("official_rows") or data_source_preflight.get("official_rows")),
                "stale_artifacts": self._stale_quality_artifacts(run_date, data_trust, official_feed_receipt),
            }

        latest = bars[-1] if isinstance(bars[-1], dict) else {}
        provider_counts: dict[str, int] = {}
        flags: set[str] = set()
        wide_bars = []
        for item in bars:
            if not isinstance(item, dict):
                continue
            provider = str(item.get("provider") or "unknown")
            provider_counts[provider] = provider_counts.get(provider, 0) + 1
            for flag in item.get("quality_flags") or []:
                flags.add(str(flag))
            high = self._safe_float(item.get("high"))
            low = self._safe_float(item.get("low"))
            close = self._safe_float(item.get("close"))
            if high is not None and low is not None and close and close > 0 and (high - low) / close > 0.01:
                wide_bars.append({
                    "timestamp": item.get("timestamp", ""),
                    "range_pct": round(((high - low) / close) * 100, 4),
                    "open": item.get("open"),
                    "high": item.get("high"),
                    "low": item.get("low"),
                    "close": item.get("close"),
                })
        provider = str(latest.get("provider") or (max(provider_counts, key=provider_counts.get) if provider_counts else "unknown"))
        official_rows = self._safe_int(
            official_feed_receipt.get("official_rows")
            or data_source_preflight.get("official_rows")
            or market_db.get("gold_5m_official_rows")
        )
        execution_venue_rows = self._safe_int(market_db.get("gold_5m_imported_rows")) - self._safe_int(market_db.get("gold_5m_official_rows"))
        execution_venue_source = provider in execution_venue_providers or official_feed_receipt.get("truth_level") == "execution_venue"
        proxy = not execution_venue_source and (provider in public_providers or "public_proxy_feed" in flags)
        if official_rows > 0 and provider in official_providers:
            truth_level = "official_broker"
        elif execution_venue_source:
            truth_level = "execution_venue"
        elif proxy:
            truth_level = "public_proxy"
        elif provider:
            truth_level = "unknown"
        else:
            truth_level = "missing"
        stale_artifacts = self._stale_quality_artifacts(run_date, data_trust, official_feed_receipt)
        replay_ready = bool(bars)
        execution_grade_ready = truth_level in {"official_broker", "execution_venue"}
        promotion_ready = execution_grade_ready and not stale_artifacts
        issues = []
        if proxy:
            issues.append("proxy feed")
        if not execution_grade_ready:
            issues.append("no execution-grade rows")
        if stale_artifacts:
            issues.append("stale quality artifacts")
        if promotion_ready:
            trust_label = "Tradable OHLC / 可交易K线"
            action = "OK for replay, review, and strategy checks. / 可用于回放、复盘和策略检查。"
        elif replay_ready:
            if execution_grade_ready:
                reason_zh = []
                reason_en = []
                if stale_artifacts:
                    reason_zh.append("质量检查过期")
                    reason_en.append("stale quality checks")
                trust_label = f"Execution venue feed; {' + '.join(reason_en) or 'review checks'} / 可执行场所行情；{' + '.join(reason_zh) or '检查未完成'}"
                action = "Binance USDM/execution venue feed is connected; review the listed candle issues before changing exposure or strategy status. / Binance USDM/可执行场所行情已接入；调整仓位或策略状态前，先处理列出的K线问题。"
            else:
                trust_label = "Replay only / 仅用于回放"
                action = "Connect an execution-grade feed before trading decisions. / 交易决策前需接入可执行行情源。"
        else:
            trust_label = "No OHLC / 无K线"
            action = "Load GOLD OHLC bars before replay or review. / 先加载黄金K线再回放或复盘。"
        return {
            "status": "ok" if promotion_ready else "warn",
            "tone": "ok" if promotion_ready else "warn",
            "provider": provider,
            "provider_counts": provider_counts,
            "truth_level": truth_level,
            "trust_label": trust_label,
            "action": action,
            "replay_ready": replay_ready,
            "promotion_ready": promotion_ready,
            "bar_count": len(bars),
            "latest_timestamp": latest.get("timestamp", ""),
            "latest_close": latest.get("close"),
            "quality_flags": sorted(flags),
            "proxy_feed": proxy,
            "wide_bar_count": len(wide_bars),
            "wide_bar_threshold_pct": 1.0,
            "wide_bar_examples": wide_bars[:5],
            "official_rows": official_rows,
            "execution_venue_rows": max(0, execution_venue_rows),
            "execution_grade_ready": execution_grade_ready,
            "oanda_ready": bool(oanda_feed.get("ready")),
            "oanda_missing_env": oanda_feed.get("missing_env", []),
            "broker_csv_valid_files": broker_feed_doctor.get("valid_file_count", 0),
            "stale_artifacts": stale_artifacts,
            "issues": issues,
        }

    @staticmethod
    def _stale_quality_artifacts(run_date: str, data_trust: dict, official_feed_receipt: dict) -> list[dict]:
        stale = []
        for name, payload in (("data_trust", data_trust), ("official_feed_receipt", official_feed_receipt)):
            if not isinstance(payload, dict) or not payload:
                continue
            artifact_date = payload.get("run_date")
            if artifact_date and artifact_date != run_date:
                stale.append({"name": name, "run_date": artifact_date, "expected_run_date": run_date})
        return stale

    def _market_data_gate(
        self,
        run_date: str,
        ohlc_quality: dict,
        official_feed_receipt: dict,
        data_source_preflight: dict,
        data_provenance: dict,
        oanda_feed: dict,
        broker_feed_doctor: dict,
        broker_feed: dict,
    ) -> dict:
        official_rows = self._safe_int(ohlc_quality.get("official_rows") or official_feed_receipt.get("official_rows") or data_source_preflight.get("official_rows"))
        execution_venue_rows = self._safe_int(
            ohlc_quality.get("execution_venue_rows")
            or data_source_preflight.get("execution_venue_rows")
            or ((data_provenance.get("provider_groups") or {}).get("execution_venue") or {}).get("rows")
        )
        wide_bar_count = self._safe_int(ohlc_quality.get("wide_bar_count"))
        broker_valid_files = self._safe_int(broker_feed_doctor.get("valid_file_count"))
        broker_imported_rows = self._safe_int(broker_feed.get("imported_rows") or official_feed_receipt.get("broker_csv", {}).get("imported_rows"))
        oanda_missing = oanda_feed.get("missing_env") or official_feed_receipt.get("oanda_feed", {}).get("missing_env") or []
        stale_artifacts = ohlc_quality.get("stale_artifacts") or []
        execution_grade_ready = bool(ohlc_quality.get("execution_grade_ready")) or official_rows > 0 or execution_venue_rows > 0
        promotion_ready = bool(ohlc_quality.get("promotion_ready"))
        replay_ready = bool(ohlc_quality.get("replay_ready"))
        blockers = []
        if not execution_grade_ready:
            blockers.append("no_execution_grade_ohlc")
        if stale_artifacts:
            blockers.append("stale_quality_artifacts")
        if promotion_ready:
            mode = "promotion_ready"
            trader_label = "可交易行情 / Tradable feed"
            trader_summary = "Gold OHLC is from an execution-grade venue and clean enough for strategy review. / 黄金K线来自可执行行情源，质量足够用于策略复核。"
            trader_action = "Use Trade Replay and closed-trade evidence for strategy review. / 可结合交易回放和平仓样本做策略复核。"
        elif replay_ready:
            mode = "quality_review" if execution_grade_ready else "replay_only"
            if execution_grade_ready:
                reason_zh = []
                reason_en = []
                if stale_artifacts:
                    reason_zh.append("质量检查过期")
                    reason_en.append("stale quality checks")
                trader_label = f"{' + '.join(reason_zh) or '行情检查未完成'} / {' + '.join(reason_en) or 'market-data checks incomplete'}"
                trader_summary = f"Binance USDM/execution venue feed is connected; entries are paused because {' and '.join(reason_en) or 'quality checks are incomplete'}. / Binance USDM/可执行场所行情已接入；暂停新增是因为{'，'.join(reason_zh) or '行情检查未完成'}。"
                trader_action = "Refresh stale quality checks before changing exposure or strategy status. / 调整仓位或策略状态前，先刷新过期质量检查。"
            else:
                trader_label = "仅可回放 / Replay only"
                trader_summary = "Gold OHLC is useful for visual replay, but not execution-grade. / 当前黄金K线可用于视觉回放，但不是可执行行情源。"
                trader_action = "Connect Binance USDM or another execution-grade GOLD feed before trading decisions. / 交易决策前需接入 Binance USDM 或其他可执行黄金行情源。"
        else:
            mode = "missing"
            trader_label = "无K线 / No OHLC"
            trader_summary = "Gold OHLC is missing for this replay. / 当前回放缺少黄金K线。"
            trader_action = "Refresh market data before reading strategy entries and exits. / 先刷新行情再解读策略进出场。"
        ops_actions = list(official_feed_receipt.get("next_actions") or [])
        if not ops_actions:
            if oanda_missing:
                ops_actions.append("Fill OANDA_API_TOKEN and OANDA_ACCOUNT_ID in configs/live.env, then run import_official_feed.")
            if broker_valid_files <= 0:
                ops_actions.append("Put a valid XAUUSD 5m MT5/broker CSV into data/broker_feeds/gold_5m, then run import_official_feed.")
            if not execution_grade_ready:
                ops_actions.append("Connect Binance USDM or another execution-grade GOLD feed before trading decisions.")
        return {
            "run_date": run_date,
            "status": "ok" if promotion_ready else "warn",
            "mode": mode,
            "trader_label": trader_label,
            "trader_summary": trader_summary,
            "trader_action": trader_action,
            "promotion_ready": promotion_ready,
            "replay_ready": replay_ready,
            "provider": ohlc_quality.get("provider", ""),
            "truth_level": ohlc_quality.get("truth_level", ""),
            "official_rows": official_rows,
            "execution_venue_rows": execution_venue_rows,
            "execution_grade_ready": execution_grade_ready,
            "bar_count": self._safe_int(ohlc_quality.get("bar_count")),
            "wide_bar_count": wide_bar_count,
            "wide_bar_threshold_pct": ohlc_quality.get("wide_bar_threshold_pct", 1.0),
            "proxy_feed": bool(ohlc_quality.get("proxy_feed")),
            "quality_flags": ohlc_quality.get("quality_flags", []),
            "stale_artifacts": stale_artifacts,
            "blockers": blockers,
            "oanda": {
                "status": oanda_feed.get("status") or official_feed_receipt.get("oanda_feed", {}).get("status"),
                "ready": bool(oanda_feed.get("ready") or official_feed_receipt.get("oanda_feed", {}).get("ready")),
                "missing_env": oanda_missing,
                "imported_rows": self._safe_int(oanda_feed.get("imported_rows") or official_feed_receipt.get("oanda_feed", {}).get("imported_rows")),
                "instrument": oanda_feed.get("instrument") or official_feed_receipt.get("oanda_feed", {}).get("instrument", "XAU_USD"),
            },
            "broker_csv": {
                "doctor_status": broker_feed_doctor.get("status") or official_feed_receipt.get("broker_csv", {}).get("doctor_status"),
                "input_dir": broker_feed_doctor.get("input_dir") or broker_feed.get("input_dir") or official_feed_receipt.get("broker_csv", {}).get("input_dir", ""),
                "valid_file_count": broker_valid_files,
                "file_count": self._safe_int(broker_feed_doctor.get("file_count") or official_feed_receipt.get("broker_csv", {}).get("file_count")),
                "row_count": self._safe_int(broker_feed_doctor.get("row_count") or official_feed_receipt.get("broker_csv", {}).get("row_count")),
                "imported_rows": broker_imported_rows,
                "latest_timestamp": broker_feed_doctor.get("latest_timestamp") or official_feed_receipt.get("broker_csv", {}).get("latest_timestamp", ""),
            },
            "data_source": {
                "ready_for_paper": bool(data_source_preflight.get("ready_for_paper")),
                "ready_for_live": bool(data_source_preflight.get("ready_for_live")),
                "provenance_mode": data_provenance.get("mode", ""),
                "allows_live": bool(data_provenance.get("allows_live")),
                "latest_provider": data_source_preflight.get("latest_provider") or data_provenance.get("latest_provider", ""),
                "latest_timestamp": data_source_preflight.get("latest_timestamp", ""),
            },
            "ops_actions": ops_actions,
            "commands": [
                "python3 -m pipelines.broker_feed_doctor --date " + run_date,
                "python3 -m pipelines.import_official_feed --date " + run_date,
                "python3 -m pipelines.data_trust --date " + run_date,
            ],
        }
