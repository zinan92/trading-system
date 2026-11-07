"""TEMPLATE — copy to apps/trading-desk/src/trading_desk/venues/<your_venue>.py, rename, fill in.

A Desk Venue Module (ADR 0007) holds the desk-side behaviour of one kind of asset. It does not
name venue profiles or instruments: those come from the asset row and the Dashboard catalog.

Rules:
- Declare only what you implement. A capability you declare without backing it is dropped at
  registry build, and a warning is logged.
- Fail closed. When your upstream is down, give no price, no ready preview, no confirmation and
  no driver. `venues/conformance.py` checks this for every registered module.
- Talk to the outside only through the fetch the desk hands you: `sources.fetch` for reads,
  `executor.fetch` for preview and confirm. Never read credentials here.
- Never fall back to another venue's price. A failed feed means no price.

This file is checked by `apps/trading-desk/tests/test_venue_template.py` so it cannot rot.
"""
from __future__ import annotations

from typing import Any, Mapping

from trading_desk.executor import ExecutionRefused
from trading_desk.venues import VenueModule

API = "https://example-venue.invalid"  # your venue's public endpoint; read-only data needs no account


class TemplateVenue(VenueModule):
    kind = "template_venue"  # the value stored in assets.kind; never rename it once rows exist
    # Start read-only: K-lines and a plan. Add "preview" and "execute" only with a real preview and
    # confirm path behind them, and only once the conformance suite passes.
    capabilities = frozenset({"live_price", "bars", "grid_plan"})
    plan_template = {"rungs": 5, "notional": 10.0, "range_pct": 0.05, "stop_pct": 0.02, "unit": "USDT"}  # notional OR units
    started_text = ""  # what Park reads after a started execute (required with "execute")

    def venue_text(self, sources: Any, asset: Mapping[str, Any]) -> tuple[str, str]:
        return "Example Venue", "测试盘（假钱）"  # (venue, money): say plainly whether the money is real

    def grid_and_price(self, sources: Any, asset: Mapping[str, Any]) -> tuple[dict[str, Any], float | None]:
        try:
            quote = sources.fetch(f"{API}/ticker/{asset['instrument_id']}", None) or {}
        except Exception:  # noqa: BLE001 - no price is the honest answer; the desk then refuses to plan
            return {"ok": False, "reason": "行情读不到"}, None
        price = quote.get("last")
        return {"ok": False, "none": True, "reason": "没有运行中的网格"}, (float(price) if price else None)

    def bars(self, sources: Any, asset: Mapping[str, Any], timeframe: str, limit: int) -> dict[str, Any]:
        source = "Example Venue K 线"
        try:
            rows = sources.fetch(f"{API}/candles/{asset['instrument_id']}?tf={timeframe}&limit={limit}", None) or []
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "reason": f"K 线读取失败：{type(exc).__name__}", "bars": [], "source": source}
        bars = [[r["time"], float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"])] for r in rows][-limit:]
        return {"ok": bool(bars), "bars": bars, "source": source, **({} if bars else {"reason": "K 线暂时没有数据"})}

    # Only with "preview" / "execute" declared. Shown here so the shape is clear.
    def preview(self, executor: Any, asset: Mapping[str, Any], plan: Mapping[str, Any]) -> dict[str, Any]:
        raise ExecutionRefused("这个交易所还没有开通预览。")

    def execute(self, executor: Any, asset: Mapping[str, Any], plan: Mapping[str, Any], *, shown_max_loss: float | None,
                judgment_id: int) -> dict[str, Any]:
        raise ExecutionRefused("这个交易所还没有开通下单。")
