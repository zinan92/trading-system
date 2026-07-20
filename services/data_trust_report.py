from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from services.config_loader import ROOT, load_pipeline_config
from services.journal_store import load_json, write_json
from services.market_data_access import market_data_repository


class DataTrustReport:
    def __init__(self, output_root: Path | None = None, market_db: Path | None = None) -> None:
        config = load_pipeline_config()
        self.config = config
        self.output_root = output_root or ROOT / config.get("output_root", "outputs")
        self.market_db = market_db or ROOT / config.get("local_market_db", "data/market_data.db")
        source_config = config.get("market_data_sources", {}).get("gold_5m", {})
        self.official_providers = set(source_config.get("official_broker_providers", ["broker_csv", "mt5_csv", "ibkr", "oanda"]))
        self.public_providers = set(source_config.get("public_providers", ["gold-api.com", "yahoo_chart:GC=F"]))
        self.execution_venue_providers = set(source_config.get("execution_venue_providers", []))
        sanity = source_config.get("price_sanity", {})
        self.min_price = float(sanity.get("min_price", 3000))
        self.max_price = float(sanity.get("max_price", 6000))

    def run(self, run_date: str) -> dict:
        clean = load_json(self.output_root / "clean_bars" / run_date / "GOLD_5m.json")
        latest_clean = clean[-1] if clean else {}
        store = market_data_repository(self.market_db)
        latest_quote = store.load_latest_quote("GOLD")
        coverage = store.coverage()
        lineage = self._latest("data_source_lineage", run_date)
        preflight = self._latest("data_source_preflight", run_date)
        truth_level = str(lineage.get("truth_level", "unknown"))
        latest_display = latest_clean if truth_level in {"official_broker", "execution_venue"} else (latest_quote or latest_clean)
        checks = [
            self._price_check(latest_display),
            self._latest_provider_check(latest_display),
            self._truth_label_check(lineage, preflight),
            self._official_rows_check(coverage, lineage),
            self._mock_leak_check(clean, latest_clean),
        ]
        payload = {
            "run_date": run_date,
            "checked_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "status": self._rollup(checks),
            "display_mode": self._display_mode(lineage),
            "latest_display": latest_display,
            "latest_clean_bar": latest_clean,
            "latest_quote": latest_quote,
            "checks": checks,
            "summary": {
                "passed": sum(1 for item in checks if item["status"] == "pass"),
                "warned": sum(1 for item in checks if item["status"] == "warn"),
                "failed": sum(1 for item in checks if item["status"] == "fail"),
                "latest_price": latest_display.get("close"),
                "latest_provider": latest_display.get("provider", ""),
                "truth_level": lineage.get("truth_level", "unknown"),
                "official_rows": self._official_rows(coverage, lineage),
                "execution_venue_rows": self._execution_venue_rows(coverage, lineage),
                "public_rows": self._public_rows(coverage, preflight),
                "mock_rows": sum(int(item.get("rows", 0) or 0) for item in coverage if self._provider_group(str(item.get("provider", ""))) == "mock"),
                "latest_is_mock": self._is_mock_record(latest_display),
                "latest_is_official": str(latest_display.get("provider", "")) in self.official_providers,
                "latest_is_execution_venue": str(latest_display.get("provider", "")) in self.execution_venue_providers,
                "latest_is_public": str(latest_display.get("provider", "")) in self.public_providers,
                "price_sanity_min": self.min_price,
                "price_sanity_max": self.max_price,
            },
            "source_artifacts": {
                "market_data_port": "datafeed",
                "clean_bars": str(self.output_root / "clean_bars" / run_date / "GOLD_5m.json"),
                "data_source_preflight": str(self.output_root / "data_source_preflight" / f"{run_date}.json"),
                "data_source_lineage": str(self.output_root / "data_source_lineage" / f"{run_date}.json"),
            },
        }
        write_json(self.output_root / "data_trust" / f"{run_date}.json", [payload])
        write_json(self.output_root / "data_trust" / "current.json", [payload])
        self._write_markdown(run_date, payload)
        return payload

    def _price_check(self, record: dict) -> dict:
        price = self._float_or_none(record.get("close"))
        if price is None:
            return self._check("latest_price", "fail", "Latest GOLD display price is missing.", record)
        if price < self.min_price or price > self.max_price:
            return self._check("latest_price", "fail", f"Latest GOLD display price {price:.2f} is outside {self.min_price:.2f}-{self.max_price:.2f}.", record)
        return self._check("latest_price", "pass", f"Latest GOLD display price {price:.2f} passes sanity range.", record)

    def _latest_provider_check(self, record: dict) -> dict:
        provider = str(record.get("provider", ""))
        if not provider:
            return self._check("latest_provider", "fail", "Latest GOLD provider is missing.", record)
        if self._is_mock_record(record):
            return self._check("latest_provider", "fail", f"Latest GOLD display record is mock/synthetic: {provider}.", record)
        if provider in self.official_providers:
            return self._check("latest_provider", "pass", f"Latest GOLD display record is official broker data: {provider}.", record)
        if provider in self.execution_venue_providers:
            return self._check("latest_provider", "pass", f"Latest GOLD display record is execution venue data: {provider}.", record)
        if provider in self.public_providers:
            return self._check("latest_provider", "warn", f"Latest GOLD display record is public paper data: {provider}.", record)
        return self._check("latest_provider", "warn", f"Latest GOLD provider is not classified: {provider}.", record)

    def _truth_label_check(self, lineage: dict, preflight: dict) -> dict:
        truth = str(lineage.get("truth_level", "unknown"))
        ready_for_live = bool(preflight.get("ready_for_live"))
        official_rows = int(preflight.get("official_rows", 0) or 0)
        execution_venue_rows = int(preflight.get("execution_venue_rows", 0) or 0)
        if ready_for_live and truth not in {"official_broker", "execution_venue"}:
            return self._check("truth_label", "fail", "Preflight says live-ready but lineage is neither official_broker nor execution_venue.", {"truth_level": truth, "ready_for_live": ready_for_live})
        if truth == "official_broker" and official_rows <= 0:
            return self._check("truth_label", "fail", "Lineage says official_broker but official_rows is zero.", {"truth_level": truth, "official_rows": official_rows})
        if truth == "execution_venue" and execution_venue_rows <= 0:
            return self._check("truth_label", "fail", "Lineage says execution_venue but execution_venue_rows is zero.", {"truth_level": truth, "execution_venue_rows": execution_venue_rows})
        if truth == "execution_venue":
            return self._check("truth_label", "pass", "Current data truth is execution_venue and is accepted for the current GOLD trading scope.", {"truth_level": truth, "ready_for_live": ready_for_live, "official_rows": official_rows, "execution_venue_rows": execution_venue_rows})
        if truth in {"public_snapshot", "official_broker", "execution_venue", "synthetic_seed"}:
            status = "pass" if truth == "official_broker" else "warn"
            return self._check("truth_label", status, f"Current data truth is {truth}.", {"truth_level": truth, "ready_for_live": ready_for_live, "official_rows": official_rows, "execution_venue_rows": execution_venue_rows})
        return self._check("truth_label", "warn", f"Current data truth is {truth}.", {"truth_level": truth, "ready_for_live": ready_for_live})

    def _official_rows_check(self, coverage: list[dict], lineage: dict) -> dict:
        official_rows = self._official_rows(coverage, lineage)
        execution_venue_rows = self._execution_venue_rows(coverage, lineage)
        if str(lineage.get("truth_level", "")) == "execution_venue" and execution_venue_rows > 0:
            return self._check("execution_grade_rows", "pass", f"Local DB has {execution_venue_rows} execution venue GOLD 5m row(s).", {"official_rows": official_rows, "execution_venue_rows": execution_venue_rows})
        if official_rows > 0:
            return self._check("official_rows", "pass", f"Local DB has {official_rows} official GOLD 5m row(s).", {"official_rows": official_rows})
        return self._check("official_rows", "warn", "Local DB has no official broker GOLD/XAUUSD 5m rows; live remains blocked.", {"official_rows": official_rows})

    def _mock_leak_check(self, clean: list[dict], latest_clean: dict) -> dict:
        latest_is_mock = self._is_mock_record(latest_clean)
        mock_rows = sum(1 for item in clean if self._is_mock_record(item))
        if latest_is_mock:
            return self._check("mock_leak", "fail", "Latest clean GOLD 5m bar is mock/synthetic and must not drive current paper decisions.", {"mock_rows": mock_rows, "latest_clean": latest_clean})
        if mock_rows:
            return self._check("mock_leak", "warn", f"Clean GOLD 5m history includes {mock_rows} mock/synthetic seed row(s), but latest bar is not mock.", {"mock_rows": mock_rows})
        return self._check("mock_leak", "pass", "No mock/synthetic rows found in current clean GOLD 5m data.", {"mock_rows": 0})

    def _display_mode(self, lineage: dict) -> str:
        truth = str(lineage.get("truth_level", "unknown"))
        if truth == "official_broker":
            return "OFFICIAL_BROKER"
        if truth == "execution_venue":
            return "EXECUTION_VENUE"
        if truth == "public_snapshot":
            return "PAPER_PUBLIC"
        if truth == "synthetic_seed":
            return "BLOCKED_SYNTHETIC"
        return "UNKNOWN"

    def _provider_group(self, provider: str) -> str:
        if provider in self.official_providers:
            return "official"
        if provider in self.execution_venue_providers:
            return "execution_venue"
        if provider in self.public_providers:
            return "public"
        if provider == "local_synthetic_seed" or "mock" in provider or "synthetic" in provider:
            return "mock"
        return "other"

    def _is_mock_record(self, record: dict) -> bool:
        provider = str(record.get("provider", ""))
        flags = {str(item) for item in record.get("quality_flags", [])}
        timestamp = str(record.get("timestamp", ""))
        return provider == "local_synthetic_seed" or "mock" in provider or "synthetic" in provider or "mock" in flags or "synthetic_seed" in flags or timestamp.startswith("mock-")

    def _official_rows(self, coverage: list[dict], lineage: dict) -> int:
        rows = sum(int(item.get("rows", 0) or 0) for item in coverage if str(item.get("provider", "")) in self.official_providers)
        if rows:
            return rows
        return int(((lineage.get("provider_groups") or {}).get("official") or {}).get("rows", 0) or 0)

    def _execution_venue_rows(self, coverage: list[dict], lineage: dict) -> int:
        rows = sum(int(item.get("rows", 0) or 0) for item in coverage if str(item.get("provider", "")) in self.execution_venue_providers)
        if rows:
            return rows
        return int(((lineage.get("provider_groups") or {}).get("execution_venue") or {}).get("rows", 0) or 0)

    def _public_rows(self, coverage: list[dict], preflight: dict) -> int:
        rows = sum(int(item.get("rows", 0) or 0) for item in coverage if str(item.get("provider", "")) in self.public_providers)
        return rows or int(preflight.get("public_rows", 0) or 0)

    def _float_or_none(self, value: object) -> float | None:
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def _latest(self, folder: str, run_date: str) -> dict:
        dated = load_json(self.output_root / folder / f"{run_date}.json")
        current = load_json(self.output_root / folder / "current.json")
        if dated:
            return dated[-1]
        return current[-1] if current else {}

    def _rollup(self, checks: list[dict]) -> str:
        states = {item["status"] for item in checks}
        if "fail" in states:
            return "fail"
        if "warn" in states:
            return "warn"
        return "pass"

    def _check(self, name: str, status: str, summary: str, evidence: dict) -> dict:
        return {"name": name, "status": status, "summary": summary, "evidence": evidence}

    def _write_markdown(self, run_date: str, payload: dict) -> None:
        lines = [
            f"# Data Trust Report - {run_date}",
            "",
            f"- Status: {payload['status']}",
            f"- Display mode: {payload['display_mode']}",
            f"- Latest price: {payload['summary']['latest_price']} from {payload['summary']['latest_provider']}",
            f"- Truth level: {payload['summary']['truth_level']}",
            f"- Official rows: {payload['summary']['official_rows']}",
            f"- Execution venue rows: {payload['summary']['execution_venue_rows']}",
            f"- Latest is mock: {payload['summary']['latest_is_mock']}",
            "",
            "## Checks",
        ]
        for item in payload["checks"]:
            lines.append(f"- {item['status']}: {item['name']} - {item['summary']}")
        path = self.output_root / "data_trust" / f"{run_date}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def run_data_trust_report(run_date: str, output_root: Path | None = None, market_db: Path | None = None) -> dict:
    return DataTrustReport(output_root, market_db).run(run_date)
