from __future__ import annotations

import hashlib
import io
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from services import mainnet_observer as obs

SUB = "0x" + "ab" * 20
SH = timezone(timedelta(hours=8))


def at(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=SH).astimezone(timezone.utc)


def config(tmp_path: Path) -> obs.MainnetConfig:
    key = tmp_path / "mainnet.pk"
    key.write_text("HYPERLIQUID_PK=0x" + hashlib.sha256(b"observer-fixture").hexdigest(), encoding="utf-8")
    key.chmod(0o600)
    return obs.MainnetConfig(subaccount=SUB, approval_id="park-mainnet-1", approved_by="park",
                             approved_at=at("2026-09-15T20:00:00"), release_sha="c" * 40, key_path=key)


def readers(values: dict[str, list[object]]):
    def make(name):
        def read(_config):
            value = values[name].pop(0)
            if isinstance(value, Exception):
                raise value
            return Decimal(str(value))
        return read
    return make("broker"), make("public")


# ---- loss calculation ------------------------------------------------------------------

def test_daily_limit_is_reached_by_an_unrealized_drop() -> None:
    now = at("2026-09-16T14:00:00")
    baseline = obs.update_baseline({}, equity=Decimal("500"), now=at("2026-09-16T08:00:30"))
    loss = obs.compute_loss(equity=Decimal("475"), read_at=now, now=now, baseline=baseline)
    assert loss == {"state": "breach", "daily_loss": "25", "total_loss": "25", "breach": "daily"}
    assert baseline["day"]["source"] == "08:00"


def test_day_resets_at_0800_shanghai_not_midnight() -> None:
    baseline = obs.update_baseline({}, equity=Decimal("500"), now=at("2026-09-16T09:00:00"))
    assert obs.trading_day(at("2026-09-17T07:59:00")) == "2026-09-16"
    assert obs.trading_day(at("2026-09-17T08:00:00")) == "2026-09-17"
    same_session = obs.update_baseline(baseline, equity=Decimal("480"), now=at("2026-09-17T07:59:00"))
    assert same_session["day"]["equity"] == "500"
    next_session = obs.update_baseline(same_session, equity=Decimal("480"), now=at("2026-09-17T08:00:10"))
    assert next_session["day"] == {"trading_day": "2026-09-17", "equity": "480", "at": at("2026-09-17T08:00:10").isoformat(), "source": "08:00"}
    assert next_session["starting_equity"] == "500"
    late = obs.update_baseline(same_session, equity=Decimal("480"), now=at("2026-09-17T10:30:00"))
    assert late["day"]["source"] == "first_reading_after_08:00"


def test_total_limit_across_days_and_gains_are_not_losses() -> None:
    baseline = {"starting_equity": "500", "day": {"trading_day": "2026-09-18", "equity": "440"}}
    now = at("2026-09-18T12:00:00")
    assert obs.compute_loss(equity=Decimal("425"), read_at=now, now=now, baseline=baseline)["breach"] == "total"
    up = obs.compute_loss(equity=Decimal("520"), read_at=now, now=now, baseline={"starting_equity": "500", "day": {"trading_day": "2026-09-18", "equity": "510"}})
    assert up == {"state": "ok", "daily_loss": "0", "total_loss": "0", "breach": None}


def test_stale_or_missing_reading_never_reports_ok() -> None:
    now = at("2026-09-16T12:00:00")
    baseline = {"starting_equity": "500", "day": {"trading_day": "2026-09-16", "equity": "500"}}
    assert obs.compute_loss(equity=Decimal("500"), read_at=now - timedelta(seconds=121), now=now, baseline=baseline)["state"] == "stale"
    assert obs.compute_loss(equity=None, read_at=None, now=now, baseline=baseline)["state"] == "stale"


# ---- lock ------------------------------------------------------------------------------

def test_lock_is_written_once_and_park_can_clear_it_only_on_a_later_date(tmp_path: Path) -> None:
    now = at("2026-09-16T23:30:00")
    loss = {"daily_loss": "26", "total_loss": "26"}
    first = obs.trigger_lock(tmp_path, kind="daily", loss=loss, equity=Decimal("474"), now=now)
    again = obs.trigger_lock(tmp_path, kind="total", loss=loss, equity=Decimal("400"), now=now + timedelta(minutes=1))
    assert again == first and first["local_date"] == "2026-09-16"

    assert obs.unlock(tmp_path, operator="wendy", now=at("2026-09-17T09:00:00")) == {"ok": False, "reason": "operator_not_park"}
    assert obs.unlock(tmp_path, operator="park", now=at("2026-09-16T23:59:00")) == {"ok": False, "reason": "same_day", "unlock_from": "2026-09-17"}
    assert obs.read_lock(tmp_path) is not None
    assert obs.unlock(tmp_path, operator="park", now=at("2026-09-17T00:01:00")) == {"ok": True, "reason": "unlocked"}
    assert obs.read_lock(tmp_path) is None
    events = [json.loads(line)["event"] for line in (tmp_path / "mainnet/lock_history.jsonl").read_text().splitlines()]
    assert events == ["locked", "unlocked"]


def test_unreadable_lock_stays_locked(tmp_path: Path) -> None:
    (tmp_path / "mainnet").mkdir()
    (tmp_path / "mainnet/lock.json").write_text("{not json", encoding="utf-8")
    assert obs.read_lock(tmp_path)["kind"] == "unreadable"
    assert obs.unlock(tmp_path, operator="park", now=at("2027-01-01T12:00:00"))["ok"] is False


# ---- observation -----------------------------------------------------------------------

def test_not_configured_makes_no_reading(tmp_path: Path) -> None:
    def boom(_config):
        raise AssertionError("must not read")
    status = obs.observe_once(tmp_path, now=at("2026-09-16T12:00:00"), config=None, broker_reader=boom, public_reader=boom)
    assert status["state"] == "not_configured"
    assert not (tmp_path / "mainnet/snapshots").exists()


def test_config_load_requires_both_files_and_park_approval(tmp_path: Path) -> None:
    cfg, key = tmp_path / "m.json", tmp_path / "m.pk"
    assert obs.MainnetConfig.load(cfg, key) is None
    key.write_text("x", encoding="utf-8")
    cfg.write_text(json.dumps({"subaccount": SUB, "approval_id": "a", "approved_by": "wendy",
                               "approved_at": "2026-09-15T20:00:00+08:00", "release_sha": "c" * 40}), encoding="utf-8")
    with pytest.raises(obs.MainnetObserverError, match="mainnet_config_invalid"):
        obs.MainnetConfig.load(cfg, key)
    cfg.write_text(cfg.read_text().replace("wendy", "park"), encoding="utf-8")
    assert obs.MainnetConfig.load(cfg, key).subaccount == SUB


def test_observation_records_both_readers_and_locks_on_breach(tmp_path: Path) -> None:
    cfg = config(tmp_path)
    broker, public = readers({"broker": [500, 474], "public": [500.2, 474.1]})
    first = obs.observe_once(tmp_path, now=at("2026-09-16T08:00:20"), config=cfg, broker_reader=broker, public_reader=public)
    assert first["state"] == "ok" and first["diff"] == "0.2" and first["lock"] is None
    second = obs.observe_once(tmp_path, now=at("2026-09-16T15:00:00"), config=cfg, broker_reader=broker, public_reader=public)
    assert second["state"] == "breach" and second["daily_loss"] == "26"
    assert second["lock"]["kind"] == "daily"
    rows = (tmp_path / "mainnet/snapshots/2026-09-16.jsonl").read_text().splitlines()
    assert len(rows) == 2
    assert obs.mainnet_status(tmp_path)["lock"]["kind"] == "daily"


def test_broker_failure_is_stale_and_never_reseeds_baseline(tmp_path: Path) -> None:
    cfg = config(tmp_path)
    broker, public = readers({"broker": [500, obs.MainnetObserverError("broker_equity_unavailable:X")], "public": [500, 499]})
    obs.observe_once(tmp_path, now=at("2026-09-16T09:00:00"), config=cfg, broker_reader=broker, public_reader=public)
    status = obs.observe_once(tmp_path, now=at("2026-09-16T09:01:00"), config=cfg, broker_reader=broker, public_reader=public)
    assert status["state"] == "stale"
    assert status["errors"] == {"broker_error": "broker_equity_unavailable:X"}

    (tmp_path / "mainnet/baseline.json").write_text("garbage", encoding="utf-8")
    broker, public = readers({"broker": [400], "public": [400]})
    status = obs.observe_once(tmp_path, now=at("2026-09-16T09:02:00"), config=cfg, broker_reader=broker, public_reader=public)
    assert status["state"] == "baseline_unreadable"
    assert (tmp_path / "mainnet/baseline.json").read_text() == "garbage"


def test_public_reader_parses_account_value_and_redacts_failures(tmp_path: Path) -> None:
    cfg = config(tmp_path)
    sent = {}

    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def opener(request, timeout):
        sent.update(json.loads(request.data))
        return Response(json.dumps({"marginSummary": {"accountValue": "512.34"}}).encode())

    assert obs.public_equity(cfg, opener=opener) == Decimal("512.34")
    assert sent == {"type": "clearinghouseState", "user": SUB}

    def failing(request, timeout):
        raise OSError("connection reset with secret-looking context")
    with pytest.raises(obs.MainnetObserverError) as raised:
        obs.public_equity(cfg, opener=failing)
    assert str(raised.value) == "public_equity_unavailable"


# ---- dry-run report --------------------------------------------------------------------

def test_report_passes_only_with_48h_coverage_and_small_differences(tmp_path: Path) -> None:
    folder = tmp_path / "mainnet/snapshots"
    folder.mkdir(parents=True)
    now = at("2026-09-18T12:00:00")
    start = now - timedelta(hours=48)
    rows = [{"at": (start + timedelta(minutes=i)).isoformat(), "diff": "0.10"} for i in range(0, 48 * 60, 1) if i % 25]
    (folder / "all.jsonl").write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    report = obs.dry_run_report(tmp_path, now=now)
    assert report["passed"] is True and report["coverage"] >= 0.95

    rows[100]["diff"] = "0.51"
    (folder / "all.jsonl").write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    assert obs.dry_run_report(tmp_path, now=now)["passed"] is False

    short = obs.dry_run_report(tmp_path, now=start + timedelta(hours=24))
    assert short["passed"] is False


# ---- standard-broker path --------------------------------------------------------------

class FakeExchange:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def load_instrument_definitions(self, **kwargs):
        self.calls.append("load_instrument_definitions")
        return [SimpleNamespace(id="BTC-USD-PERP.HYPERLIQUID", raw_symbol="BTC", info={"asset_index": 0}, size_precision=5)]

    def cache_instrument(self, instrument):
        pass

    async def request_account_state(self):
        self.calls.append("request_account_state")
        return {"type": "AccountState", "account_id": f"{SUB}-HYPERLIQUID", "event_id": "e-1", "reported": True,
                "base_currency": "USDC", "balances": [{"currency": "USDC", "total": "498.50", "free": "498.50", "locked": "0"}],
                "margins": []}

    async def submit_order(self, *args, **kwargs):  # pragma: no cover - must never be called
        self.calls.append("submit_order")


def test_broker_reader_goes_through_the_readonly_mainnet_profile(tmp_path: Path) -> None:
    pytest.importorskip("standard_broker.adapters.hyperliquid", reason="standard-broker checkout required")
    from standard_broker.adapters import hyperliquid
    if not hasattr(hyperliquid, "NautilusHyperliquidMainnetReadOnlyBackend"):
        pytest.skip("standard-broker without the read-only Mainnet profile (zinan92/standard-broker#139)")
    exchange = FakeExchange()
    seen: list[str] = []
    equity = obs.broker_equity(config(tmp_path), client_factory=lambda key, account: seen.append(account) or exchange)
    assert equity == Decimal("498.50")
    assert seen == [SUB]
    assert "submit_order" not in exchange.calls


def test_cli_observe_without_setup_reports_not_configured(tmp_path: Path, monkeypatch, capsys) -> None:
    from pipelines import mainnet_observer as cli
    monkeypatch.setattr(obs.MainnetConfig, "load", classmethod(lambda cls, *a, **k: None))
    assert cli.main(["observe", "--output-root", str(tmp_path)]) == 0
    assert json.loads(capsys.readouterr().out)["state"] == "not_configured"
