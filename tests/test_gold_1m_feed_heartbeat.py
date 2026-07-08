import json

import pytest

from pipelines import gold_1m_feed_heartbeat as heartbeat


def test_gold_1m_feed_heartbeat_returns_import_result(monkeypatch):
    captured = {}

    def fake_import(run_date: str) -> dict:
        captured["run_date"] = run_date
        return {"status": "pass", "imported_rows": 12, "timeframe": "1m"}

    monkeypatch.setattr(heartbeat, "run_binance_usdm_1m_feed_import", fake_import)

    result = heartbeat.run("2026-07-08")

    assert captured["run_date"] == "2026-07-08"
    assert result["status"] == "pass"
    assert result["imported_rows"] == 12


def test_gold_1m_feed_heartbeat_exits_nonzero_on_import_failure(monkeypatch, capsys):
    def fail_import(_run_date: str) -> dict:
        raise RuntimeError("feed unavailable")

    monkeypatch.setattr(heartbeat, "run_binance_usdm_1m_feed_import", fail_import)

    with pytest.raises(SystemExit) as exc:
        heartbeat.main(["--date", "2026-07-08", "--json"])

    assert exc.value.code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "error"
    assert "RuntimeError: feed unavailable" in payload["message"]
