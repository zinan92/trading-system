import json
from pathlib import Path

from services.journal_store import load_json
from services.market_store import MarketStore
from services.oanda_feed_client import OandaFeedClient, run_oanda_feed_import


class _FakeResponse:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")


def test_oanda_feed_preflight_skips_without_credentials(monkeypatch, tmp_path: Path):
    monkeypatch.delenv("OANDA_API_TOKEN", raising=False)
    monkeypatch.delenv("OANDA_ACCOUNT_ID", raising=False)
    store = MarketStore(tmp_path / "market.db")

    result = OandaFeedClient(store).fetch_and_store()

    assert result["status"] == "skipped"
    assert result["ready"] is False
    assert result["missing_env"] == ["OANDA_API_TOKEN", "OANDA_ACCOUNT_ID"]
    assert result["imported_rows"] == 0


def test_oanda_feed_preflight_reads_configs_live_env(monkeypatch, tmp_path: Path):
    env = tmp_path / "live.env"
    env.write_text("OANDA_API_TOKEN=file-token\nOANDA_ACCOUNT_ID=file-account\n", encoding="utf-8")
    monkeypatch.setenv("TRADING_ORCHESTRATOR_LIVE_ENV", str(env))
    monkeypatch.delenv("OANDA_API_TOKEN", raising=False)
    monkeypatch.delenv("OANDA_ACCOUNT_ID", raising=False)
    store = MarketStore(tmp_path / "market.db")

    result = OandaFeedClient(store).preflight()

    assert result["ready"] is True
    assert result["missing_env"] == []
    assert result["env_file"] == str(env)
    assert "file-token" not in json.dumps(result)


def test_oanda_feed_preflight_treats_live_env_placeholders_as_missing(monkeypatch, tmp_path: Path):
    env = tmp_path / "live.env"
    env.write_text("OANDA_API_TOKEN=CHANGE_ME\nOANDA_ACCOUNT_ID=your_account_id\n", encoding="utf-8")
    monkeypatch.setenv("TRADING_ORCHESTRATOR_LIVE_ENV", str(env))
    monkeypatch.delenv("OANDA_API_TOKEN", raising=False)
    monkeypatch.delenv("OANDA_ACCOUNT_ID", raising=False)
    store = MarketStore(tmp_path / "market.db")

    result = OandaFeedClient(store).preflight()

    assert result["ready"] is False
    assert result["missing_env"] == ["OANDA_API_TOKEN", "OANDA_ACCOUNT_ID"]


def test_oanda_feed_imports_complete_midpoint_m5_bars(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("OANDA_API_TOKEN", "token")
    monkeypatch.setenv("OANDA_ACCOUNT_ID", "acct")
    seen = {}

    def opener(request, timeout):
        seen["url"] = request.full_url
        seen["auth"] = request.headers["Authorization"]
        seen["timeout"] = timeout
        return _FakeResponse(
            {
                "instrument": "XAU_USD",
                "granularity": "M5",
                "candles": [
                    {
                        "complete": True,
                        "time": "2026-05-26T03:00:00.000000000Z",
                        "mid": {"o": "4535.1", "h": "4537.0", "l": "4534.5", "c": "4536.0"},
                        "volume": 12,
                    },
                    {
                        "complete": False,
                        "time": "2026-05-26T03:05:00.000000000Z",
                        "mid": {"o": "4536.0", "h": "4538.0", "l": "4535.5", "c": "4537.1"},
                        "volume": 9,
                    },
                ],
            }
        )

    store = MarketStore(tmp_path / "market.db")
    result = OandaFeedClient(store, opener=opener).fetch_and_store(count=2)
    bars = store.load_bars("GOLD", "5m", 10)

    assert result["status"] == "pass"
    assert result["imported_rows"] == 1
    assert "granularity=M5" in seen["url"]
    assert "price=M" in seen["url"]
    assert seen["auth"] == "Bearer token"
    assert bars[0].timestamp == "2026-05-26T03:00:00+00:00"
    assert bars[0].close == 4536.0
    assert bars[0].provider == "oanda"
    assert bars[0].quality_flags == ["oanda_rest", "official_broker_feed"]


def test_oanda_feed_pipeline_writes_artifacts_when_credentials_missing(monkeypatch, tmp_path: Path):
    root = tmp_path / "outputs"
    db_path = tmp_path / "market.db"
    monkeypatch.setenv("TRADING_ORCHESTRATOR_OUTPUT_ROOT", str(root))
    monkeypatch.setenv("TRADING_ORCHESTRATOR_MARKET_DB", str(db_path))
    monkeypatch.delenv("OANDA_API_TOKEN", raising=False)
    monkeypatch.delenv("OANDA_ACCOUNT_ID", raising=False)

    result = run_oanda_feed_import("2026-05-26")

    assert result["status"] == "skipped"
    assert result["market_db"] == str(db_path)
    assert load_json(root / "oanda_feed" / "current.json")[0]["status"] == "skipped"
