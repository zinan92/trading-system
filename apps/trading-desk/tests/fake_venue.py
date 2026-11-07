"""A pretend exchange: the whole of what adding a venue takes (#1379 acceptance).

It talks to its own API only through the fetch the desk hands it. Nothing in the desk's business code
knows its name.
"""
from __future__ import annotations

from typing import Any, Mapping

from trading_desk.executor import ExecutionRefused
from trading_desk.venues import VenueModule

API = "http://fake-venue.test"


class FakeVenue(VenueModule):
    kind = "fake_venue"
    capabilities = frozenset({"live_price", "bars", "grid_plan", "preview", "execute"})
    plan_template = {"rungs": 4, "notional": 10.0, "range_pct": 0.05, "stop_pct": 0.02, "unit": "FUSD"}
    started_text = "假交易所已接手。"

    def venue_text(self, sources: Any, asset: Mapping[str, Any]) -> tuple[str, str]:
        return "Fake Exchange", "测试用（假钱）"

    def grid_and_price(self, sources: Any, asset: Mapping[str, Any]) -> tuple[dict[str, Any], float | None]:
        try:
            quote = sources.fetch(f"{API}/quote/{asset['instrument_id']}", None) or {}
        except Exception:  # noqa: BLE001 - no quote means no price; the desk then refuses to plan
            return {"ok": False, "reason": "假交易所读不到"}, None
        price = quote.get("price")
        return {"ok": False, "none": True, "reason": "没有运行中的网格"}, (float(price) if price else None)

    def bars(self, sources: Any, asset: Mapping[str, Any], timeframe: str, limit: int) -> dict[str, Any]:
        source = "Fake Exchange K 线"
        try:
            rows = sources.fetch(f"{API}/bars/{asset['instrument_id']}?tf={timeframe}", None) or []
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "reason": f"K 线读取失败：{type(exc).__name__}", "bars": [], "source": source}
        bars = [[r["t"], r["o"], r["h"], r["l"], r["c"]] for r in rows][-limit:]
        return {"ok": bool(bars), "bars": bars, "source": source, **({} if bars else {"reason": "没有数据"})}

    def preview(self, executor: Any, asset: Mapping[str, Any], plan: Mapping[str, Any]) -> dict[str, Any]:
        answer = executor.fetch(f"{API}/preview", {"instrument": asset["instrument_id"], "rungs": plan.get("rungs"), "stop": plan.get("hard_stop")}) or {}
        return {"execution_ready": answer.get("ready") is True, "blockers": answer.get("blockers") or [], "preview_digest": answer.get("digest"),
                "max_loss": answer.get("max_loss")}

    def execute(self, executor: Any, asset: Mapping[str, Any], plan: Mapping[str, Any], *, shown_max_loss: float | None,
                judgment_id: int) -> dict[str, Any]:
        fresh = self.preview(executor, asset, plan)
        if not fresh["execution_ready"]:
            raise ExecutionRefused("预览没有通过风险检查，没有下单。", {"blockers": fresh["blockers"]})
        answer = executor.fetch(f"{API}/confirm", {"digest": fresh["preview_digest"], "judgment": judgment_id}) or {}
        if answer.get("status") != "confirmed":
            raise ExecutionRefused("确认没有通过，没有下单。", {"confirmation": {"status": answer.get("status")}})
        return {"preview": fresh, "status": "confirmed", "started": True}
