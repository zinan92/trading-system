from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

from services.config_loader import ROOT, load_pipeline_config
from services.journal_store import load_json, write_json
from services.market_store import MarketStore


class DataSourcePreflight:
    def __init__(
        self,
        output_root: Path | None = None,
        market_db: Path | None = None,
        checked_at: datetime | None = None,
        symbol: str = "GOLD",
        timeframe: str = "5m",
    ) -> None:
        config = load_pipeline_config()
        self.config = config
        self.output_root = output_root or Path(os.getenv("TRADING_ORCHESTRATOR_OUTPUT_ROOT", str(ROOT / config.get("output_root", "outputs"))))
        self.market_db = market_db or Path(os.getenv("TRADING_ORCHESTRATOR_MARKET_DB", str(ROOT / config.get("local_market_db", "data/market_data.db"))))
        # The freshness/price-sanity thresholds are the GOLD instrument's, shared
        # across its timeframes — a per-strategy 1m run reuses them. `symbol`/
        # `timeframe` only select WHICH clean-bar series + coverage to read, so
        # the default (GOLD/5m) is byte-identical to the legacy global preflight.
        self.symbol = symbol
        self.timeframe = timeframe
        self.source_config = config.get("market_data_sources", {}).get(f"{symbol.lower()}_{timeframe}", {}) or config.get("market_data_sources", {}).get("gold_5m", {})
        self.checked_at = checked_at

    def run(self, run_date: str) -> dict:
        checked_at = (self.checked_at or datetime.now(timezone.utc)).astimezone(timezone.utc).replace(microsecond=0)
        clean_bars = load_json(self.output_root / "clean_bars" / run_date / f"{self.symbol}_{self.timeframe}.json")
        latest = clean_bars[-1] if clean_bars else {}
        data_quality = self._load_gold_data_quality(run_date)
        data_quality_allows_trading = data_quality.get("allows_trading") if data_quality else None
        data_quality_reasons = data_quality.get("reasons", []) if data_quality else []
        coverage = MarketStore(self.market_db).coverage() if self.market_db.exists() else []
        gold_coverage = [item for item in coverage if item["symbol"] == self.symbol and item["timeframe"] == self.timeframe]
        official_providers = set(self.source_config.get("official_broker_providers", ["broker_csv", "mt5_csv", "ibkr", "oanda"]))
        public_providers = set(self.source_config.get("public_providers", ["gold-api.com", "yahoo_chart:GC=F"]))
        execution_venue_providers = set(self.source_config.get("execution_venue_providers", []))
        allow_execution_venue_for_live = bool(self.source_config.get("allow_execution_venue_for_live", False))
        official_rows = sum(item["rows"] for item in gold_coverage if item["provider"] in official_providers)
        execution_venue_rows = sum(item["rows"] for item in gold_coverage if item["provider"] in execution_venue_providers)
        public_rows = sum(item["rows"] for item in gold_coverage if item["provider"] in public_providers)
        latest_provider = str(latest.get("provider", ""))
        latest_price = latest.get("close")
        latest_quote = MarketStore(self.market_db).load_latest_quote(self.symbol) if self.market_db.exists() else {}
        price_sanity = self._price_sanity(latest, latest_quote)
        max_live_bar_lag_minutes = float(self.source_config.get("max_live_bar_lag_minutes", 15))
        max_public_quote_age_minutes = float(self.source_config.get("max_public_quote_age_minutes", max_live_bar_lag_minutes))
        live_bar_lag_minutes = self._live_bar_lag_minutes(latest, latest_quote)
        live_bar_is_fresh = live_bar_lag_minutes is None or live_bar_lag_minutes <= max_live_bar_lag_minutes
        latest_record = latest_quote or latest
        latest_record_age_minutes = self._record_age_minutes(latest_record, checked_at)
        checks_current_session = self._is_current_run_date(run_date, checked_at)
        latest_record_is_fresh = (
            True
            if not checks_current_session
            else latest_record_age_minutes is not None and latest_record_age_minutes <= max_public_quote_age_minutes
        )
        price_sanity_passes = bool(price_sanity.get("passes", True))
        base_ready_for_paper = bool(latest and self.market_db.exists() and latest_record_is_fresh and price_sanity_passes)
        ready_for_paper = bool(base_ready_for_paper and data_quality_allows_trading is not False)
        official_live_ready = bool(official_rows > 0 and latest_provider in official_providers and live_bar_is_fresh and latest_record_is_fresh and price_sanity_passes)
        execution_venue_live_ready = bool(
            allow_execution_venue_for_live
            and execution_venue_rows > 0
            and latest_provider in execution_venue_providers
            and live_bar_is_fresh
            and latest_record_is_fresh
            and price_sanity_passes
        )
        ready_for_live = bool(official_live_ready or execution_venue_live_ready)
        status = "pass" if ready_for_live else ("warn" if ready_for_paper else "fail")
        if official_live_ready:
            message = f"official broker market data is active for {self.symbol} {self.timeframe}"
        elif execution_venue_live_ready:
            message = f"execution venue market data is active for {self.symbol} {self.timeframe}"
        elif checks_current_session and not latest_record_is_fresh:
            age_text = f"{latest_record_age_minutes:.1f}" if latest_record_age_minutes is not None else "unknown"
            message = f"latest {self.symbol} quote/bar is stale by {age_text} minutes; refresh market data before paper or live trading"
        elif not price_sanity_passes:
            message = "; ".join(price_sanity.get("reasons", [])) or f"{self.symbol} price sanity check failed"
        elif official_rows > 0 and latest_provider in official_providers and not live_bar_is_fresh:
            message = f"official {self.symbol} {self.timeframe} latest bar is stale by {live_bar_lag_minutes:.1f} minutes; refresh broker/MT5 feed"
        elif data_quality_allows_trading is False:
            message = "; ".join(data_quality_reasons) or "data quality gate blocked paper trading"
        elif ready_for_paper:
            message = "paper trading uses public/local market data; no official broker market feed is active"
        else:
            message = f"{self.symbol} {self.timeframe} market data is not ready"
        payload = {
            "run_date": run_date,
            "checked_at": checked_at.isoformat(),
            "status": status,
            "message": message,
            "ready_for_paper": ready_for_paper,
            "ready_for_live": ready_for_live,
            "base_ready_for_paper": base_ready_for_paper,
            "data_quality_allows_trading": data_quality_allows_trading,
            "data_quality_reasons": data_quality_reasons,
            "latest_provider": latest_provider,
            "latest_price": latest_price,
            "latest_timestamp": latest.get("timestamp", ""),
            "latest_bar": latest,
            "latest_quote": latest_quote,
            "price_sanity": price_sanity,
            "live_bar_lag_minutes": round(live_bar_lag_minutes, 2) if live_bar_lag_minutes is not None else None,
            "max_live_bar_lag_minutes": max_live_bar_lag_minutes,
            "live_bar_is_fresh": live_bar_is_fresh,
            "latest_record_age_minutes": round(latest_record_age_minutes, 2) if latest_record_age_minutes is not None else None,
            "max_public_quote_age_minutes": max_public_quote_age_minutes,
            "latest_record_is_fresh": latest_record_is_fresh,
            "current_session_freshness_enforced": checks_current_session,
            "market_db": str(self.market_db),
            "coverage": gold_coverage,
            "official_broker_providers": sorted(official_providers),
            "execution_venue_providers": sorted(execution_venue_providers),
            "public_providers": sorted(public_providers),
            "official_rows": official_rows,
            "execution_venue_rows": execution_venue_rows,
            "public_rows": public_rows,
            "live_data_mode": "official_broker" if official_live_ready else ("execution_venue" if execution_venue_live_ready else "not_live_ready"),
        }
        write_json(self.output_root / "data_source_preflight" / "current.json", [payload])
        write_json(self.output_root / "data_source_preflight" / f"{run_date}.json", [payload])
        return payload

    def _price_sanity(self, latest_bar: dict, latest_quote: dict) -> dict:
        config = self.source_config.get("price_sanity", {})
        enabled = bool(config.get("enabled", True))
        min_price = float(config.get("min_price", 0))
        max_price = float(config.get("max_price", 999999))
        max_deviation_pct = float(config.get("max_quote_bar_deviation_pct", 3))
        bar_price = self._float_or_none(latest_bar.get("close")) if latest_bar else None
        quote_price = self._float_or_none(latest_quote.get("close")) if latest_quote else None
        latest_record_price = quote_price if quote_price is not None else bar_price
        reasons: list[str] = []
        if enabled:
            for label, price in [("latest GOLD record", latest_record_price), ("latest GOLD clean bar", bar_price)]:
                if price is None:
                    continue
                if price < min_price or price > max_price:
                    reasons.append(f"{label} price {price:.2f} outside sanity range {min_price:.2f}-{max_price:.2f}")
            deviation_pct = self._deviation_pct(bar_price, quote_price)
            if deviation_pct is not None and deviation_pct > max_deviation_pct:
                reasons.append(f"GOLD quote/bar price deviation {deviation_pct:.2f}% above max {max_deviation_pct:.2f}%")
        else:
            deviation_pct = self._deviation_pct(bar_price, quote_price)
        return {
            "enabled": enabled,
            "passes": not reasons,
            "reasons": reasons,
            "min_price": min_price,
            "max_price": max_price,
            "max_quote_bar_deviation_pct": max_deviation_pct,
            "latest_record_price": latest_record_price,
            "latest_bar_price": bar_price,
            "latest_quote_price": quote_price,
            "quote_bar_deviation_pct": round(deviation_pct, 4) if deviation_pct is not None else None,
        }

    def _float_or_none(self, value: object) -> float | None:
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def _deviation_pct(self, left: float | None, right: float | None) -> float | None:
        if left is None or right is None:
            return None
        baseline = max(abs(left), abs(right))
        if baseline == 0:
            return 0.0
        return abs(left - right) / baseline * 100

    def _record_age_minutes(self, latest_record: dict, checked_at: datetime) -> float | None:
        if not latest_record:
            return None
        record_time = self._parse_time(str(latest_record.get("timestamp", "")))
        if not record_time:
            return None
        return max(0.0, (checked_at - record_time).total_seconds() / 60)

    def _is_current_run_date(self, run_date: str, checked_at: datetime) -> bool:
        try:
            return datetime.fromisoformat(run_date).date() == checked_at.astimezone(timezone.utc).date()
        except ValueError:
            return False

    def _live_bar_lag_minutes(self, latest_bar: dict, latest_quote: dict) -> float | None:
        if not latest_bar or not latest_quote:
            return None
        bar_time = self._parse_time(str(latest_bar.get("timestamp", "")))
        quote_time = self._parse_time(str(latest_quote.get("timestamp", "")))
        if not bar_time or not quote_time:
            return None
        lag = (quote_time - bar_time).total_seconds() / 60
        return max(0.0, lag)

    def _parse_time(self, value: str) -> datetime | None:
        if not value or value.startswith("mock-"):
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    def _load_gold_data_quality(self, run_date: str) -> dict:
        path = self.output_root / "data_quality" / f"{run_date}.json"
        if not path.exists():
            return {}
        data = load_json(path)
        if isinstance(data, dict):
            return data.get(self.symbol, {})
        return {}
