import json
from pathlib import Path

from pipelines.testnet_drill import TestnetDrill
from services.journal_store import load_json


class _FakeResponse:
    status = 200

    def __init__(self, payload) -> None:
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")


def _env(tmp_path: Path, monkeypatch) -> None:
    env = tmp_path / "live.env"
    env.write_text("BINANCE_API_KEY=ALPHA123\nBINANCE_API_SECRET=OMEGA456\n", encoding="utf-8")
    monkeypatch.setenv("TRADING_ORCHESTRATOR_LIVE_ENV", str(env))
    monkeypatch.delenv("BINANCE_API_KEY", raising=False)
    monkeypatch.delenv("BINANCE_API_SECRET", raising=False)


def test_testnet_drill_validation_writes_artifact_without_exchange_order(tmp_path: Path, monkeypatch):
    _env(tmp_path, monkeypatch)
    calls = []

    def opener(request, timeout):
        calls.append(request.full_url)
        if "/fapi/v1/ticker/price" in request.full_url:
            return _FakeResponse({"symbol": "XAUUSDT", "price": "4325.10"})
        raise AssertionError(request.full_url)

    output_root = tmp_path / "outputs"
    result = TestnetDrill(output_root=output_root, opener=opener, run_id="drilltest").run(
        "2026-06-09",
        execute_testnet=False,
        scenarios=["always_on_deadman"],
    )

    assert result["status"] == "pass"
    assert result["scenarios"][0]["name"] == "connectivity"
    assert result["scenarios"][0]["status"] == "pass"
    assert result["scenarios"][1]["name"] == "always_on_deadman"
    assert result["scenarios"][1]["evidence"]["deadman"]["ping"]["status"] == "dry_run_fail"
    assert not any(url.endswith("/fapi/v1/order") or url.endswith("/fapi/v1/algoOrder") for url in calls)
    assert load_json(output_root / "testnet_drill" / "drilltest.json")[0]["run_id"] == "drilltest"


def test_testnet_drill_deadman_uses_fail_branch_for_stale_runner_and_unknown_position(tmp_path: Path):
    result = TestnetDrill(output_root=tmp_path / "outputs", run_id="drilltest")._always_on_deadman("2026-06-09")

    assert result["status"] == "pass"
    deadman = result["evidence"]["deadman"]
    assert deadman["always_on"]["status"] == "BLOCKED_ALWAYS_ON_STALE"
    assert deadman["ping"]["status"] == "dry_run_fail"
    assert deadman["ping"]["target_kind"] == "fail"
    assert deadman["severity"] == "critical"
    assert deadman["exposure"]["position_unknown"] is True


def test_testnet_drill_daily_loss_injection_records_canonical_audit_reason(tmp_path: Path):
    root = tmp_path / "outputs" / "scenario"
    result = TestnetDrill(output_root=tmp_path / "outputs", run_id="drilltest")._guardrail_daily_loss_injection(
        root,
        "2026-06-09",
        price=4325.10,
        quantity=0.002,
    )

    assert result["guardrail_status"] == "BLOCKED_DAILY_LOSS_LIMIT"
    assert result["posts"] == 0
    audit = result["cycle_audit"]
    assert audit["trade_permission"]["status"] == "BLOCKED_DAILY_LOSS_LIMIT"
    assert audit["why_not_executed"] == [
        {
                "reason_code": "daily_loss_limit",
                "status": "BLOCKED_DAILY_LOSS_LIMIT",
                "reason": "daily live/testnet loss 2.0000% reached limit 1.0000%",
                "source": "trade_permission.primary_blocker",
            }
        ]
