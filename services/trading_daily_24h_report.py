from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from services.config_loader import ROOT, load_pipeline_config
from services.dualtrack_clock import BJ_TZ, cycle_window, cycle_window_from_id


class TradingDaily24hReportBuilder:
    """Build the single CEO-facing report for the two completed 12-hour cycles."""

    def __init__(self, output_root: Path | None = None) -> None:
        config = load_pipeline_config()
        self.output_root = Path(output_root or ROOT / str(config.get("output_root", "outputs")))

    def build(self, *, now: datetime | None = None) -> Path:
        observed_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        report_end = cycle_window(observed_at).start
        report_start = report_end - timedelta(hours=24)
        expected_cycle_ids = self._expected_cycle_ids(report_start, report_end)
        completed = {str(row.get("cycle_id")): row for row in self._completed_reviews(report_start, report_end)}
        missing = [cycle_id for cycle_id in expected_cycle_ids if cycle_id not in completed]
        if missing:
            raise ValueError(f"incomplete 24h review window; missing completed review(s): {', '.join(missing)}")
        reviews = [completed[cycle_id] for cycle_id in expected_cycle_ids]
        report_date = report_end.astimezone(BJ_TZ).date().isoformat()
        cycle_directions: dict[str, list[str]] = {}
        directions: list[str] = []
        for row in reviews:
            cycle_id = str(row.get("cycle_id"))
            fills = self._cycle_fills(cycle_id)
            expected_fill_count = self._num(row.get("fill_count"))
            if len(fills) != expected_fill_count:
                raise ValueError(
                    f"incomplete fill evidence for {cycle_id}; review={expected_fill_count}, artifact={len(fills)}"
                )
            cycle_directions[cycle_id] = self._compressed_entry_directions(fills)
            directions.extend(cycle_directions[cycle_id])
        directions = self._compress(directions)
        direction_changes = sum(1 for before, after in zip(directions, directions[1:]) if before != after)
        total_fills = sum(self._num(row.get("fill_count")) for row in reviews)
        total_trades = sum(self._num(row.get("trade_count")) for row in reviews)
        total_pnl = sum(self._float(row.get("realized_pnl")) for row in reviews)

        lines = [
            f"# 黄金交易 24 小时报告｜{report_date}",
            "",
            "## 成交",
            "",
            f"过去 24 小时共 {total_trades} 笔交易、{total_fills} 笔成交记录，已实现 PnL {total_pnl:+.2f} USD。",
        ]
        lines.extend(self._cycle_line(row, cycle_directions[str(row.get("cycle_id"))]) for row in reviews)
        lines.extend(
            [
                "",
                "## 方向变化",
                "",
                f"方向序列：{' → '.join(self._direction_zh(item) for item in directions) or '缺少证据'}；共变化 {direction_changes} 次。",
                "",
                "## 值得学习",
                "",
                self._learning_paragraph(reviews),
                "",
            ]
        )
        path = self.output_root / "pm_reports" / f"{report_date}-daily-24h.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines), encoding="utf-8")
        return path

    def _expected_cycle_ids(self, start: datetime, end: datetime) -> list[str]:
        cycle_ids: list[str] = []
        cursor = start
        while cursor < end:
            window = cycle_window(cursor)
            if window.start != cursor or window.end > end:
                raise ValueError("24h report window does not align with complete trading cycles")
            cycle_ids.append(window.cycle_id)
            cursor = window.end
        if cursor != end or len(cycle_ids) != 2:
            raise ValueError(f"24h report requires exactly two complete cycles; found {len(cycle_ids)}")
        return cycle_ids

    def _completed_reviews(self, start: datetime, end: datetime) -> list[dict[str, Any]]:
        root = self.output_root / "dualtrack" / "reviews"
        rows: list[tuple[datetime, dict[str, Any]]] = []
        if not root.exists():
            return []
        for path in root.glob("*_machine.json"):
            review = self._latest_dict(path)
            cycle_id = str(review.get("cycle_id") or "")
            if not review.get("completed") or not cycle_id:
                continue
            try:
                window = cycle_window_from_id(cycle_id)
            except ValueError:
                continue
            if start < window.end <= end:
                rows.append((window.start, review))
        return [row for _, row in sorted(rows, key=lambda item: item[0])]

    def _cycle_line(self, row: dict[str, Any], directions: list[str]) -> str:
        tpsl = row.get("tpsl_review") if isinstance(row.get("tpsl_review"), dict) else {}
        executed = " → ".join(self._direction_zh(item) for item in directions) if directions else "空仓"
        return (
            f"- {row.get('cycle_id')}：实际 {executed}，"
            f"交易 {self._num(row.get('trade_count'))} 笔，成交记录 {self._num(row.get('fill_count'))} 笔，"
            f"止盈 {self._num(tpsl.get('target_count'))}、止损 {self._num(tpsl.get('stop_count'))}，"
            f"已实现 {self._float(row.get('realized_pnl')):+.2f} USD。"
        )

    def _learning_paragraph(self, reviews: list[dict[str, Any]]) -> str:
        lessons: list[str] = []
        for row in reviews:
            cycle_id = str(row.get("cycle_id") or "本周期")
            direction = row.get("direction_review") if isinstance(row.get("direction_review"), dict) else {}
            if direction.get("hit") is False and direction.get("summary"):
                lessons.append(f"{cycle_id}：{direction['summary']}")
            range_review = row.get("range_review") if isinstance(row.get("range_review"), dict) else {}
            if str(range_review.get("verdict") or "") == "adjust" and range_review.get("summary"):
                lessons.append(f"{cycle_id}：{range_review['summary']}")
            tpsl = row.get("tpsl_review") if isinstance(row.get("tpsl_review"), dict) else {}
            if self._float(tpsl.get("average_r")) < 1 and tpsl.get("summary"):
                lessons.append(f"{cycle_id}：{tpsl['summary']}")
        unique = list(dict.fromkeys(lessons))[:3]
        if not unique:
            return "过去两个周期没有出现需要升级的新偏差；继续积累样本，不用单日盈亏替代策略结论。"
        latest = reviews[-1] if reviews else {}
        next_iteration = latest.get("next_iteration") if isinstance(latest.get("next_iteration"), dict) else {}
        change = str(next_iteration.get("change") or "").strip()
        base = "；".join(unique) + "。"
        if change:
            base += f" 下一周期只验证一个调整：{change[:160]}"
        return base

    def _cycle_fills(self, cycle_id: str) -> list[dict[str, Any]]:
        path = self.output_root / "dualtrack" / "fills" / f"{cycle_id}_machine.json"
        if not path.exists():
            raise ValueError(f"missing fill artifact for {cycle_id}")
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, list) or any(not isinstance(row, dict) for row in data):
            raise ValueError(f"invalid fill artifact for {cycle_id}")
        return data

    def _compressed_entry_directions(self, fills: list[dict[str, Any]]) -> list[str]:
        ordered = sorted(
            (row for row in fills if str(row.get("event") or "") == "entry"),
            key=lambda row: str(row.get("ts") or ""),
        )
        if not ordered:
            if not fills:
                return ["flat"]
            raise ValueError("fill artifact has executions but no entry evidence")
        directions = [
            "long" if str(row.get("side") or "").lower() == "buy" else "short"
            for row in ordered
            if str(row.get("side") or "").lower() in {"buy", "sell"}
        ]
        return self._compress(directions)

    def _compress(self, directions: list[str]) -> list[str]:
        return [item for index, item in enumerate(directions) if index == 0 or item != directions[index - 1]]

    def _latest_dict(self, path: Path) -> dict[str, Any]:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return next((row for row in reversed(data) if isinstance(row, dict)), {})
        return data if isinstance(data, dict) else {}

    def _direction_zh(self, value: Any) -> str:
        return {"long": "做多", "short": "做空", "neutral": "中性", "flat": "空仓"}.get(str(value or ""), "缺少证据")

    def _num(self, value: Any) -> int:
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0

    def _float(self, value: Any) -> float:
        try:
            return float(value or 0)
        except (TypeError, ValueError):
            return 0.0
