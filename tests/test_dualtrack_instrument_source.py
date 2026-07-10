from __future__ import annotations

import json
from pathlib import Path

import pytest

import pipelines.dualtrack_nautilus_shadow_prepare as prepare_pipeline
from services.dualtrack_instrument_source import fetch_execution_instrument_definition
from services.journal_store import load_json
from tests.test_dualtrack_nautilus_instrument import _definition


class _Response:
    status = 200

    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")


def test_fetches_and_validates_execution_venue_instrument_definition() -> None:
    seen = {}

    def opener(request, *, timeout):
        seen["url"] = request.full_url
        seen["timeout"] = timeout
        return _Response(_definition())

    definition = fetch_execution_instrument_definition(endpoint="http://datafeed.test/api/instruments/commodity/XAUUSDT", opener=opener)

    assert definition["instrument_id"] == "XAUUSDT.BINANCE"
    assert "source=binance_usdm_futures" in seen["url"]
    assert "require_execution_venue=true" in seen["url"]
    assert seen["timeout"] == 8.0


def test_fetch_rejects_untrusted_instrument_definition() -> None:
    definition = _definition()
    definition["served_from"] = "cache"

    with pytest.raises(ValueError, match="upstream"):
        fetch_execution_instrument_definition(opener=lambda *args, **kwargs: _Response(definition))


def test_preflight_persists_explicit_paper_only_fee_model(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(prepare_pipeline, "fetch_execution_instrument_definition", lambda **kwargs: _definition())

    assert prepare_pipeline.main(["--output-root", str(tmp_path / "outputs")]) == 0

    artifact = load_json(tmp_path / "outputs" / "dualtrack" / "nautilus" / "instrument_preflight.json")[-1]
    assert artifact["status"] == "ready_for_paper_shadow"
    assert artifact["instrument"]["symbol"] == "XAUUSDT"
    assert artifact["fee_model"]["mode"] == "paper_assumption"
    assert artifact["fee_model"]["real_money_eligible"] is False
    assert artifact["blockers"] == []


def test_preflight_blocks_when_paper_fee_model_is_not_explicit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(prepare_pipeline, "fetch_execution_instrument_definition", lambda **kwargs: _definition())
    monkeypatch.setattr(prepare_pipeline, "dualtrack_config", lambda: {"execution_shadow": {"nautilus": {}}})

    assert prepare_pipeline.main(["--output-root", str(tmp_path / "outputs")]) == 0

    artifact = load_json(tmp_path / "outputs" / "dualtrack" / "nautilus" / "instrument_preflight.json")[-1]
    assert artifact["status"] == "blocked"
    assert artifact["blockers"] == ["shadow fee model is missing maker_fee_rate or taker_fee_rate", "shadow fee model must explicitly be paper-only"]
