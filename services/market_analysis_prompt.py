from __future__ import annotations

from datetime import date
from pathlib import Path

from services.feishu_report_sender import FeishuReportSender


def build_market_analysis_prompt(run_date: str) -> str:
    return (
        "今天你对黄金市场怎么看？\n"
        "请像昨天那样给我一段口述分析。飞书这里只做提醒；请把口述贴到黄金日报 Codex thread，"
        "我会先保存到 Obsidian「park 原始输出/每日交易分析」，再由这份 note 生成 market_views/current.json，"
        "用于当天方向过滤。\n"
        "你不用写命令，只要把下面这些信息讲清楚：\n\n"
        "1. 大方向分数 0-100\n"
        "   - 0-30：只做空\n"
        "   - 40-60：多空都做\n"
        "   - 70-100：只做多\n\n"
        "2. 多周期判断\n"
        "   3D / 1D / 4H / 15m / 5m 分别支持什么方向？大级别是在高位、低位还是中间？\n\n"
        "3. 关键位置\n"
        "   请说出支撑、压力、EMA50、Fib、整数关口、平台位，以及你最在意的 1-3 个价位。\n\n"
        "4. 今天交易计划\n"
        "   今天是只做多、只做空，还是双向？在哪里等信号？Entry / TP / SL 的逻辑是什么？\n\n"
        "5. 观点过期条件\n"
        "   这条方向不是永久有效的。请至少给一个过期条件：\n"
        "   - 时间：有效到几点，或有效几个小时。\n"
        "   - 目标位：比如强空目标 3950，打到目标后重新评估，不再继续用这条方向过滤。\n"
        "   - 价格偏离：如果价格从参考价反向或顺向移动多少百分比，就认为观点过期。\n"
        "   - 结构失效：上破哪个价位或下破哪个价位后，这条判断失效。\n\n"
        "如果你不指定，系统会用默认规则：12 小时过期，或价格相对参考价偏离 1% 后过期。\n\n"
        f"今天日期：{run_date}\n"
    )


def send_market_analysis_prompt(
    output_root: Path,
    run_date: str | None = None,
    sender=None,
) -> dict:
    effective_date = run_date or date.today().isoformat()
    return FeishuReportSender(output_root, sender=sender).run(
        run_date=effective_date,
        kind="market_analysis_prompt",
        title=f"黄金市场分析提醒 - {effective_date}",
        message=build_market_analysis_prompt(effective_date),
    )
