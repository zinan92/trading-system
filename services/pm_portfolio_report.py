from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from services.config_loader import ROOT, load_pipeline_config


PM_REPORT_KINDS = {
    "pm_morning": ("早盘复盘", "早报", "morning"),
    "pm_evening": ("晚盘复盘", "晚报", "evening"),
}
REPORT_WINDOW_HOURS = 12


@dataclass(frozen=True)
class MarketMove:
    timeframe: str
    provider: str
    start_time: str
    end_time: str
    start_price: float
    end_price: float
    high: float
    low: float

    @property
    def pct(self) -> float:
        return ((self.end_price - self.start_price) / self.start_price * 100) if self.start_price else 0.0

    @property
    def points(self) -> float:
        return self.end_price - self.start_price


class PMPortfolioReportBuilder:
    """Build a user-facing gold trading review for the single active strategy."""

    def __init__(self, output_root: Path | None = None, market_db: Path | None = None) -> None:
        pipeline_config = load_pipeline_config()
        self.output_root = Path(output_root or os.getenv("TRADING_ORCHESTRATOR_OUTPUT_ROOT", str(ROOT / pipeline_config.get("output_root", "outputs"))))
        self.market_db = Path(market_db or os.getenv("TRADING_ORCHESTRATOR_MARKET_DB", str(ROOT / pipeline_config.get("local_market_db", "data/market_data.db"))))
        self.config = pipeline_config

    def build(self, run_date: str, kind: str = "pm_morning") -> Path:
        if kind not in PM_REPORT_KINDS:
            raise ValueError(f"kind must be one of {sorted(PM_REPORT_KINDS)}")

        display_name, _, suffix = PM_REPORT_KINDS[kind]
        active_strategy_id = self._active_strategy_id()
        market = self._market_move()
        window_end = self._parse_time(market.end_time) if market else datetime.now(timezone.utc)
        window_start = window_end - timedelta(hours=REPORT_WINDOW_HOURS)
        strategy = self._strategy_window(active_strategy_id, run_date, window_start)
        view_note = self._market_view_note(run_date)

        lines = [
            f"# 黄金交易{display_name} - {run_date}",
            "",
            "## 一句话",
            "",
            self._headline(active_strategy_id, market, strategy),
            "",
            "## 行情",
            "",
            *self._market_lines(market),
            "",
            "## 策略表现",
            "",
            *self._strategy_lines(active_strategy_id, strategy),
            "",
            "## 为什么",
            "",
            *self._why_lines(market, strategy),
            "",
            "## 现在看什么",
            "",
            *self._next_focus_lines(active_strategy_id, strategy, view_note),
            "",
            "## 证据",
            "",
            *[f"- `{path}`" for path in self._evidence_paths(active_strategy_id)],
            "",
        ]

        report_path = self.output_root / "pm_reports" / f"{run_date}-{suffix}.md"
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text("\n".join(lines), encoding="utf-8")
        return report_path

    def _market_move(self) -> MarketMove | None:
        if not self.market_db.exists():
            return None
        with sqlite3.connect(self.market_db) as con:
            for timeframe in ("5m", "1m"):
                latest = con.execute(
                    """
                    SELECT timestamp, close, provider
                    FROM bars
                    WHERE symbol='GOLD' AND timeframe=?
                    ORDER BY timestamp DESC
                    LIMIT 1
                    """,
                    (timeframe,),
                ).fetchone()
                if not latest:
                    continue
                end_time, end_price, provider = latest
                cutoff = self._parse_time(end_time) - timedelta(hours=REPORT_WINDOW_HOURS)
                start = con.execute(
                    """
                    SELECT timestamp, close
                    FROM bars
                    WHERE symbol='GOLD' AND timeframe=? AND timestamp <= ?
                    ORDER BY timestamp DESC
                    LIMIT 1
                    """,
                    (timeframe, cutoff.isoformat().replace("+00:00", "+00:00")),
                ).fetchone()
                if not start:
                    start = con.execute(
                        """
                        SELECT timestamp, close
                        FROM bars
                        WHERE symbol='GOLD' AND timeframe=?
                        ORDER BY timestamp ASC
                        LIMIT 1
                        """,
                        (timeframe,),
                    ).fetchone()
                high_low = con.execute(
                    """
                    SELECT MAX(high), MIN(low)
                    FROM bars
                    WHERE symbol='GOLD' AND timeframe=? AND timestamp >= ? AND timestamp <= ?
                    """,
                    (timeframe, start[0], end_time),
                ).fetchone()
                high, low = high_low or (None, None)
                return MarketMove(
                    timeframe=timeframe,
                    provider=str(provider),
                    start_time=str(start[0]),
                    end_time=str(end_time),
                    start_price=float(start[1]),
                    end_price=float(end_price),
                    high=float(high if high is not None else max(float(start[1]), float(end_price))),
                    low=float(low if low is not None else min(float(start[1]), float(end_price))),
                )
        return None

    def _strategy_window(self, strategy_id: str, run_date: str, window_start: datetime) -> dict[str, Any]:
        perf = self._latest_path(f"strategies/{strategy_id}/performance/{run_date}.json") or self._latest_path(f"strategies/{strategy_id}/performance/current.json")
        summary = perf.get("summary", {})
        closed = [trade for trade in perf.get("closed_today", []) if self._is_after(trade.get("closed_at"), window_start)]
        open_trades = perf.get("open_trades", [])
        paper_orders = [row for row in self._rows(f"strategies/{strategy_id}/paper_orders/{run_date}.json") if self._is_after(row.get("filled_at") or row.get("requested_at"), window_start)]
        demo_requests = [row for row in self._rows(f"strategies/{strategy_id}/demo_order_requests/{run_date}.json") if self._is_after(row.get("requested_at"), window_start)]
        tickets = self._rows(f"strategies/{strategy_id}/trade_tickets/{run_date}.json")
        cycle = self._latest_path(f"strategies/{strategy_id}/cycle_audit/current.json")

        realized = sum(self._num(item.get("realized_pnl")) for item in closed)
        open_unrealized = sum(self._num(item.get("unrealized_pnl")) for item in open_trades)
        return {
            "summary": summary,
            "closed": closed,
            "open_trades": open_trades,
            "paper_orders": paper_orders,
            "demo_requests": demo_requests,
            "tickets": tickets,
            "cycle": cycle,
            "realized_window": realized,
            "open_unrealized": open_unrealized,
            "tp_count": sum(1 for item in closed if self._is_target_exit(item.get("exit_reason"))),
            "sl_count": sum(1 for item in closed if self._is_stop_exit(item.get("exit_reason"))),
            "other_exit_count": sum(1 for item in closed if not self._is_target_exit(item.get("exit_reason")) and not self._is_stop_exit(item.get("exit_reason"))),
            "long_count": self._side_count(paper_orders, closed, "long"),
            "short_count": self._side_count(paper_orders, closed, "short"),
            "blocked_demo_count": sum(1 for item in demo_requests if str(item.get("status", "")).lower() == "blocked"),
        }

    def _headline(self, strategy_id: str, market: MarketMove | None, strategy: dict[str, Any]) -> str:
        market_text = "黄金行情缺少可读数据" if not market else f"过去 {REPORT_WINDOW_HOURS} 小时黄金 {self._signed_pct(market.pct)}（{self._signed_number(market.points)} 点）"
        orders = len(strategy["paper_orders"]) + len(strategy["demo_requests"])
        closed = len(strategy["closed"])
        realized = strategy["realized_window"]
        if orders == 0 and closed == 0:
            return f"{market_text}；`{strategy_id}` 没有实际开仓/平仓，当前重点是看它为什么没交易。"
        return (
            f"{market_text}；`{strategy_id}` 开仓/请求 {orders} 单，"
            f"平仓 {closed} 单，TP {strategy['tp_count']}、SL {strategy['sl_count']}，"
            f"过去窗口已实现 PnL {self._signed_money(realized)}。"
        )

    def _market_lines(self, market: MarketMove | None) -> list[str]:
        if not market:
            return ["- 没有读到 GOLD 1m/5m 行情；先检查 `data/market_data.db`。"]
        direction = "上涨" if market.pct > 0 else ("下跌" if market.pct < 0 else "横盘")
        return [
            f"- 过去 {REPORT_WINDOW_HOURS} 小时：{direction} {self._signed_pct(market.pct)}，从 {market.start_price:.2f} 到 {market.end_price:.2f}。",
            f"- 区间：高点 {market.high:.2f}，低点 {market.low:.2f}；数据源 {market.provider}，周期 {market.timeframe}。",
            f"- 最新 bar：{market.end_time}。15m 不是原生行情时，不把它当作主要判断来源。",
        ]

    def _strategy_lines(self, strategy_id: str, strategy: dict[str, Any]) -> list[str]:
        summary = strategy.get("summary", {})
        lines = [
            f"- 当前只复盘 active 策略：`{strategy_id}`。",
            f"- 过去 {REPORT_WINDOW_HOURS} 小时：paper filled {len(strategy['paper_orders'])}，demo requests {len(strategy['demo_requests'])}，demo blocked {strategy['blocked_demo_count']}。",
            f"- 平仓结果：TP {strategy['tp_count']}，SL {strategy['sl_count']}，其他 {strategy['other_exit_count']}；窗口已实现 PnL {self._signed_money(strategy['realized_window'])}。",
            f"- 当前 open：{len(strategy['open_trades'])} 单，未实现 PnL {self._signed_money(strategy['open_unrealized'])}。",
            f"- 累计证据：closed {summary.get('closed_all_count', 'n/a')}，realized_all {self._signed_money(summary.get('realized_pnl_all'))}，profit factor {self._fmt_number(summary.get('profit_factor'))}。",
        ]
        if strategy["long_count"] or strategy["short_count"]:
            lines.append(f"- 方向：long {strategy['long_count']}，short {strategy['short_count']}。")
        return lines

    def _why_lines(self, market: MarketMove | None, strategy: dict[str, Any]) -> list[str]:
        if strategy["blocked_demo_count"]:
            reason = self._first_demo_block_reason(strategy["demo_requests"])
            return [
                f"- 这次不是策略主动放弃交易，而是 demo 请求被安全检查拦住：{reason}",
                "- 处理顺序：先确认交易所状态和本地 reconciliation，再讨论策略方向对错。",
            ]
        if not strategy["paper_orders"] and not strategy["closed"]:
            reason = self._cycle_reason(strategy.get("cycle", {}))
            return [f"- 策略没有交易的主要原因：{reason}"]
        if not market:
            return ["- 行情数据缺失，暂时不能判断策略是否顺势或逆势。"]
        if strategy["realized_window"] > 0:
            return [f"- 策略在这段行情里赚钱，主要看它是否站在黄金 {self._market_direction_word(market)} 的同侧，以及 TP 是否真实落袋。"]
        if strategy["realized_window"] < 0:
            return [f"- 策略在这段行情里亏钱，优先检查是否逆着黄金 {self._market_direction_word(market)} 开仓，或信号确认太早。"]
        return ["- 过去窗口没有已实现盈亏，先看 open 单是否会到 TP/SL，不要用浮盈浮亏下结论。"]

    def _next_focus_lines(self, strategy_id: str, strategy: dict[str, Any], view_note: str) -> list[str]:
        lines = []
        if view_note:
            lines.append(f"- 人工观点：{view_note}")
        if strategy["blocked_demo_count"]:
            lines.append(f"- 下一步先查 `{strategy_id}` 的 live_reconciliation，再允许新的 demo 请求。")
        elif strategy["open_trades"]:
            lines.append("- 下一步只看这些 open 单最终是 TP 还是 SL；不要提前按浮盈浮亏评价策略。")
        else:
            lines.append("- 下一步看 active 策略是否在下一段明显行情里给出有效信号；没有信号就接受空仓。")
        return lines[:3]

    def _market_view_note(self, run_date: str) -> str:
        view = self._latest_path("market_views/current.json")
        if not view:
            return "今天没有人工 market view。"
        if view.get("run_date") == run_date:
            return str(view.get("stance") or view.get("direction_bias") or "今天观点已同步。")
        return f"当前 market view 是 {view.get('run_date')}，不是今天观点。"

    def _active_strategy_id(self) -> str:
        demo = self.config.get("demo_trading", {}) or {}
        if demo.get("active_strategy_id"):
            return str(demo["active_strategy_id"])
        samples = self._latest_path("daily_trade_samples/current.json")
        return str(samples.get("active_strategy_id") or "unknown")

    def _latest(self, name: str, run_date: str) -> dict[str, Any]:
        return self._latest_path(f"{name}/{run_date}.json") or self._latest_path(f"{name}/current.json")

    def _latest_path(self, rel_path: str) -> dict[str, Any]:
        rows = self._rows(rel_path)
        return rows[-1] if rows else {}

    def _rows(self, rel_path: str) -> list[dict[str, Any]]:
        path = self.output_root / rel_path
        if not path.exists():
            return []
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
        if isinstance(data, dict):
            return [data]
        return []

    def _evidence_paths(self, strategy_id: str) -> list[str]:
        rels = [
            "health/current.json",
            "market_views/current.json",
            f"strategies/{strategy_id}/performance/current.json",
            f"strategies/{strategy_id}/paper_orders/current.json",
            f"strategies/{strategy_id}/demo_order_requests/current.json",
            f"strategies/{strategy_id}/live_reconciliation/current.json",
            f"strategies/{strategy_id}/cycle_audit/current.json",
        ]
        paths = [self.market_db]
        paths.extend(self.output_root / rel for rel in rels if (self.output_root / rel).exists())
        return [str(path.resolve()) for path in paths if path.exists()]

    def _side_count(self, orders: list[dict[str, Any]], closed: list[dict[str, Any]], side: str) -> int:
        total = sum(1 for item in closed if str(item.get("side", "")).lower() == side)
        for order in orders:
            ticket_id = str(order.get("ticket_id", "")).lower()
            if side == "long" and ("buy" in ticket_id or "long" in ticket_id):
                total += 1
            elif side == "short" and ("sell" in ticket_id or "short" in ticket_id):
                total += 1
        return total

    def _first_demo_block_reason(self, rows: list[dict[str, Any]]) -> str:
        for row in rows:
            if str(row.get("status", "")).lower() != "blocked":
                continue
            guard = row.get("guard", {}) if isinstance(row.get("guard"), dict) else {}
            reason = guard.get("block_reason") or row.get("rejection_reason") or row.get("reason")
            if reason:
                return str(reason)
        return "demo request blocked"

    def _cycle_reason(self, cycle: dict[str, Any]) -> str:
        reasons = cycle.get("no_ticket_reasons", [])
        if isinstance(reasons, list) and reasons:
            first = reasons[-1] if isinstance(reasons[-1], dict) else {}
            return str(first.get("reason") or first.get("reason_code") or "no ticket")
        if cycle.get("error"):
            return str(cycle["error"])
        return str(cycle.get("phase") or cycle.get("status") or "没有有效信号或没有出票")

    def _parse_time(self, value: Any) -> datetime:
        text = str(value or "")
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            return datetime.now(timezone.utc)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)

    def _is_after(self, value: Any, cutoff: datetime) -> bool:
        if not value:
            return False
        return self._parse_time(value) >= cutoff

    def _is_target_exit(self, reason: Any) -> bool:
        return str(reason or "").lower() in {"target", "take_profit", "tp"}

    def _is_stop_exit(self, reason: Any) -> bool:
        return "stop" in str(reason or "").lower() or str(reason or "").lower() == "sl"

    def _market_direction_word(self, market: MarketMove) -> str:
        if market.pct > 0:
            return "上涨"
        if market.pct < 0:
            return "下跌"
        return "横盘"

    def _fmt_number(self, value: Any) -> str:
        if value is None:
            return "n/a"
        try:
            return f"{float(value):.4f}"
        except (TypeError, ValueError):
            return str(value)

    def _signed_number(self, value: Any) -> str:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return "n/a"
        return f"{number:+.2f}"

    def _signed_pct(self, value: Any) -> str:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return "n/a"
        return f"{number:+.2f}%"

    def _signed_money(self, value: Any) -> str:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return "n/a"
        return f"{number:+.2f}"

    def _num(self, value: Any) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0


def report_title(run_date: str, kind: str) -> str:
    if kind not in PM_REPORT_KINDS:
        raise ValueError(f"kind must be one of {sorted(PM_REPORT_KINDS)}")
    display_name, _, _ = PM_REPORT_KINDS[kind]
    return f"黄金交易{display_name} - {run_date}"


def receipt_path(output_root: Path, run_date: str) -> Path:
    return Path(output_root) / "feishu_reports" / f"{run_date}.json"


def verify_report_receipt(output_root: Path, run_date: str, kind: str, source_path: Path) -> dict[str, Any]:
    path = receipt_path(output_root, run_date)
    if not path.exists():
        raise RuntimeError(f"Feishu receipt missing: {path}")
    rows = json.loads(path.read_text(encoding="utf-8"))
    source = Path(source_path).resolve()
    resolved_source = str(source)
    expected_hash = hashlib.sha256(source.read_text(encoding="utf-8").encode("utf-8")).hexdigest()
    for row in reversed(rows if isinstance(rows, list) else []):
        if (
            row.get("kind") == kind
            and row.get("source_path") == resolved_source
            and row.get("delivered") is True
            and row.get("source_sha256") == expected_hash
        ):
            return row
    raise RuntimeError(f"Delivered Feishu receipt with matching content hash not found for {kind}: {resolved_source}")
