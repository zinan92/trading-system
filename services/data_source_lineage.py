from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

from services.config_loader import ROOT, load_pipeline_config
from services.data_source_preflight import DataSourcePreflight
from services.journal_store import load_json, write_json
from services.market_data_access import market_data_repository


class DataSourceLineage:
    def __init__(self, output_root: Path | None = None, market_db: Path | None = None) -> None:
        config = load_pipeline_config()
        self.config = config
        self.output_root = output_root or Path(os.getenv("TRADING_ORCHESTRATOR_OUTPUT_ROOT", str(ROOT / config.get("output_root", "outputs"))))
        self.market_db = market_db or Path(os.getenv("TRADING_ORCHESTRATOR_MARKET_DB", str(ROOT / config.get("local_market_db", "data/market_data.db"))))
        source_config = config.get("market_data_sources", {}).get("gold_5m", {})
        self.official_providers = set(source_config.get("official_broker_providers", ["broker_csv", "mt5_csv", "ibkr", "oanda"]))
        self.public_providers = set(source_config.get("public_providers", ["gold-api.com", "yahoo_chart:GC=F"]))
        self.execution_venue_providers = set(source_config.get("execution_venue_providers", []))

    def run(self, run_date: str) -> dict:
        store = market_data_repository(self.market_db)
        coverage = store.coverage()
        gold_coverage = [item for item in coverage if item.get("symbol") == "GOLD" and item.get("timeframe") == "5m"]
        clean_bars = load_json(self.output_root / "clean_bars" / run_date / "GOLD_5m.json")
        latest_clean_bar = clean_bars[-1] if clean_bars else {}
        latest_quote = store.load_latest_quote("GOLD")
        latest_public_bar = store.load_latest_bar("GOLD", "5m", sorted(self.public_providers))
        latest_execution_venue_bar = store.load_latest_bar("GOLD", "5m", sorted(self.execution_venue_providers))
        latest_official_bar = store.load_latest_bar("GOLD", "5m", sorted(self.official_providers))
        preflight_rows = load_json(self.output_root / "data_source_preflight" / f"{run_date}.json")
        preflight = preflight_rows[-1] if preflight_rows else DataSourcePreflight(self.output_root, self.market_db).run(run_date)
        grouped = self._group_coverage(gold_coverage)
        truth_level = self._truth_level(latest_clean_bar)
        official_live_eligible = bool(preflight.get("ready_for_live")) and truth_level == "official_broker" and grouped["official"]["rows"] > 0
        execution_venue_ready = bool(preflight.get("ready_for_live")) and truth_level == "execution_venue" and grouped["execution_venue"]["rows"] > 0
        execution_grade_ready = official_live_eligible or execution_venue_ready
        status = "pass" if execution_grade_ready else ("warn" if preflight.get("ready_for_paper") else "fail")
        payload = {
            "run_date": run_date,
            "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "status": status,
            "market_data_port": "datafeed",
            "market_data_available": bool(coverage or latest_quote),
            "symbol": "GOLD",
            "timeframe": "5m",
            "truth_level": truth_level,
            "ready_for_paper": bool(preflight.get("ready_for_paper")),
            "ready_for_live": execution_grade_ready,
            "official_live_eligible": official_live_eligible,
            "execution_venue_ready": execution_venue_ready,
            "lineage_message": self._message(status, truth_level, grouped, preflight),
            "latest_clean_bar": latest_clean_bar,
            "latest_quote": latest_quote,
            "latest_public_bar": latest_public_bar,
            "latest_execution_venue_bar": latest_execution_venue_bar,
            "latest_official_bar": latest_official_bar,
            "provider_groups": grouped,
            "coverage": gold_coverage,
            "official_broker_providers": sorted(self.official_providers),
            "execution_venue_providers": sorted(self.execution_venue_providers),
            "public_providers": sorted(self.public_providers),
            "preflight_status": preflight.get("status", ""),
            "preflight_message": preflight.get("message", ""),
            "next_action": self._next_action(official_live_eligible, grouped, preflight, execution_venue_ready),
        }
        write_json(self.output_root / "data_source_lineage" / "current.json", [payload])
        write_json(self.output_root / "data_source_lineage" / f"{run_date}.json", [payload])
        return payload

    def _group_coverage(self, coverage: list[dict]) -> dict:
        groups = {
            "official": {"rows": 0, "providers": [], "latest_timestamp": ""},
            "execution_venue": {"rows": 0, "providers": [], "latest_timestamp": ""},
            "public": {"rows": 0, "providers": [], "latest_timestamp": ""},
            "synthetic": {"rows": 0, "providers": [], "latest_timestamp": ""},
            "other": {"rows": 0, "providers": [], "latest_timestamp": ""},
        }
        for item in coverage:
            provider = str(item.get("provider", ""))
            group = self._provider_group(provider)
            groups[group]["rows"] += int(item.get("rows", 0) or 0)
            groups[group]["providers"].append(provider)
            groups[group]["latest_timestamp"] = max(groups[group]["latest_timestamp"], str(item.get("last_timestamp", "")))
        for value in groups.values():
            value["providers"] = sorted(set(value["providers"]))
        return groups

    def _provider_group(self, provider: str) -> str:
        if provider in self.official_providers:
            return "official"
        if provider in self.execution_venue_providers:
            return "execution_venue"
        if provider in self.public_providers:
            return "public"
        if "synthetic" in provider or provider == "local_synthetic_seed":
            return "synthetic"
        return "other"

    def _truth_level(self, latest_clean_bar: dict) -> str:
        provider = str(latest_clean_bar.get("provider", ""))
        if provider in self.official_providers:
            return "official_broker"
        if provider in self.execution_venue_providers:
            return "execution_venue"
        if provider in self.public_providers:
            return "public_snapshot"
        if "synthetic" in provider or provider == "local_synthetic_seed":
            return "synthetic_seed"
        return "unknown"

    def _message(self, status: str, truth_level: str, grouped: dict, preflight: dict) -> str:
        if status == "pass":
            if truth_level == "execution_venue":
                return "Local GOLD 5m database is backed by the execution venue provider and can be used for trading preflight."
            return "Local GOLD 5m database is backed by a broker provider and can be used for trading preflight."
        if truth_level == "execution_venue" and grouped.get("execution_venue", {}).get("rows", 0) > 0:
            return "Execution venue GOLD 5m rows exist, but the latest clean bar or freshness checks are not ready."
        if grouped.get("execution_venue", {}).get("rows", 0) > 0 and truth_level != "execution_venue":
            return "Execution venue rows exist, but the current clean bar is not sourced from the execution venue provider."
        if grouped["official"]["rows"] <= 0:
            return "Local GOLD 5m database currently has no official broker rows; paper uses public data only."
        if truth_level != "official_broker":
            return "Official broker rows exist, but the current clean bar is not sourced from the official provider."
        return str(preflight.get("message") or "Local data source is not live eligible.")

    def _next_action(self, live_eligible: bool, grouped: dict, preflight: dict, execution_venue_ready: bool = False) -> str:
        if live_eligible:
            return "Keep collecting execution-grade 5m bars and run live activation only after manual approval."
        if execution_venue_ready:
            return "Keep collecting Binance USDM XAUUSDT 5m bars; execution venue data is accepted for the current GOLD trading scope."
        if grouped.get("execution_venue", {}).get("rows", 0) > 0:
            return "Refresh execution venue 5m feed so the latest clean bar and quote pass freshness checks."
        if grouped["official"]["rows"] <= 0:
            return "Connect an execution-grade GOLD feed before enabling live."
        if not preflight.get("ready_for_live"):
            return "Refresh official 5m feed so the latest clean bar and quote pass freshness checks."
        return "Review data quality and provider routing before enabling live."
