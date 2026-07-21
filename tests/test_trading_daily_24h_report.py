from datetime import datetime, timezone
from pathlib import Path

import pytest

from services.journal_store import write_json
from services.trading_daily_24h_report import TradingDaily24hReportBuilder


def _review(cycle_id: str, direction: str, *, pnl: float, hit: bool) -> dict:
    return {
        "cycle_id": cycle_id,
        "completed": True,
        "decision": direction,
        "direction_hit": hit,
        "fill_count": 6,
        "trade_count": 3,
        "realized_pnl": pnl,
        "direction_review": {"hit": hit, "summary": f"计划 {direction}，实际 neutral；需要复核方向判定"},
        "range_review": {"verdict": "pass", "summary": "区间通过"},
        "tpsl_review": {"average_r": 1.2, "target_count": 2, "stop_count": 1, "summary": "平均 R 1.20"},
        "next_iteration": {"change": "下一周期只调整方向。"},
    }


def _fills(cycle_id: str, side: str, count: int = 6) -> list[dict]:
    exit_side = "sell" if side == "buy" else "buy"
    rows = []
    for index in range(count // 2):
        rows.append({"fill_id": f"{cycle_id}-e{index}", "event": "entry", "side": side, "ts": f"2026-07-17T0{index}:00:00+00:00"})
    for index in range(count - len(rows)):
        rows.append({"fill_id": f"{cycle_id}-x{index}", "event": "target", "side": exit_side, "ts": f"2026-07-17T1{index}:00:00+00:00"})
    return rows


def test_builds_single_24h_report_with_direction_changes_and_learning(tmp_path: Path) -> None:
    output_root = tmp_path / "outputs"
    write_json(output_root / "dualtrack" / "reviews" / "2026-07-17_DAY_machine.json", [_review("2026-07-17_DAY", "short", pnl=10, hit=True)])
    write_json(output_root / "dualtrack" / "reviews" / "2026-07-17_NIGHT_machine.json", [_review("2026-07-17_NIGHT", "long", pnl=-5, hit=False)])
    write_json(output_root / "dualtrack" / "fills" / "2026-07-17_DAY_machine.json", _fills("2026-07-17_DAY", "buy"))
    write_json(output_root / "dualtrack" / "fills" / "2026-07-17_NIGHT_machine.json", _fills("2026-07-17_NIGHT", "sell"))

    path = TradingDaily24hReportBuilder(output_root).build(now=datetime(2026, 7, 18, 1, 3, tzinfo=timezone.utc))
    text = path.read_text(encoding="utf-8")

    assert path.name == "2026-07-18-daily-24h.md"
    assert "过去 24 小时共 6 笔交易、12 笔成交记录，已实现 PnL +5.00 USD" in text
    assert "2026-07-17_DAY：实际 做多" in text
    assert "2026-07-17_NIGHT：实际 做空" in text
    assert "方向序列：做多 → 做空；共变化 1 次" in text
    assert "需要复核方向判定" in text


def test_ignores_incomplete_or_out_of_window_reviews(tmp_path: Path) -> None:
    output_root = tmp_path / "outputs"
    old = _review("2026-07-16_DAY", "short", pnl=100, hit=True)
    incomplete = _review("2026-07-17_NIGHT", "long", pnl=100, hit=True)
    incomplete["completed"] = False
    write_json(output_root / "dualtrack" / "reviews" / "2026-07-16_DAY_machine.json", [old])
    write_json(output_root / "dualtrack" / "reviews" / "2026-07-17_NIGHT_machine.json", [incomplete])

    with pytest.raises(ValueError, match="missing completed review.*2026-07-17_DAY, 2026-07-17_NIGHT"):
        TradingDaily24hReportBuilder(output_root).build(now=datetime(2026, 7, 18, 1, 3, tzinfo=timezone.utc))


def test_fails_closed_when_fill_artifact_is_missing_or_count_mismatches(tmp_path: Path) -> None:
    output_root = tmp_path / "outputs"
    for cycle_id in ("2026-07-17_DAY", "2026-07-17_NIGHT"):
        write_json(output_root / "dualtrack" / "reviews" / f"{cycle_id}_machine.json", [_review(cycle_id, "long", pnl=0, hit=True)])
    write_json(output_root / "dualtrack" / "fills" / "2026-07-17_DAY_machine.json", _fills("2026-07-17_DAY", "buy"))

    with pytest.raises(ValueError, match="missing fill artifact for 2026-07-17_NIGHT"):
        TradingDaily24hReportBuilder(output_root).build(now=datetime(2026, 7, 18, 1, 3, tzinfo=timezone.utc))


def test_counts_direction_changes_within_a_cycle(tmp_path: Path) -> None:
    output_root = tmp_path / "outputs"
    for cycle_id in ("2026-07-17_DAY", "2026-07-17_NIGHT"):
        write_json(output_root / "dualtrack" / "reviews" / f"{cycle_id}_machine.json", [_review(cycle_id, "neutral", pnl=0, hit=True)])
    day_fills = _fills("2026-07-17_DAY", "buy")
    day_fills[1]["side"] = "sell"
    write_json(output_root / "dualtrack" / "fills" / "2026-07-17_DAY_machine.json", day_fills)
    write_json(output_root / "dualtrack" / "fills" / "2026-07-17_NIGHT_machine.json", _fills("2026-07-17_NIGHT", "sell"))

    text = TradingDaily24hReportBuilder(output_root).build(
        now=datetime(2026, 7, 18, 1, 3, tzinfo=timezone.utc)
    ).read_text(encoding="utf-8")

    assert "方向序列：做多 → 做空 → 做多 → 做空；共变化 3 次" in text
