from pathlib import Path
import hashlib
import sqlite3

import pytest

from services.journal_store import write_json
from services.pm_portfolio_report import PMPortfolioReportBuilder, report_title, verify_report_receipt


def test_builds_pm_morning_report_from_existing_evidence(tmp_path: Path):
    output_root = tmp_path / "outputs"
    market_db = tmp_path / "market.db"
    _write_market_db(market_db)
    write_json(output_root / "health" / "current.json", [{
        "run_date": "2026-07-03",
        "status": "warn",
        "checks": [{"name": "runner", "status": "ok"}, {"name": "daily_review", "status": "warn"}],
    }])
    write_json(output_root / "strategies" / "gold_1m_macd" / "performance" / "current.json", [{
        "run_date": "2026-07-03",
        "summary": {
            "open_trade_count": 1,
            "closed_all_count": 25,
            "realized_pnl_all": 18.5,
            "profit_factor": 0.7,
        },
        "closed_today": [{
            "side": "long",
            "exit_reason": "target",
            "closed_at": "2026-07-03T02:00:00+00:00",
            "realized_pnl": 12.5,
        }],
        "open_trades": [{
            "side": "long",
            "opened_at": "2026-07-03T03:00:00+00:00",
            "unrealized_pnl": 2.0,
        }],
    }])
    write_json(output_root / "strategies" / "gold_1m_macd" / "paper_orders" / "2026-07-03.json", [{
        "ticket_id": "ticket_buy_gold_20260703_1",
        "filled_at": "2026-07-03T02:30:00+00:00",
        "status": "filled",
    }])
    write_json(output_root / "market_views" / "current.json", [{
        "run_date": "2026-07-02",
        "direction_bias": "neutral",
    }])

    path = PMPortfolioReportBuilder(output_root, market_db).build("2026-07-03", "pm_morning")

    text = path.read_text(encoding="utf-8")
    assert path == output_root / "pm_reports" / "2026-07-03-morning.md"
    assert "# 黄金交易早盘复盘 - 2026-07-03" in text
    assert "过去 12 小时黄金 +5.00%" in text
    assert "`gold_1m_macd`" in text
    assert "TP 1，SL 0" in text
    assert "窗口已实现 PnL +12.50" in text
    assert "当前 market view 是 2026-07-02" in text
    assert "## 3. 策略表" not in text
    assert "| 策略 | 家族/周期 |" not in text
    assert "Backend maturity" not in text


def test_verify_report_receipt_requires_delivered_matching_source(tmp_path: Path):
    output_root = tmp_path / "outputs"
    report = output_root / "pm_reports" / "2026-07-03-morning.md"
    report.parent.mkdir(parents=True)
    report.write_text("# report\n", encoding="utf-8")
    write_json(output_root / "feishu_reports" / "2026-07-03.json", [{
        "run_date": "2026-07-03",
        "kind": "pm_morning",
        "source_path": str(report.resolve()),
        "delivered": False,
    }])

    with pytest.raises(RuntimeError, match="matching content hash"):
        verify_report_receipt(output_root, "2026-07-03", "pm_morning", report)

    source_sha256 = hashlib.sha256(report.read_text(encoding="utf-8").encode("utf-8")).hexdigest()
    write_json(output_root / "feishu_reports" / "2026-07-03.json", [{
        "run_date": "2026-07-03",
        "kind": "pm_morning",
        "source_path": str(report.resolve()),
        "delivered": True,
        "source_sha256": source_sha256,
        "generated_at": "2026-07-03T03:00:00+00:00",
    }])

    receipt = verify_report_receipt(output_root, "2026-07-03", "pm_morning", report)
    assert receipt["generated_at"] == "2026-07-03T03:00:00+00:00"


def test_report_title_labels_pm_kinds():
    assert report_title("2026-07-03", "pm_morning") == "黄金交易早盘复盘 - 2026-07-03"
    assert report_title("2026-07-03", "pm_evening") == "黄金交易晚盘复盘 - 2026-07-03"


def _write_market_db(path: Path) -> None:
    con = sqlite3.connect(path)
    con.execute(
        """
        CREATE TABLE bars (
            symbol TEXT NOT NULL,
            timeframe TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            open REAL NOT NULL,
            high REAL NOT NULL,
            low REAL NOT NULL,
            close REAL NOT NULL,
            volume REAL NOT NULL,
            provider TEXT NOT NULL,
            quality_flags TEXT NOT NULL,
            PRIMARY KEY (symbol, timeframe, timestamp)
        )
        """
    )
    con.executemany(
        "INSERT INTO bars VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            ("GOLD", "5m", "2026-07-02T16:00:00+00:00", 4000, 4002, 3998, 4000, 1, "binance_usdm", "[]"),
            ("GOLD", "5m", "2026-07-03T04:00:00+00:00", 4198, 4210, 3990, 4200, 1, "binance_usdm", "[]"),
        ],
    )
    con.commit()
    con.close()
