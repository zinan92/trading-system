"""#1406: Paper reads its instrument from the Strategy Session (ADR 0009).

Market, contract, fees and fill receipts come from the instrument the session
recorded when it started. Gold behaves exactly as before; silver and crude read
their own OKX data and never fall back to gold.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from services.dualtrack_execution_contract import normalize_execution_command
from services.park_ai_chat import ParkAiChatError, ParkAiChatService
from services.park_paper_runtime import ParkPaperRuntime, ParkPaperRuntimeError, prepare_park_paper_config
from services.park_strategy_session import ParkStrategyIdentityError, ParkStrategyIdentityJournal
from services.park_telegram_runtime import ParkTelegramRuntimeError, default_market_reader
from tests.test_park_ai_chat import FakeProvider, _account, _candidate
from tests.test_park_paper_runtime import FakePaperAdapter, _config, _evidence

DUALTRACK = {
    "paper_gold_venue": {"active": "okx"},
    "execution_shadow": {
        "nautilus": {
            "instrument_endpoint": "http://127.0.0.1:8100/api/instruments/commodity/XAU-USDT-SWAP",
            "instrument_source": "okx_swap_public",
        }
    },
    "execution_contract": {
        "schema_version": "dualtrack-execution-contract-v1",
        "price_precision": 1,
        "price_increment": "0.1",
        "quantity_precision": 3,
        "quantity_increment": "0.001",
        "execution_instrument_id": "XAU-USDT-SWAP",
    },
    "paper_fee_model": {"maker_fee_rate": "0", "taker_fee_rate": "0.000400"},
    "capital_per_track_usd": 10000,
    "max_leverage": 10,
}

SPECS = {
    "XAU-USDT-SWAP": ("1", "0.1", "3", "0.001"),
    "XAG-USDT-SWAP": ("2", "0.01", "2", "0.01"),
    "CL-USDT-SWAP": ("2", "0.01", "1", "0.1"),
}


def _definition(symbol: str, **overrides) -> dict:
    price_precision, price_increment, size_precision, size_increment = SPECS[symbol]
    value = {
        "schema_version": "instrument-definition-v1",
        "instrument_id": f"{symbol}.OKX",
        "venue": "OKX",
        "symbol": symbol,
        "asset_class": "commodity",
        "status": "TRADING",
        "source_mode": "okx_swap_public",
        "provider": "okx_swap_public",
        "price_precision": int(price_precision),
        "price_increment": price_increment,
        "size_precision": int(size_precision),
        "size_increment": size_increment,
        "contract_multiplier": "1",
    }
    value.update(overrides)
    return value


@pytest.fixture
def fetches(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every definition read, by URL path; the datafeed answers from SPECS."""

    calls: list[str] = []

    def fetch(*, endpoint: str, source: str, **_kwargs) -> dict:
        calls.append(f"{endpoint}?source={source}")
        return _definition(endpoint.rsplit("/", 1)[-1])

    monkeypatch.setattr("services.dualtrack_config.dualtrack_config", lambda: json.loads(json.dumps(DUALTRACK)))
    monkeypatch.setattr("services.dualtrack_instrument_source.fetch_execution_instrument_definition", fetch)
    monkeypatch.setattr("services.paper_instruments.fetch_execution_instrument_definition", fetch)
    return calls


# --- the session record ------------------------------------------------------


def test_a_session_records_its_instrument_and_older_sessions_read_as_gold(tmp_path: Path) -> None:
    journal = ParkStrategyIdentityJournal(tmp_path)
    gold = journal.start_clean_session(observed_at="2026-11-07T10:00:00+00:00", plan_digest="sha256:" + "1" * 64,
                                       reconciliation_healthy=True)
    assert gold["paper_instrument"] == "gold"
    journal.close_session(strategy_session_id=gold["strategy_session_id"], strategy_revision_id=gold["strategy_revision_id"],
                          observed_at="2026-11-07T10:01:00+00:00", reason="test")
    silver = journal.start_clean_session(observed_at="2026-11-07T10:02:00+00:00", plan_digest="sha256:" + "2" * 64,
                                         reconciliation_healthy=True, paper_instrument="silver")
    assert journal.active_session()["paper_instrument"] == "silver" == silver["paper_instrument"]

    from services.paper_instruments import session_instrument_key

    assert session_instrument_key({"strategy_session_id": "before-1406"}) == "gold"


def test_an_unknown_instrument_cannot_start_a_session(tmp_path: Path) -> None:
    journal = ParkStrategyIdentityJournal(tmp_path)
    with pytest.raises(ParkStrategyIdentityError):
        journal.start_clean_session(observed_at="2026-11-07T10:00:00+00:00", plan_digest="sha256:" + "1" * 64,
                                    reconciliation_healthy=True, paper_instrument="copper")
    assert journal.active_session() is None


# --- contract and fees -------------------------------------------------------


@pytest.mark.parametrize("key,symbol", [("silver", "XAG-USDT-SWAP"), ("crude", "CL-USDT-SWAP")])
def test_silver_and_crude_take_definition_contract_and_unverified_fees_from_their_own_instrument(
    fetches: list[str], key: str, symbol: str
) -> None:
    resolved = prepare_park_paper_config({"feature_enabled": True}, instrument=key)["paper_execution"]

    assert resolved["paper_instrument"] == key
    assert resolved["instrument"]["symbol"] == symbol
    _, price_increment, _, size_increment = SPECS[symbol]
    assert resolved["execution_contract"]["price_increment"] == price_increment
    assert resolved["execution_contract"]["quantity_increment"] == size_increment
    assert resolved["execution_contract"]["execution_instrument_id"] == symbol
    assert resolved["paper_fee_model"]["taker_fee_rate"] == "0.000400"
    assert resolved["paper_fee_model"]["verified"] is False
    assert fetches == [f"http://127.0.0.1:8100/api/instruments/commodity/{symbol}?source=okx_swap_public"]


def test_gold_is_prepared_exactly_as_before(fetches: list[str]) -> None:
    resolved = prepare_park_paper_config({"feature_enabled": True}, instrument="gold")

    assert resolved == prepare_park_paper_config({"feature_enabled": True})
    assert set(resolved["paper_execution"]) == {"instrument", "paper_fee_model"}
    assert "verified" not in resolved["paper_execution"]["paper_fee_model"]
    assert fetches[0] == "http://127.0.0.1:8100/api/instruments/commodity/XAU-USDT-SWAP?source=okx_swap_public"


@pytest.mark.parametrize(
    "answer,code",
    [
        (RuntimeError("datafeed down"), "paper_instrument_unavailable"),
        (_definition("XAG-USDT-SWAP", contract_multiplier="10"), "paper_instrument_multiplier"),
        (_definition("XAG-USDT-SWAP", status="SUSPENDED"), "paper_instrument_not_trading"),
        (_definition("XAU-USDT-SWAP"), "paper_gold_venue_mismatch"),
    ],
)
def test_an_unusable_silver_definition_blocks_and_never_becomes_gold(
    monkeypatch: pytest.MonkeyPatch, fetches: list[str], answer, code: str
) -> None:
    def fetch(**_kwargs):
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr("services.paper_instruments.fetch_execution_instrument_definition", fetch)
    with pytest.raises(ParkPaperRuntimeError) as caught:
        prepare_park_paper_config({"feature_enabled": True}, instrument="silver")
    assert caught.value.code == code


def test_a_contract_prepared_for_another_instrument_is_refetched_not_reused(fetches: list[str]) -> None:
    gold = prepare_park_paper_config({"feature_enabled": True}, instrument="gold")
    silver = prepare_park_paper_config(gold, instrument="silver")["paper_execution"]
    assert silver["instrument"]["symbol"] == "XAG-USDT-SWAP" and silver["paper_instrument"] == "silver"

    back = prepare_park_paper_config({"feature_enabled": True, "paper_execution": silver}, instrument="gold")
    assert back == gold


def test_a_silver_order_is_rounded_to_silver_increments_not_gold(fetches: list[str]) -> None:
    paper_execution = prepare_park_paper_config({"feature_enabled": True}, instrument="silver")["paper_execution"]
    configured = json.loads(json.dumps(DUALTRACK))
    configured.update(paper_execution)  # exactly what build_park_authoritative_adapter does

    order = normalize_execution_command({"price": 52.137, "quantity": 0.123}, configured)

    assert order["quantity"] == 0.12  # gold's 0.001 would keep 0.123
    assert order["price"] == 52.14
    assert order["execution_contract"]["execution_instrument_id"] == "XAG-USDT-SWAP"


# --- market ------------------------------------------------------------------


def _bars(symbol: str | None = None, *, provider_symbol: str | None = None, source_mode: str = "okx_swap_public") -> dict:
    return {
        "status": "ready",
        "is_synthetic": False,
        "provider": "okx",
        "source_mode": source_mode,
        "provider_symbol": provider_symbol if provider_symbol is not None else (symbol or "XAU-USDT-SWAP"),
        "latest_close": 52.2,
        "latest_timestamp": "2026-11-07T10:00:00+00:00",
        "fresh": True,
    }


@pytest.fixture
def bar_reads(monkeypatch: pytest.MonkeyPatch, fetches: list[str]) -> list[dict]:
    calls: list[dict] = []

    def read(**kwargs):
        calls.append(kwargs)
        return _bars(kwargs.get("symbol"))

    monkeypatch.delenv("TRADING_ORCHESTRATOR_MARKET_SOURCE", raising=False)
    monkeypatch.setattr("pipelines.dashboard_server.build_dualtrack_market_bars_response", read)
    return calls


def test_gold_reads_its_market_exactly_as_before(bar_reads: list[dict]) -> None:
    market = default_market_reader(instrument="gold")
    assert bar_reads == [{"timeframe": "1m", "limit": 240}]
    assert "instrument_id" not in market and market["trusted"] is True


def test_silver_reads_its_own_okx_bars_and_names_its_instrument(bar_reads: list[dict]) -> None:
    market = default_market_reader(instrument="silver")
    assert bar_reads == [{"symbol": "XAG-USDT-SWAP", "timeframe": "1m", "limit": 240}]
    assert market["instrument_id"] == market["symbol"] == "XAG-USDT-SWAP"
    assert market["trusted"] is True and market["fresh"] is True


@pytest.mark.parametrize("answer", [_bars(provider_symbol="XAU-USDT-SWAP"), _bars("XAG-USDT-SWAP", source_mode="binance_usdm_futures")])
def test_bars_for_another_instrument_or_source_are_not_silvers_price(
    monkeypatch: pytest.MonkeyPatch, bar_reads: list[dict], answer: dict
) -> None:
    monkeypatch.setattr("pipelines.dashboard_server.build_dualtrack_market_bars_response", lambda **_kwargs: answer)
    assert default_market_reader(instrument="silver")["trusted"] is False


def test_datafeed_mode_blocks_silver_rather_than_falling_through(monkeypatch: pytest.MonkeyPatch, bar_reads: list[dict]) -> None:
    from services.datafeed_execution_market_client import DatafeedExecutionMarketClient, ExecutionMarketUnavailable

    def unsupported(self, *, venue: str, instrument_id: str):
        raise ExecutionMarketUnavailable(f"unsupported_instrument {venue}/{instrument_id}")

    monkeypatch.setenv("TRADING_ORCHESTRATOR_MARKET_SOURCE", "datafeed")
    monkeypatch.setattr(DatafeedExecutionMarketClient, "read", unsupported)
    with pytest.raises(ParkTelegramRuntimeError) as caught:
        default_market_reader(instrument="silver")
    assert caught.value.code == "market_unavailable" and "XAG-USDT-SWAP.OKX" in str(caught.value)
    assert bar_reads == []


def test_reads_without_an_instrument_follow_the_running_session(tmp_path: Path, bar_reads: list[dict]) -> None:
    default_market_reader(output_root=tmp_path)
    ParkStrategyIdentityJournal(tmp_path).start_clean_session(
        observed_at="2026-11-07T10:00:00+00:00", plan_digest="sha256:" + "1" * 64, reconciliation_healthy=True,
        paper_instrument="crude",
    )
    default_market_reader(output_root=tmp_path)
    assert [call.get("symbol") for call in bar_reads] == [None, "CL-USDT-SWAP"]


# --- runtime -----------------------------------------------------------------


def _silver_session(output: Path) -> dict:
    return ParkStrategyIdentityJournal(output).start_clean_session(
        observed_at="2026-08-14T10:00:00+00:00", plan_digest="sha256:" + "3" * 64, reconciliation_healthy=True,
        strategy_session_id="session-silver", strategy_revision_id="revision-silver", paper_instrument="silver",
    )


def _runtime(output: Path, adapter: FakePaperAdapter, config: dict, market_reader=None) -> ParkPaperRuntime:
    return ParkPaperRuntime(
        output, adapter=adapter, park_user_id="park-user", chat_id="park-chat", config=config,
        market_reader=market_reader, now=lambda: "2026-08-14T10:00:00+00:00", safety_evidence_reader=_evidence,
    )


def test_a_silver_session_on_a_gold_contract_is_blocked_before_any_order(tmp_path: Path, fetches: list[str]) -> None:
    _silver_session(tmp_path)
    adapter = FakePaperAdapter()
    gold_config = prepare_park_paper_config(_config(), instrument="gold")

    result = _runtime(tmp_path, adapter, gold_config, market_reader=lambda: {"price": 52.2}).run_once()

    assert result["code"] == "paper_instrument_mismatch"
    assert adapter.submit_calls == [] and adapter.process_calls == []


def test_a_silver_session_reads_silvers_market_on_silvers_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fetches: list[str]
) -> None:
    _silver_session(tmp_path)
    asked: list[str | None] = []

    def reader(*, output_root=None, instrument=None):
        asked.append(instrument)
        raise ParkTelegramRuntimeError("market_unavailable", "test stops here")

    monkeypatch.setattr("services.park_telegram_runtime.default_market_reader", reader)
    silver_config = prepare_park_paper_config(_config(), instrument="silver")
    runtime = _runtime(tmp_path, FakePaperAdapter(), silver_config)

    assert runtime._paper_instrument_blocker(runtime.identity.active_session(), observed_at="2026-08-14T10:00:00+00:00") is None
    assert runtime._paper_instrument_symbol() == "XAG-USDT-SWAP"
    with pytest.raises(ParkTelegramRuntimeError):
        runtime._default_market_reader()
    assert asked == ["silver"]


def test_a_gold_session_on_a_silver_contract_is_blocked(tmp_path: Path, fetches: list[str]) -> None:
    ParkStrategyIdentityJournal(tmp_path).start_clean_session(
        observed_at="2026-08-14T10:00:00+00:00", plan_digest="sha256:" + "4" * 64, reconciliation_healthy=True,
    )
    adapter = FakePaperAdapter()
    silver_config = prepare_park_paper_config(_config(), instrument="silver")
    result = _runtime(tmp_path, adapter, silver_config, market_reader=lambda: {"price": 4000.0}).run_once()
    assert result["code"] == "paper_instrument_mismatch" and adapter.submit_calls == []


def _reverse_setup(output: Path, *, instrument: str, old_session: str = "session-old") -> FakePaperAdapter:
    old_digest, new_digest = "sha256:" + "1" * 64, "sha256:" + "2" * 64
    ParkStrategyIdentityJournal(output).start_clean_session(
        observed_at="2026-08-14T10:00:00+00:00", plan_digest=old_digest, reconciliation_healthy=True,
        strategy_session_id="session-old", strategy_revision_id="revision-old", paper_instrument=instrument,
    )
    root = output / "park_strategy"
    # The numbers of the gold Reverse test; only the recorded instrument differs.
    plans = [
        {"event": "plan_proposed", "plan_digest": old_digest, "strategy_session_id": old_session, "strategy_revision_id": "revision-old",
         "normalized_input": {"strategy_type": "dca", "direction": "short", "upper_price_boundary": 4444.0, "lower_price_boundary": 4200.0,
                              "stop_price": 4450.0, "take_profit_price": 4100.0},
         "risk": {"effective_leverage": 5.0, "theoretical_max_loss": 100.0, "order_count": 1, "per_order_quantity": 1.0}},
        {"event": "plan_proposed", "plan_digest": new_digest, "strategy_session_id": "session-new", "strategy_revision_id": "revision-new",
         "normalized_input": {"strategy_type": "dca", "direction": "long", "upper_price_boundary": 4500.0, "lower_price_boundary": 4200.0,
                              "stop_price": 4100.0, "take_profit_price": 4600.0, "maximum_leverage": 5.0, "order_count": 1},
         "market": {"price": 4300.0, "trusted": True, "fresh": True, "source": "paper-feed", "observed_at": "2026-08-14T10:01:00+00:00"},
         "risk": {"maximum_notional": 50000.0, "effective_leverage": 5.0, "theoretical_max_loss": 2325.58139535, "order_count": 1,
                  "per_order_quantity": 11.627906976744}},
    ]
    (root / "plans.jsonl").write_text("\n".join(json.dumps(row) for row in plans) + "\n", encoding="utf-8")
    (root / "confirmations.jsonl").write_text("\n".join(json.dumps(
        {"event": "confirmed", "plan_digest": digest, "strategy_session_id": session, "strategy_revision_id": revision, "execution_authorized": True}
    ) for digest, session, revision in ((old_digest, old_session, "revision-old"), (new_digest, "session-new", "revision-new"))) + "\n",
        encoding="utf-8")
    (root / "reverse_requests.jsonl").write_text(json.dumps({
        "schema_version": "park-reverse-request-v1", "event": "reverse_request", "request_id": "reverse-test",
        "status": "confirmed_pending_transition",
        "old_strategy": {"strategy_session_id": old_session, "strategy_revision_id": "revision-old", "plan_digest": old_digest},
        "new_strategy_session_id": "session-new", "new_strategy_revision_id": "revision-new", "new_plan_digest": new_digest,
        "disposition": {"position_action": "flatten", "entry_action": "cancel", "exit_action": "keep"},
    }) + "\n", encoding="utf-8")
    return FakePaperAdapter()


MARKET = {"price": 4300.0, "trusted": True, "fresh": True, "source": "okx_swap_public", "provider": "okx",
          "observed_at": "2026-08-14T10:01:00+00:00", "instrument_id": "XAG-USDT-SWAP", "symbol": "XAG-USDT-SWAP"}


def test_a_reverse_keeps_the_old_sessions_instrument(tmp_path: Path, fetches: list[str]) -> None:
    adapter = _reverse_setup(tmp_path, instrument="silver")
    config = prepare_park_paper_config(_config(), instrument="silver")

    result = _runtime(tmp_path, adapter, config, market_reader=lambda: dict(MARKET)).run_once()

    assert result["status"] == "reverse_transitioned", result
    new = ParkStrategyIdentityJournal(tmp_path).active_session()
    assert new["strategy_session_id"] == "session-new" and new["paper_instrument"] == "silver"


# --- the Paper drafter -------------------------------------------------------


SILVER_MARKET = {"price": 52.2, "trusted": True, "fresh": True, "source": "okx_swap_public", "provider": "okx",
                 "observed_at": "2026-08-17T03:00:00+00:00", "instrument_id": "XAG-USDT-SWAP"}


def _drafter(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, provider=None, account=None) -> tuple[ParkAiChatService, list]:
    asked: list = []

    def reader(*, output_root=None, instrument=None):
        asked.append(instrument)
        return dict(SILVER_MARKET)

    monkeypatch.setattr("services.park_telegram_runtime.default_market_reader", reader)
    service = ParkAiChatService(
        tmp_path / "outputs", park_user_id="park-dashboard",
        provider=provider or FakeProvider(_candidate(upper_price_boundary=54, lower_price_boundary=50, stop_price=54.5,
                                                     take_profit_price=49)),
        account_reader=lambda _root, _cycle: dict(account or _account()),
        now=lambda: "2026-08-17T03:00:00+00:00",
    )
    return service, asked


def test_a_silver_draft_reads_silver_and_starts_a_silver_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    service, asked = _drafter(tmp_path, monkeypatch)

    draft = service.handle_message("做空白银 DCA，区间 54~50", paper_instrument="silver")
    assert draft["confirmable"] is True and set(asked) == {"silver"}
    confirmed = service.confirm(draft["draft"]["draft_id"], draft["draft"]["plan_digest"])

    assert confirmed["status"] == "confirmed"
    assert service.identity.active_session()["paper_instrument"] == "silver"
    plan = json.loads((tmp_path / "outputs" / "park_strategy" / "plans.jsonl").read_text().splitlines()[-1])
    assert plan["paper_instrument"] == "silver"


def test_a_gold_draft_keeps_its_plan_shape(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    service, asked = _drafter(tmp_path, monkeypatch)
    draft = service.handle_message("做空 DCA，区间 54~50")
    service.confirm(draft["draft"]["draft_id"], draft["draft"]["plan_digest"])
    plan = json.loads((tmp_path / "outputs" / "park_strategy" / "plans.jsonl").read_text().splitlines()[-1])
    assert "paper_instrument" not in plan and asked[0] == "gold"
    assert service.identity.active_session()["paper_instrument"] == "gold"


def test_a_draft_cannot_change_instrument_halfway(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    service, _ = _drafter(tmp_path, monkeypatch, provider=FakeProvider(_candidate(needs_clarification=True)))
    service.handle_message("白银网格", paper_instrument="silver")
    with pytest.raises(ParkAiChatError) as caught:
        service.handle_message("改成原油", paper_instrument="crude")
    assert caught.value.code == "paper_instrument_changed"
    with pytest.raises(ParkAiChatError) as unknown:
        ParkAiChatService(tmp_path / "other", park_user_id="p", provider=FakeProvider()).handle_message("铜", paper_instrument="copper")
    assert unknown.value.code == "paper_instrument_unknown"


def test_a_reverse_cannot_switch_instrument(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    service, _ = _drafter(
        tmp_path, monkeypatch,
        provider=FakeProvider(_candidate(direction="long", upper_price_boundary=54, lower_price_boundary=50, stop_price=50,
                                         take_profit_price=54)),
        account=_account(positions=[{"position_id": "p", "trade_id": "t", "status": "open", "side": "short",
                                     "remaining_units": 1.0, "entry_price": 4250.0, "notional": 4250.0}]),
    )
    service.identity.start_clean_session(observed_at="2026-08-17T03:00:00+00:00", plan_digest="sha256:" + "1" * 64,
                                         reconciliation_healthy=True, strategy_session_id="session-gold",
                                         strategy_revision_id="revision-gold")
    plans = tmp_path / "outputs" / "park_strategy" / "plans.jsonl"
    plans.write_text(json.dumps({"event": "plan_proposed", "plan_digest": "sha256:" + "1" * 64,
                                 "normalized_input": {"direction": "short", "strategy_type": "dca"}}) + "\n")
    service.handle_message("反转做多白银 DCA，区间 50~54，止损50，止盈54", paper_instrument="silver")
    ready = service.handle_message("旧仓全部平仓，撤掉未成交挂单，保留止盈止损")
    assert ready["confirmable"] is True

    blocked = service.confirm(ready["draft"]["draft_id"], ready["draft"]["plan_digest"])

    assert blocked["code"] == "paper_instrument_differs_from_active"
    assert not (tmp_path / "outputs" / "park_strategy" / "reverse_requests.jsonl").exists()


def test_the_dashboard_passes_an_instrument_only_when_the_request_names_one(tmp_path: Path) -> None:
    from pipelines.dashboard_server import build_park_ai_chat_response

    seen: list[dict] = []

    class Fake:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def handle_message(self, message, **kwargs):
            seen.append(kwargs)
            return {"status": "draft"}

    build_park_ai_chat_response({"message": "gold"}, output_root=tmp_path, service_factory=Fake)
    build_park_ai_chat_response({"message": "silver", "paper_instrument": "silver"}, output_root=tmp_path, service_factory=Fake)
    assert seen == [{"actor": None}, {"actor": None, "paper_instrument": "silver"}]
