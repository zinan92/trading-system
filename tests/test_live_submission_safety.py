from pathlib import Path

from services.journal_store import load_json
from services.live_submission_safety import LiveSubmissionSafetySmoke


def test_live_submission_safety_blocks_real_submit_without_activation(monkeypatch, tmp_path: Path):
    root = tmp_path / "outputs"
    monkeypatch.delenv("OANDA_API_TOKEN", raising=False)
    monkeypatch.delenv("OANDA_ACCOUNT_ID", raising=False)

    result = LiveSubmissionSafetySmoke(root).run("2026-05-26")

    assert result["status"] == "pass"
    assert result["blocked_by_activation_gate"] is True
    assert result["network_call_attempted"] is False
    assert "source-bound Live activation/canary is not ready" in result["error"]
    assert load_json(root / "live_submission_safety" / "current.json")[0]["status"] == "pass"
    assert __import__("os").getenv("OANDA_API_TOKEN") is None
    assert __import__("os").getenv("OANDA_ACCOUNT_ID") is None
