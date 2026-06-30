from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from services.config_loader import load_pipeline_config
from services.journal_store import load_json, write_json


class PaperEquityCurve:
    def __init__(self, output_root: Path, starting_equity: float | None = None) -> None:
        self.output_root = output_root
        if starting_equity is None:
            starting_equity = float(load_pipeline_config().get("paper_account", {}).get("starting_equity", 10_000.0))
        self.starting_equity = starting_equity

    def build(self, run_date: str, performance: dict | None = None) -> dict:
        performance = performance or self._latest_performance(run_date)
        summary = performance.get("summary", {}) if performance else {}
        net_pnl = float(summary.get("net_pnl_marked", 0) or 0)
        point = {
            "run_date": run_date,
            "timestamp": performance.get("generated_at") or datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "starting_equity": self.starting_equity,
            "net_pnl_marked": round(net_pnl, 4),
            "equity": round(self.starting_equity + net_pnl, 4),
            "open_unrealized_r": summary.get("open_unrealized_r", 0),
            "realized_pnl_all": summary.get("realized_pnl_all", 0),
            "unrealized_pnl": summary.get("unrealized_pnl", 0),
            "total_execution_costs": summary.get("total_execution_costs", 0),
            "open_trade_count": summary.get("open_trade_count", 0),
            "closed_all_count": summary.get("closed_all_count", 0),
        }
        points = self._merge_point(point)
        running_peak = self.starting_equity
        max_drawdown_pct = 0.0
        enriched = []
        previous_item: dict | None = None
        for item in points:
            equity = float(item.get("equity", self.starting_equity))
            day_start_equity = float(previous_item.get("equity", self.starting_equity)) if previous_item else self.starting_equity
            previous_net_pnl = float(previous_item.get("net_pnl_marked", 0) or 0) if previous_item else 0.0
            net_pnl = float(item.get("net_pnl_marked", 0) or 0)
            daily_pnl = round(net_pnl - previous_net_pnl, 4)
            daily_pnl_pct = round((daily_pnl / day_start_equity) * 100, 4) if day_start_equity else 0.0
            running_peak = max(running_peak, equity)
            drawdown_pct = round(((equity - running_peak) / running_peak) * 100, 4) if running_peak else 0.0
            max_drawdown_pct = min(max_drawdown_pct, drawdown_pct)
            enriched_item = {
                **item,
                "day_start_equity": round(day_start_equity, 4),
                "daily_pnl": daily_pnl,
                "daily_pnl_pct": daily_pnl_pct,
                "peak_equity": round(running_peak, 4),
                "drawdown_pct": drawdown_pct,
            }
            enriched.append(enriched_item)
            previous_item = enriched_item

        latest = enriched[-1] if enriched else {}
        payload = {
            "run_date": run_date,
            "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "status": "pass" if performance else "warn",
            "starting_equity": self.starting_equity,
            "day_start_equity": latest.get("day_start_equity", self.starting_equity),
            "daily_pnl": latest.get("daily_pnl", 0.0),
            "daily_pnl_pct": latest.get("daily_pnl_pct", 0.0),
            "current_equity": latest.get("equity", self.starting_equity),
            "current_drawdown_pct": latest.get("drawdown_pct", 0.0),
            "max_drawdown_pct": round(max_drawdown_pct, 4),
            "point_count": len(enriched),
            "points": enriched,
        }
        write_json(self.output_root / "equity_curve" / "current.json", [payload])
        write_json(self.output_root / "equity_curve" / f"{run_date}.json", [payload])
        self._write_markdown(run_date, payload)
        return payload

    def _latest_performance(self, run_date: str) -> dict:
        rows = load_json(self.output_root / "performance" / f"{run_date}.json")
        if rows:
            return rows[-1]
        current = load_json(self.output_root / "performance" / "current.json")
        return current[-1] if current else {}

    def _merge_point(self, point: dict) -> list[dict]:
        rows = load_json(self.output_root / "equity_curve" / "current.json")
        current = rows[-1] if rows else {}
        points = [item for item in current.get("points", []) if item.get("run_date") != point["run_date"]]
        points.append(point)
        return sorted(points, key=lambda item: item.get("run_date", ""))

    def _write_markdown(self, run_date: str, payload: dict) -> None:
        lines = [
            f"# Paper Equity Curve - {run_date}",
            "",
            f"- Status: {payload['status']}",
            f"- Starting equity: {payload['starting_equity']}",
            f"- Day start equity: {payload['day_start_equity']}",
            f"- Daily PnL: {payload['daily_pnl']}",
            f"- Daily PnL pct: {payload['daily_pnl_pct']}%",
            f"- Current equity: {payload['current_equity']}",
            f"- Current drawdown: {payload['current_drawdown_pct']}%",
            f"- Max drawdown: {payload['max_drawdown_pct']}%",
            f"- Points: {payload['point_count']}",
            "",
            "## Points",
        ]
        for item in payload["points"][-20:]:
            lines.append(
                f"- {item['run_date']}: equity {item['equity']} net {item['net_pnl_marked']} drawdown {item['drawdown_pct']}% open_R {item.get('open_unrealized_r', 0)}"
            )
        path = self.output_root / "equity_curve" / f"{run_date}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def build_paper_equity_curve(run_date: str, output_root: Path, performance: dict | None = None) -> dict:
    return PaperEquityCurve(output_root).build(run_date, performance)
