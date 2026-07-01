"""The strategies CLI refreshes the live 1m tail before running strategies so the
chan engine signals on current structure — and a feed failure must never block
the strategy run (best-effort)."""

import sys

import pipelines.strategies as strat


def test_refresh_1m_feed_returns_import_result(monkeypatch):
    monkeypatch.setattr(strat, "run_binance_usdm_1m_feed_import", lambda run_date: {"status": "pass", "imported_rows": 3, "timeframe": "1m"})
    result = strat.refresh_1m_feed("2026-06-02")
    assert result["status"] == "pass"
    assert result["imported_rows"] == 3


def test_refresh_1m_feed_is_best_effort_on_failure(monkeypatch):
    def boom(run_date):
        raise OSError("binance unreachable")

    monkeypatch.setattr(strat, "run_binance_usdm_1m_feed_import", boom)
    result = strat.refresh_1m_feed("2026-06-02")
    # Swallowed into an error record, NOT raised — the strategy run must proceed.
    assert result["status"] == "error"
    assert "binance unreachable" in result["message"]


def test_strategies_cli_default_date_is_utc_trading_day(monkeypatch, capsys):
    captured = {}

    class _Runner:
        def run(self, run_date, paper_auto_approve=False):
            captured["run_date"] = run_date
            captured["paper_auto_approve"] = paper_auto_approve
            return {"run_date": run_date, "strategies": []}

    monkeypatch.setattr(strat, "utc_run_date", lambda: "2026-06-09")
    monkeypatch.setattr(strat, "MultiStrategyRunner", lambda: _Runner())
    monkeypatch.setattr(sys, "argv", ["strategies", "--skip-feed-refresh", "--paper-auto-approve"])

    strat.main()

    assert captured == {"run_date": "2026-06-09", "paper_auto_approve": True}
    assert '"run_date": "2026-06-09"' in capsys.readouterr().out
