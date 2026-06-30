from __future__ import annotations

import shutil
from datetime import datetime, timezone
from pathlib import Path

from services.broker_feed_bridge import BrokerFeedBridge
from services.config_loader import ROOT, load_pipeline_config
from services.data_source_preflight import DataSourcePreflight
from services.journal_store import write_json
from services.market_store import MarketStore


class BrokerFeedSmoke:
    def __init__(self, output_root: Path | None = None, sandbox_root: Path | None = None) -> None:
        config = load_pipeline_config()
        self.output_root = output_root or ROOT / config.get("output_root", "outputs")
        self.sandbox_root = sandbox_root or ROOT / "data" / "broker_feed_smoke"

    def run(self, run_date: str) -> dict:
        if self.sandbox_root.exists():
            shutil.rmtree(self.sandbox_root)
        feed_dir = self.sandbox_root / "feed"
        sandbox_outputs = self.sandbox_root / "outputs"
        sandbox_db = self.sandbox_root / "market_data.db"
        feed_dir.mkdir(parents=True, exist_ok=True)
        csv_path = feed_dir / "XAUUSD_5m_smoke.csv"
        csv_path.write_text(
            "\n".join(
                [
                    "timestamp,open,high,low,close,volume",
                    f"{run_date}T01:00:00Z,4570.0,4572.0,4569.0,4571.0,10",
                    f"{run_date}T01:05:00Z,4571.0,4573.0,4570.0,4572.0,12",
                    f"{run_date}T01:10:00Z,4572.0,4574.0,4571.0,4573.0,9",
                ]
            )
            + "\n",
            encoding="utf-8",
        )

        bridge = BrokerFeedBridge(
            output_root=sandbox_outputs,
            market_db=sandbox_db,
            config={
                "input_dir": str(feed_dir),
                "provider": "mt5_csv",
                "symbol": "GOLD",
                "timeframe": "5m",
                "pattern": "*.csv",
            },
        )
        import_summary = bridge.import_pending(run_date)
        bars = MarketStore(sandbox_db).load_bars("GOLD", "5m", 100)
        write_json(sandbox_outputs / "clean_bars" / run_date / "GOLD_5m.json", [bar.to_dict() for bar in bars])
        preflight = DataSourcePreflight(sandbox_outputs, sandbox_db, checked_at=datetime.fromisoformat(f"{run_date}T01:12:00+00:00")).run(run_date)
        status = "pass" if import_summary["imported_rows"] > 0 and preflight.get("ready_for_live") else "fail"
        result = {
            "run_date": run_date,
            "checked_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "status": status,
            "summary": "official broker CSV feed can be imported into local SQLite and pass live data-source preflight"
            if status == "pass"
            else "official broker CSV feed smoke failed",
            "csv_path": str(csv_path),
            "sandbox_root": str(self.sandbox_root),
            "sandbox_db": str(sandbox_db),
            "import_summary": import_summary,
            "preflight": preflight,
        }
        write_json(self.output_root / "broker_feed_smoke" / "current.json", [result])
        write_json(self.output_root / "broker_feed_smoke" / f"{run_date}.json", [result])
        return result
