import json
from pathlib import Path
from urllib.error import HTTPError

from services.journal_store import load_json
from services.oanda_account_preflight import OandaAccountPreflight, run_oanda_account_preflight


class _FakeResponse:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")


def test_oanda_account_preflight_skips_without_credentials(monkeypatch, tmp_path: Path):
    monkeypatch.delenv("OANDA_API_TOKEN", raising=False)
    monkeypatch.delenv("OANDA_ACCOUNT_ID", raising=False)

    result = OandaAccountPreflight(output_root=tmp_path / "outputs").run("2026-05-26")

    assert result["status"] == "skipped"
    assert result["ready"] is False
    assert result["account_ready"] is False
    assert result["instrument_ready"] is False
    assert result["missing_env"] == ["OANDA_API_TOKEN", "OANDA_ACCOUNT_ID"]
    assert "secret-token" not in json.dumps(result)


def test_oanda_account_preflight_reads_live_env_file(monkeypatch, tmp_path: Path):
    env = tmp_path / "live.env"
    env.write_text("OANDA_API_TOKEN=file-token\nOANDA_ACCOUNT_ID=file-account\n", encoding="utf-8")
    monkeypatch.setenv("TRADING_ORCHESTRATOR_LIVE_ENV", str(env))
    monkeypatch.delenv("OANDA_API_TOKEN", raising=False)
    monkeypatch.delenv("OANDA_ACCOUNT_ID", raising=False)

    result = OandaAccountPreflight(output_root=tmp_path / "outputs").preflight_base("2026-05-26")

    assert result["ready"] is True
    assert result["missing_env"] == []
    assert result["env_file"] == str(env)
    assert "file-token" not in json.dumps(result)


def test_oanda_account_preflight_reads_summary_and_instrument(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("OANDA_API_TOKEN", "secret-token")
    monkeypatch.setenv("OANDA_ACCOUNT_ID", "101-001-123")
    seen = []

    def opener(request, timeout):
        seen.append((request.full_url, request.headers["Authorization"], timeout))
        if request.full_url.endswith("/summary"):
            return _FakeResponse(
                {
                    "account": {
                        "id": "101-001-123",
                        "currency": "USD",
                        "balance": "10000.50",
                        "NAV": "10020.25",
                        "marginAvailable": "9000.00",
                        "openTradeCount": "2",
                        "pendingOrderCount": "1",
                        "hedgingEnabled": False,
                    }
                }
            )
        return _FakeResponse(
            {
                "instruments": [
                    {
                        "name": "XAU_USD",
                        "displayName": "Gold",
                        "type": "METAL",
                        "tradeUnitsPrecision": 0,
                        "minimumTradeSize": "1",
                        "marginRate": "0.05",
                    }
                ]
            }
        )

    result = OandaAccountPreflight(output_root=tmp_path / "outputs", opener=opener).run("2026-05-26")

    assert result["status"] == "pass"
    assert result["account_ready"] is True
    assert result["instrument_ready"] is True
    assert result["account"]["account_id"] == "101-001-123"
    assert result["account"]["NAV"] == 10020.25
    assert result["instrument"]["name"] == "XAU_USD"
    assert result["instrument"]["margin_rate"] == 0.05
    assert any("/v3/accounts/101-001-123/summary" in item[0] for item in seen)
    assert any("/v3/accounts/101-001-123/instruments?instruments=XAU_USD" in item[0] for item in seen)
    assert all(item[1] == "Bearer secret-token" for item in seen)
    assert "secret-token" not in json.dumps(result)


def test_oanda_account_preflight_handles_http_error(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("OANDA_API_TOKEN", "secret-token")
    monkeypatch.setenv("OANDA_ACCOUNT_ID", "101-001-123")

    def opener(request, timeout):
        raise HTTPError(request.full_url, 401, "Unauthorized", hdrs=None, fp=None)

    result = OandaAccountPreflight(output_root=tmp_path / "outputs", opener=opener).run("2026-05-26")

    assert result["status"] == "fail"
    assert result["account_ready"] is False
    assert result["instrument_ready"] is False
    assert "401" in result["message"]


def test_oanda_account_pipeline_writes_artifacts(monkeypatch, tmp_path: Path):
    root = tmp_path / "outputs"
    monkeypatch.setenv("TRADING_ORCHESTRATOR_OUTPUT_ROOT", str(root))
    monkeypatch.delenv("OANDA_API_TOKEN", raising=False)
    monkeypatch.delenv("OANDA_ACCOUNT_ID", raising=False)

    result = run_oanda_account_preflight("2026-05-26")

    assert result["status"] == "skipped"
    assert load_json(root / "oanda_account" / "current.json")[0]["status"] == "skipped"
