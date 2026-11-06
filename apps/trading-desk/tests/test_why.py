"""这一笔为什么 (#1359): the chain is read from records that already exist; a gap is named, never filled."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from test_desk import client_for
from trading_desk import why

DIGEST = "sha256:abc"
SESSION = "session-1"
PLAN_ID = "dashboard-plan:abc"
SECRETS = ("acct-fingerprint-1", "runtime-secret-id", "park-user-42")


def _lines(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def _write(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def ai_call(**extra) -> dict:
    return {"id": 7, "author": "ai", "asset": "XAU", "direction": "long", "confidence": 3, "reason": "金价站上 4400", "created_at": "2026-09-08T00:40:00+00:00",
            "evidence": {"model": {"channel": "codex", "asked_at": "2026-09-08T00:40:00+00:00"}, "unverified": [], "news_stale": False, "kline": {"id": "kline"}, "price": {"source": "venue"}}, **extra}


@pytest.fixture
def paper(tmp_path: Path) -> Path:
    strategy = tmp_path / "park_strategy"
    _lines(strategy / "plans.jsonl", [{"event": "plan_proposed", "plan_digest": DIGEST, "strategy_session_id": SESSION, "created_at": "2026-09-08T01:51:24+00:00",
                                       "source": "dashboard_ai", "market": {"source": "binance_usdm_futures", "price": 4439.97, "observed_at": "2026-09-08T01:51:00+00:00"}}])
    _lines(strategy / "confirmations.jsonl", [
        {"event": "proposal", "plan_digest": DIGEST, "strategy_session_id": SESSION, "park_user_id": "park-user-42"},
        {"event": "confirmed", "plan_digest": DIGEST, "strategy_session_id": SESSION, "confirmed_at": 1788832284.36, "execution_environment": "paper", "park_user_id": "park-user-42"}])
    _lines(strategy / "identity.jsonl", [{"event": "session_started", "strategy_session_id": SESSION, "observed_at": "2026-09-08T01:51:24+00:00"},
                                         {"event": "session_closed", "strategy_session_id": SESSION, "observed_at": "2026-09-14T09:19:35+00:00", "reason": "lower_boundary_invalidated"}])
    _lines(strategy / "lifecycle.jsonl", [{"event": "plan_activated", "plan_digest": DIGEST, "strategy_session_id": SESSION, "state": "ACTIVE_LOCKED", "strategy_type": "grid", "direction": "long"}])
    _lines(strategy / "executions.jsonl", [
        {"event": "entry_submitted", "plan_digest": DIGEST, "strategy_session_id": SESSION, "recorded_at": "2026-09-08T02:00:00+00:00"},
        {"event": "entry_submitted", "plan_digest": DIGEST, "strategy_session_id": SESSION, "recorded_at": "2026-09-08T03:00:00+00:00"},
        {"event": "terminal_paused", "plan_digest": DIGEST, "strategy_session_id": SESSION, "recorded_at": "2026-09-14T09:19:35+00:00", "terminal_key": f"{SESSION}:lower_boundary_invalidated",
         "result": {"reconciliation": {"status": "ok", "counts": {"open_positions": 0}}}}])
    _write(tmp_path / "dualtrack" / "nautilus_authoritative" / "fills" / f"park-{SESSION}.json", [
        {"ts": "2026-09-08T02:00:01+00:00", "side": "BUY", "event": "entry", "price": 4400.0, "quantity": 1.0, "cost": 0.5, "order_id": "1"},
        {"ts": "2026-09-14T09:19:36+00:00", "side": "SELL", "event": "stop", "price": 4380.0, "quantity": 1.0, "cost": 0.5, "order_id": "2"}])
    return tmp_path


@pytest.fixture
def testnet(tmp_path: Path) -> Path:
    base = tmp_path / "dashboard_control_plane"
    _write(base / "previews.json", [{"preview_digest": "sha256:other", "execution_ready": False, "blockers": ["account_not_clean"]},
                                    {"preview_digest": DIGEST, "execution_ready": True, "blockers": [], "release_sha": "f80fa54b1e03", "venue_profile_id": "hyperliquid.testnet", "instrument_id": "BTC-USD-PERP",
                                     "strategy_family": "grid", "account_fingerprint": "acct-fingerprint-1", "runtime_id": "runtime-secret-id"}])
    _write(base / "confirmations.json", [{"event": "operator_confirmed", "status": "confirmed", "preview_digest": DIGEST, "plan_digest": DIGEST, "activation_id": "sha256:act", "operator_id": "park",
                                          "confirmed_at": "2026-09-14T01:13:46+00:00", "account_fingerprint": "acct-fingerprint-1", "runtime_id": "runtime-secret-id"},
                                         {"event": "plan_closed", "status": "plan_closed", "plan_digest": DIGEST, "activation_id": "sha256:act"}])
    _write(tmp_path / "testnet_automation" / "events.json", [{"event": "activated", "status": "activated", "activation_id": "sha256:act", "plan_digest": DIGEST, "occurred_at": "2026-09-14T01:14:00+00:00", "runtime_id": "runtime-secret-id"}])
    _write(tmp_path / "dualtrack" / "grid_testnet_lifecycle" / f"{PLAN_ID}.json", [_state()])
    return tmp_path


def _state(**extra) -> dict:
    return {"strategy_plan_id": PLAN_ID, "plan_digest": DIGEST, "instrument_id": "BTC-USD-PERP", "direction": "long", "status": "terminal", "terminal_reason": "hard_stop", "created_at": "2026-09-14T01:14:10+00:00",
            "orders": [{"state": "accepted"}, {"state": "accepted"}],
            "fills": [{"timestamp": "2026-09-14T02:00:00+00:00", "event": "entry", "price": 100.0, "quantity": 1.0, "fee": 0.1, "order_id": "1"}],
            "events": [{"event": "lifecycle_started", "timestamp": "2026-09-14T01:14:10+00:00"}, {"event": "ladder_activated", "timestamp": "2026-09-14T01:14:20+00:00"},
                       {"event": "revision_sealed", "timestamp": "2026-09-14T05:00:00+00:00"}], **extra}


def _step(result: dict, key: str) -> dict:
    return next(step for step in result["steps"] if step["key"] == key)


def test_a_clean_paper_run_reads_as_one_chain(paper: Path) -> None:
    result = why.explain_run(paper, f"paper:{SESSION}", [ai_call()])
    assert result["schema_version"] == "why-this-trade-v1"
    assert [step["key"] for step in result["steps"]] == ["ai", "proposal", "confirm", "activate", "orders", "fills", "end"]
    assert result["status"] == "clean" and result["first_problem"] is None
    assert result["identity"]["plan_digest"] == DIGEST and result["identity"]["market_source"] == "binance_usdm_futures"
    assert result["identity"]["release_sha"] is None  # Paper never recorded it: named, not looked up from today's checkout
    assert _step(result, "orders")["summary"] == "提交了 2 笔开仓单"
    assert "价格跌破区间下沿" in _step(result, "end")["summary"]
    assert "记录" in result["note"]


def test_the_first_gap_is_the_break_and_later_steps_still_show(paper: Path) -> None:
    (paper / "park_strategy" / "confirmations.jsonl").write_text("", encoding="utf-8")
    result = why.explain_run(paper, f"paper:{SESSION}", [])
    assert result["status"] == "broken"
    assert result["first_problem"]["key"] == "confirm"
    assert _step(result, "confirm")["status"] == "unknown"
    assert _step(result, "fills")["status"] == "ok"


def test_a_missing_file_is_unknown_not_empty(paper: Path) -> None:
    (paper / "park_strategy" / "plans.jsonl").unlink()
    result = why.explain_run(paper, f"paper:{SESSION}", [])
    assert _step(result, "proposal")["status"] == "unknown"
    assert "park_strategy/plans.jsonl" in _step(result, "proposal")["summary"]


def test_a_run_that_is_still_open_has_not_ended_and_is_not_broken(paper: Path) -> None:
    strategy = paper / "park_strategy"
    _lines(strategy / "identity.jsonl", [{"event": "session_started", "strategy_session_id": SESSION}])
    rows = [json.loads(line) for line in (strategy / "executions.jsonl").read_text().splitlines()]
    _lines(strategy / "executions.jsonl", [row for row in rows if row["event"] != "terminal_paused"])
    result = why.explain_run(paper, f"paper:{SESSION}", [])
    assert _step(result, "end")["status"] == "not_reached"
    assert result["first_problem"] is None


def test_a_closed_run_without_a_seal_is_a_gap(paper: Path) -> None:
    strategy = paper / "park_strategy"
    rows = [json.loads(line) for line in (strategy / "executions.jsonl").read_text().splitlines()]
    _lines(strategy / "executions.jsonl", [row for row in rows if row["event"] != "terminal_paused"])
    result = why.explain_run(paper, f"paper:{SESSION}", [])
    assert result["first_problem"]["key"] == "end"


def test_a_clean_testnet_run(testnet: Path) -> None:
    result = why.explain_run(testnet, f"testnet:{PLAN_ID}", [])
    assert [step["key"] for step in result["steps"]] == ["ai", "preview", "confirm", "activate", "orders", "fills", "end"]
    assert result["status"] == "clean"
    assert result["identity"]["release_sha"] == "f80fa54b1e03"
    assert result["identity"]["activation_id"] == "sha256:act"
    assert "触发硬止损" in _step(result, "end")["summary"]


def test_a_blocked_lifecycle_names_the_first_reason(testnet: Path) -> None:
    state = _state(status="blocked_reconciliation", terminal_reason=None, blocker="position_open_unprotected")
    state["events"].insert(2, {"event": "lifecycle_blocked", "timestamp": "2026-09-14T03:00:00+00:00", "reason": "hard_stop_submit_failed:submit_unknown"})
    _write(testnet / "dualtrack" / "grid_testnet_lifecycle" / f"{PLAN_ID}.json", [state])
    result = why.explain_run(testnet, f"testnet:{PLAN_ID}", [])
    assert result["first_problem"]["key"] == "end"
    assert result["first_problem"]["summary"] == "对账拦下：position_open_unprotected"
    assert "hard_stop_submit_failed:submit_unknown" in _step(result, "orders")["summary"]
    assert _step(result, "orders")["status"] == "attention"


def test_a_ladder_that_never_finished_counts_as_blocked(testnet: Path) -> None:
    state = _state(status="blocked_local_validation", terminal_reason=None, blocker="initial_ladder_incomplete:ValueError", orders=[])
    state["events"].insert(1, {"event": "ladder_incomplete", "timestamp": "2026-09-14T01:14:15+00:00", "reason": "blocked_local_validation:ValueError"})
    _write(testnet / "dualtrack" / "grid_testnet_lifecycle" / f"{PLAN_ID}.json", [state])
    result = why.explain_run(testnet, f"testnet:{PLAN_ID}", [])
    assert _step(result, "orders")["status"] == "attention" and "blocked_local_validation:ValueError" in _step(result, "orders")["summary"]
    assert result["first_problem"]["summary"] == "本地校验拦下：initial_ladder_incomplete:ValueError"


def test_a_preview_that_was_not_ready_is_failed_even_if_the_run_started(testnet: Path) -> None:
    rows = json.loads((testnet / "dashboard_control_plane" / "previews.json").read_text())
    rows[1].update(execution_ready=False, blockers=["risk_gate_exceeded"])
    _write(testnet / "dashboard_control_plane" / "previews.json", rows)
    result = why.explain_run(testnet, f"testnet:{PLAN_ID}", [])
    assert result["first_problem"]["key"] == "preview"
    assert "risk_gate_exceeded" in result["first_problem"]["summary"]


def test_account_and_runtime_ids_never_appear(testnet: Path, paper: Path) -> None:
    text = json.dumps([why.explain_run(testnet, f"testnet:{PLAN_ID}", []), why.explain_run(paper, f"paper:{SESSION}", [])], ensure_ascii=False)
    assert not any(secret in text for secret in SECRETS)


def test_the_nearest_earlier_ai_call_is_shown_as_time_matched_only(paper: Path) -> None:
    later = ai_call(id=8, created_at="2026-09-09T00:40:00+00:00")
    other = ai_call(id=9, asset="BTC", created_at="2026-09-08T01:00:00+00:00")
    park = ai_call(id=10, author="park", created_at="2026-09-08T01:10:00+00:00")
    step = _step(why.explain_run(paper, f"paper:{SESSION}", [later, other, park, ai_call()]), "ai")
    assert step["status"] == "ok" and step["at"] == "2026-09-08T00:40:00+00:00"
    assert "按时间" in step["summary"]


def test_an_ai_call_with_unbacked_numbers_needs_attention(paper: Path) -> None:
    call = ai_call()
    call["evidence"].update(unverified=["4400"], news_stale=True)
    result = why.explain_run(paper, f"paper:{SESSION}", [call])
    assert _step(result, "ai")["status"] == "attention" and result["status"] == "attention"
    assert result["first_problem"] is None


def test_no_ai_call_is_not_a_problem(paper: Path) -> None:
    result = why.explain_run(paper, f"paper:{SESSION}", [])
    assert _step(result, "ai")["status"] == "not_reached" and result["status"] == "clean"


@pytest.mark.parametrize("run_id", ["", "paper", "paper:", "paper:../x", "testnet:a/b", "live:x", "paper:nope", "testnet:nope"])
def test_an_unknown_run_is_none(paper: Path, run_id: str) -> None:
    assert why.explain_run(paper, run_id, []) is None


def judgment(**extra) -> dict:
    return {"id": 3, "author": "park", "asset": "BTC", "direction": "long", "confidence": 3, "reason": "看多", "price_at": 100.0, "action": "approved", "created_at": "2026-09-14T01:00:00+00:00",
            "plan": {"kind": "new", "executable": True, "title": "做多网格"}, "evidence": {}, **extra}


def execution(stage: str, ident: int = 1, digest: str | None = None, **detail) -> dict:
    return {"id": ident, "created_at": f"2026-09-14T01:0{ident}:00+00:00", "judgment_id": 3, "asset": "BTC", "stage": stage, "preview_digest": digest, "detail": detail}


def test_a_recorded_judgment_stops_at_the_judgment(tmp_path: Path) -> None:
    result = why.explain_judgment(tmp_path, judgment(action="recorded", plan=None), [], [])
    assert result["status"] == "clean"
    assert _step(result, "preview")["status"] == "not_reached"
    assert "只记录" in _step(result, "plan")["summary"]


def test_a_plan_with_no_price_is_the_break(tmp_path: Path) -> None:
    result = why.explain_judgment(tmp_path, judgment(plan={"kind": "unavailable", "title": "现价读不到，暂时生成不了计划"}), [], [])
    assert result["first_problem"]["key"] == "plan"


def test_an_approved_plan_with_no_preview_record_is_a_gap(tmp_path: Path) -> None:
    assert why.explain_judgment(tmp_path, judgment(), [], [])["first_problem"]["key"] == "preview"


def test_a_blocked_preview_is_the_break(tmp_path: Path) -> None:
    rows = [execution("preview", execution_ready=False, blockers=["超过风险上限"])]
    result = why.explain_judgment(tmp_path, judgment(), rows, [])
    assert result["first_problem"]["key"] == "preview" and "超过风险上限" in result["first_problem"]["summary"]


def test_a_ready_preview_waits_for_the_button(tmp_path: Path) -> None:
    rows = [execution("preview", digest=DIGEST, execution_ready=True, max_loss=12.5)]
    result = why.explain_judgment(tmp_path, judgment(), rows, [])
    assert result["status"] == "clean"
    assert _step(result, "press")["status"] == "not_reached"


def test_a_refused_execute_is_the_break(tmp_path: Path) -> None:
    rows = [execution("preview", 1, DIGEST, execution_ready=True), execution("execute_started", 2, pressed_at="x"), execution("refused", 3, reason="预览没有通过风险检查，没有下单。")]
    result = why.explain_judgment(tmp_path, judgment(), rows, [])
    assert result["first_problem"]["key"] == "result" and "没有下单" in result["first_problem"]["summary"]


def test_a_failed_driver_names_the_last_reason_code(tmp_path: Path) -> None:
    rows = [execution("preview", 1, DIGEST, execution_ready=True), execution("execute_started", 2), execution("execute_failed", 3, DIGEST, status="blocked", started=False,
            attempts=[{"attempt": 1, "status": "no_receipt", "reason_code": None}, {"attempt": 2, "status": "blocked", "reason_code": "market_price_mismatch"}])]
    result = why.explain_judgment(tmp_path, judgment(), rows, [])
    assert "market_price_mismatch" in result["first_problem"]["summary"]


def test_a_press_with_no_result_is_a_gap(tmp_path: Path) -> None:
    rows = [execution("preview", 1, DIGEST, execution_ready=True), execution("execute_started", 2)]
    result = why.explain_judgment(tmp_path, judgment(), rows, [])
    assert result["first_problem"]["key"] == "result" and _step(result, "result")["status"] == "unknown"


def test_a_started_plan_links_to_its_testnet_run(testnet: Path) -> None:
    rows = [execution("preview", 1, DIGEST, execution_ready=True), execution("execute_started", 2), execution("executed", 3, DIGEST, started=True, status="grid_running", activation_id="sha256:act", release="deadbeef")]
    result = why.explain_judgment(testnet, judgment(), rows, [])
    assert result["status"] == "clean"
    assert result["identity"] | {"run_id": f"testnet:{PLAN_ID}", "release_sha": "deadbeef", "activation_id": "sha256:act"} == result["identity"]


def test_a_started_gold_plan_links_to_its_paper_session(paper: Path) -> None:
    rows = [execution("preview", 1, DIGEST, execution_ready=True), execution("execute_started", 2), execution("executed", 3, DIGEST, started=True, status="confirmed")]
    assert why.explain_judgment(paper, judgment(asset="XAU"), rows, [])["identity"]["run_id"] == f"paper:{SESSION}"


def test_a_started_plan_that_no_run_recorded_is_a_gap(tmp_path: Path) -> None:
    rows = [execution("preview", 1, DIGEST, execution_ready=True), execution("execute_started", 2), execution("executed", 3, DIGEST, started=True, status="grid_running")]
    result = why.explain_judgment(tmp_path, judgment(), rows, [])
    assert result["first_problem"]["key"] == "run"


def test_the_route_needs_exactly_one_subject(config, sources, store, fetch) -> None:
    client, _ = client_for(config, sources, store, fetch)
    assert client.get("/api/why").status_code == 422
    assert client.get("/api/why?run=paper:x&judgment=1").status_code == 422


def test_the_route_404s_what_it_does_not_know(config, sources, store, fetch) -> None:
    client, _ = client_for(config, sources, store, fetch)
    assert client.get("/api/why?run=paper:nope").status_code == 404
    assert client.get("/api/why?judgment=99999").status_code == 404


def test_the_route_reads_a_testnet_run_and_a_judgment(config, sources, store, fetch) -> None:
    client, _ = client_for(config, sources, store, fetch)
    run = client.get(f"/api/why?run=testnet:{PLAN_ID}").json()
    assert run["subject"] == {"kind": "run", "id": f"testnet:{PLAN_ID}"} and run["first_problem"]["key"] == "preview"  # the fixture has no previews.json
    saved = store.add_judgment(asset="BTC", direction="long", confidence=3, reason="看多", cited=[], price_at=100.0, plan=None, action="recorded")
    assert client.get(f"/api/why?judgment={saved['id']}").json()["steps"][1]["key"] == "judgment"


def test_execution_records_carry_the_serving_commit(tmp_path: Path) -> None:
    from trading_desk.store import Store
    store = Store(tmp_path / "d.db", release="deadbeef")
    store.log_execution(asset="BTC", stage="preview", judgment_id=1, preview_digest=None, detail={"a": 1})
    assert store.executions_of(1)[0]["detail"] == {"a": 1, "release": "deadbeef"}
    assert Store(tmp_path / "e.db").log_execution(asset="BTC", stage="preview", judgment_id=1, preview_digest=None, detail={})["detail"] == {}


def test_the_review_chart_box_has_a_fixed_height() -> None:
    """The chart sizes itself to its box: without a fixed height it grows with itself, to 80,000 px."""
    css = (Path(why.__file__).parent / "static" / "gm-desk.css").read_text(encoding="utf-8")
    assert "#deskRunChart{height:420px;overflow:hidden}" in css
