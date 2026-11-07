"""Hyperliquid Testnet perpetual grids (fake money). Venue and instrument come from the asset row."""
from __future__ import annotations

from typing import Any, Mapping

from .base import VenueModule


class HyperliquidTestnet(VenueModule):
    kind = "hl_testnet"
    capabilities = frozenset({"live_price", "bars", "grid_plan", "preview", "execute", "stop", "rearm_hold", "price_alerts", "bars_health"})
    # Park's approved f107 shape: 5 rungs x 19 USDC, range ~4.5% under price, stop ~2.7% beyond.
    plan_template = {"rungs": 5, "notional": 19.0, "range_pct": 0.045, "stop_pct": 0.027, "unit": "USDC"}
    started_text = "网格已经挂上测试盘，系统接管盯盘。"
    bars_health_label = "测试盘 K 线（Hyperliquid）"

    def venue_text(self, sources: Any, asset: Mapping[str, Any]) -> tuple[str, str]:
        return "Hyperliquid Testnet", "测试盘（假钱）"

    def grid_and_price(self, sources: Any, asset: Mapping[str, Any]) -> tuple[dict[str, Any], float | None]:
        return sources.hl_grid(asset), sources.hl_mid(asset)

    def bars(self, sources: Any, asset: Mapping[str, Any], timeframe: str, limit: int) -> dict[str, Any]:
        return sources.hl_bars(asset, timeframe, limit)

    def preview(self, executor: Any, asset: Mapping[str, Any], plan: Mapping[str, Any]) -> dict[str, Any]:
        return executor.preview_testnet(asset, plan)

    def execute(self, executor: Any, asset: Mapping[str, Any], plan: Mapping[str, Any], *, shown_max_loss: float | None,
                judgment_id: int) -> dict[str, Any]:
        return executor.execute_testnet(asset, plan, shown_max_loss=shown_max_loss, judgment_id=judgment_id)
