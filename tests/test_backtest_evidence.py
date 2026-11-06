"""回测证据包 (#1369): the pack says what produced a number, and the future-data checks bite."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import subprocess

import pytest

from services import backtest_evidence as ev
from services import dca_backtest as dca
from services import grid_backtest as gb
from tests.test_grid_backtest import CONFIG, INSTRUMENT, SHAPE, SOURCE, START, ScriptedReplay, bars

WEEK = timedelta(days=7)


def test_the_bars_digest_follows_every_price_and_time() -> None:
    base = bars([100.0, 101.0, 99.0])
    digest = ev.bars_sha256(base)
    assert digest.startswith("sha256:") and digest == ev.bars_sha256(bars([100.0, 101.0, 99.0]))
    assert digest != ev.bars_sha256(bars([100.0, 101.0, 99.5]))
    assert digest != ev.bars_sha256(bars([100.0, 101.0, 99.0], start=START + timedelta(minutes=1)))


def test_the_bars_digest_can_be_reproduced_by_hand() -> None:
    import hashlib

    rows = bars([100.0, 101.0])
    text = "".join(f"{b.opened_at.isoformat()},{b.open!r},{b.close!r}\n" for b in rows)
    assert ev.bars_sha256(rows) == "sha256:" + hashlib.sha256(text.encode()).hexdigest()


def _git(folder: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=folder, check=True, capture_output=True)


def test_code_identity_names_the_commit_and_any_uncommitted_change(tmp_path: Path) -> None:
    _git(tmp_path, "init", "-q")
    (tmp_path / "a.py").write_text("x = 1\n")
    _git(tmp_path, "add", "a.py")
    _git(tmp_path, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "one")
    clean = ev.code_identity(tmp_path)
    assert len(clean["commit"]) == 40 and clean["dirty"] is False and clean["reason"] is None
    (tmp_path / "notes.txt").write_text("untracked does not count")
    assert ev.code_identity(tmp_path)["dirty"] is False
    (tmp_path / "a.py").write_text("x = 2\n")
    assert ev.code_identity(tmp_path)["dirty"] is True


def test_code_identity_says_unverified_when_git_cannot_answer(tmp_path: Path) -> None:
    outside = ev.code_identity(tmp_path)
    assert outside["commit"] is None and outside["reason"] == "读不到提交号"

    def missing(*_args, **_kwargs):
        raise FileNotFoundError("git")

    assert ev.code_identity(tmp_path, run=missing) == {"commit": None, "dirty": None, "reason": "git 不可用：FileNotFoundError"}


def test_history_must_have_closed_before_the_run() -> None:
    last = START
    assert ev.history_check(last_bar=last, run_started=last + timedelta(minutes=1))["status"] == ev.OK
    assert ev.history_check(last_bar=last, run_started=last + timedelta(seconds=30))["status"] == ev.FAILED
    assert ev.history_check(last_bar=None, run_started=last)["status"] == ev.UNVERIFIED


def row(**overrides) -> dict:
    base = {
        "started_at": START.isoformat(), "ended_at": (START + WEEK).isoformat(), "closed_at": (START + WEEK).isoformat(),
        "last_bar_at": (START + WEEK - timedelta(minutes=1)).isoformat(),
        "first_fill_at": (START + timedelta(seconds=1)).isoformat(), "last_fill_at": (START + WEEK + timedelta(seconds=1)).isoformat(), "fill_times_complete": True,
        "funding_first_at": (START + timedelta(hours=8)).isoformat(), "funding_last_at": (START + timedelta(days=6)).isoformat(),
        "first_funded_at": (START + timedelta(hours=8)).isoformat(),
    }
    return {**base, **overrides}


def statuses(rows: list[dict]) -> dict[str, str]:
    return {check["name"]: check["status"] for check in ev.window_checks(rows, length=WEEK)}


def test_clean_windows_pass_every_check() -> None:
    assert set(statuses([row(), row()]).values()) == {ev.OK}


@pytest.mark.parametrize(("change", "name"), [
    ({"last_bar_at": (START + WEEK).isoformat()}, "bars_inside_window"),
    ({"first_fill_at": (START - timedelta(minutes=1)).isoformat()}, "fills_inside_window"),
    ({"last_fill_at": (START + WEEK + timedelta(minutes=5)).isoformat()}, "fills_inside_window"),
    ({"fill_times_complete": False}, "fills_inside_window"),
    ({"funding_first_at": (START - timedelta(hours=8)).isoformat()}, "funding_inside_window"),
    ({"first_funded_at": START.isoformat(), "first_fill_at": (START + timedelta(minutes=1)).isoformat()}, "funding_after_position"),
])
def test_each_check_fails_on_its_own_breach(change: dict, name: str) -> None:
    found = statuses([row(), row(**change)])
    assert found[name] == ev.FAILED and all(status == ev.OK for key, status in found.items() if key != name)
    detail = next(c for c in ev.window_checks([row(), row(**change)], length=WEEK) if c["name"] == name)["detail"]
    assert detail.startswith("1 / 2 段不符合；第 2 段")


def test_a_window_without_the_times_is_unverified_not_passed() -> None:
    old = {key: value for key, value in row().items() if key not in {"first_fill_at", "last_fill_at"}}
    found = statuses([row(), old])
    assert found["fills_inside_window"] == ev.UNVERIFIED and found["funding_after_position"] == ev.UNVERIFIED
    assert statuses([]) == {name: ev.UNVERIFIED for name in found}


def test_no_fills_and_no_funding_pass() -> None:
    quiet = row(first_fill_at=None, last_fill_at=None, funding_first_at=None, funding_last_at=None, first_funded_at=None)
    assert set(statuses([quiet]).values()) == {ev.OK}


def test_the_verdict_is_ok_only_when_every_check_ran_and_passed() -> None:
    ok, bad, unknown = ({"status": s} for s in (ev.OK, ev.FAILED, ev.UNVERIFIED))
    assert ev.verdict([ok, ok]) == ev.OK
    assert ev.verdict([ok, unknown]) == ev.UNVERIFIED and ev.verdict([]) == ev.UNVERIFIED
    assert ev.verdict([unknown, bad]) == ev.FAILED


def test_the_pack_without_data_is_unverified_and_keeps_the_selection_note() -> None:
    pack = ev.pack(code={"commit": "a" * 40, "dirty": False, "reason": None}, data=None, params={"b": 1, "a": 2}, reason="读不到历史数据")
    assert pack["schema_version"] == "backtest-evidence-v1" and pack["verdict"] == ev.UNVERIFIED and pack["data"] is None
    assert pack["params"]["digest"] == ev.pack(code={}, data=None, params={"a": 2, "b": 1})["params"]["digest"]
    assert ev.SELECTION_NOTE in pack["notes"] and pack["tested"]


# ---- the window records carry what the checks need -------------------------------------------


def test_a_grid_window_records_its_own_times_and_passes() -> None:
    settled = [{"funding_time": (START + timedelta(minutes=5)).isoformat(), "funding_rate": 0.001, "realized_rate": None}]
    result = gb.run_window(SHAPE, bars([100.0] * 30), replay=ScriptedReplay(), config=CONFIG, instrument=INSTRUMENT, source=SOURCE,
                           price_step="0.1", quantity_step="0.001", funding=settled, funding_from=START.isoformat())
    assert result["last_bar_at"] == (START + timedelta(minutes=29)).isoformat()
    assert result["first_fill_at"] == (START + timedelta(seconds=1)).isoformat() and result["fill_times_complete"] is True
    assert result["funding_first_at"] == result["funding_last_at"] == result["first_funded_at"] == settled[0]["funding_time"]
    assert set(statuses([result]).values()) == {ev.OK}


def test_a_dca_window_records_its_own_times_and_passes() -> None:
    minutes = [gb.Bar(START + timedelta(minutes=i), 100.0, 100.0) for i in range(180)]
    settled = [{"funding_time": (START + timedelta(minutes=90)).isoformat(), "funding_rate": 0.001, "realized_rate": None}]
    result = dca.run_window({"amount_usdt": 100, "interval_hours": 1}, minutes, fee_rate=0.0, funding=settled, funding_from=START.isoformat())
    assert result["first_fill_at"] == (START + timedelta(minutes=1)).isoformat()
    assert result["last_fill_at"] == (START + timedelta(minutes=121)).isoformat()
    assert result["first_funded_at"] == settled[0]["funding_time"]
    assert set(statuses([result]).values()) == {ev.OK}


# ---- change the future, the past stays (the prefix test the pack lists as covered by tests) ------


class PriceReplay:
    """Stands in for Nautilus with prices: a buy fills at the first event at or below it, then its target at or above."""

    def __init__(self) -> None:
        self.outputs: list[dict] = []

    def __call__(self, bundle: dict) -> dict:
        events = sorted(bundle["market_events"], key=lambda e: e["ts_event"])
        fills, positions = [], []
        for item in bundle["commands"]:
            command = item["command"]
            if command["event"] != "entry":
                fills.append({"side": command["side"], "price": command["price"], "quantity": command["quantity"], "ts": command["ts"], "cost": 0.0})
                positions = [p for p in positions if p["position_id"] != command["position_id"]]
                continue
            later = [e for e in events if e["ts_event"] >= command["ts"]]
            entry = next((e for e in later if e["price"] <= command["price"]), None)
            if entry is None:
                continue
            fills.append({"side": "buy", "price": command["price"], "quantity": command["quantity"], "ts": entry["ts_event"], "cost": 0.0})
            target = next((e for e in later if e["ts_event"] > entry["ts_event"] and e["price"] >= command["tp"]), None)
            if target is None:
                positions.append({"trade_id": item["command_id"], "position_id": f"POS-{item['command_id']}", "status": "open", "side": "long",
                                  "remaining_units": command["quantity"]})
            else:
                fills.append({"side": "sell", "price": command["tp"], "quantity": command["quantity"], "ts": target["ts_event"], "cost": 0.0})
        output = {"fills": fills, "positions": positions}
        self.outputs.append(output)
        return output


def grid_fills_before(closes: list[float], cutoff: datetime) -> list[tuple]:
    replay = PriceReplay()
    gb.run_window(SHAPE, bars(closes), replay=replay, config=CONFIG, instrument=INSTRUMENT, source=SOURCE, price_step="0.1", quantity_step="0.001")
    return sorted((f["ts"], f["side"], f["price"], f["quantity"]) for f in replay.outputs[-1]["fills"] if datetime.fromisoformat(f["ts"]) <= cutoff)


@pytest.mark.parametrize("future", [[100.0] * 10, [108.0, 109.0] * 5, [92.0, 91.0] * 5, [80.0] * 10])
def test_a_grid_window_s_past_does_not_depend_on_its_future(future: list[float]) -> None:
    past = [100.0, 98.0, 96.0, 94.5, 97.0, 101.0, 103.0, 106.0, 104.0, 99.0]
    cutoff = START + timedelta(minutes=len(past))  # the close of the last shared bar
    reference = grid_fills_before(past + [100.0] * 10, cutoff)
    assert reference  # the past has fills, so the comparison means something
    assert grid_fills_before(past + future, cutoff) == reference


@pytest.mark.parametrize("future", [[100.0] * 120, [50.0] * 120, [400.0, 1.0] * 60])
def test_a_dca_window_s_past_buys_do_not_depend_on_its_future(future: list[float]) -> None:
    past = [100.0 + (i % 7) for i in range(130)]
    series = lambda tail: [gb.Bar(START + timedelta(minutes=i), p, p) for i, p in enumerate(past + tail)]
    cutoff = START + timedelta(minutes=len(past))
    before = lambda tail: [b for b in dca.buys({"amount_usdt": 100, "interval_hours": 1}, series(tail), fee_rate=0.001)
                           if datetime.fromisoformat(b["at"]) <= cutoff]
    assert len(before([100.0] * 120)) == 3
    assert before(future) == before([100.0] * 120)


# ---- the pipelines attach the pack -----------------------------------------------------------


def test_compare_attaches_evidence_with_data_params_and_checks() -> None:
    from pipelines.backtest_compare import compare
    from tests.test_dca_backtest import History

    client = History()
    code = {"commit": "c" * 40, "dirty": False, "reason": None}
    result = compare({"asset": "xau", "strategy": "dca", "variants": [{"amount_usdt": 50, "interval_hours": 24}]},
                     client=client, config={"paper_fee_model": {"taker_fee_rate": "0.0004"}}, now=client.end, code=code,
                     started=client.end + timedelta(minutes=1))
    pack = result["evidence"]
    assert pack["code"] == code and pack["data"]["bars"] == 8 * 1440 and pack["data"]["expected_bars"] == 8 * 1440
    assert pack["data"]["bars_sha256"].startswith("sha256:") and pack["data"]["source"] == "okx_swap_public"
    assert pack["params"]["values"]["fee_rate"] == 0.0004 and pack["params"]["values"]["request"]["variants"] == [{"amount_usdt": 50, "interval_hours": 24}]
    assert [c["name"] for c in pack["checks"]][0] == "history_before_run"
    assert pack["verdict"] == ev.OK, pack["checks"]


def test_a_comparison_that_could_not_read_data_still_says_which_code_ran(tmp_path: Path) -> None:
    from pipelines import backtest_compare

    request, output = tmp_path / "r.json", tmp_path / "o.json"
    request.write_text(json.dumps({"asset": "XAU", "strategy": "dca", "variants": [{"amount_usdt": 50, "interval_hours": 24}]}))
    assert backtest_compare.main(["--request", str(request), "--output", str(output), "--datafeed-url", "http://127.0.0.1:9"]) == 1
    result = json.loads(output.read_text())
    pack = result["evidence"]
    assert result["status"] == "unavailable" and pack["data"] is None and pack["verdict"] == ev.UNVERIFIED
    assert len(pack["code"]["commit"] or "") == 40 and pack["reason"] == result["reason"]
    assert pack["params"]["values"]["request"]["asset"] == "XAU"


def test_the_pre_start_backtest_attaches_evidence_even_when_unavailable(tmp_path: Path) -> None:
    from pipelines import grid_backtest

    plan, output, preflight = tmp_path / "p.json", tmp_path / "o.json", tmp_path / "pre.json"
    plan.write_text(json.dumps({"normalized_input": {"strategy_type": "dca"}}))
    preflight.write_text("[]")
    assert grid_backtest.main(["--plan-path", str(plan), "--output", str(output), "--preflight", str(preflight),
                               "--nautilus-python", "python", "--datafeed-url", "http://127.0.0.1:9"]) == 1
    pack = json.loads(output.read_text())["evidence"]
    assert pack["verdict"] == ev.UNVERIFIED and pack["inputs"]["preflight_sha256"] == ev.file_sha256(preflight)
    assert len(pack["code"]["commit"] or "") == 40


def test_a_dca_buy_on_the_last_bar_of_a_window_is_inside_it() -> None:
    minutes = [gb.Bar(START + timedelta(minutes=i), 100.0, 100.0) for i in range(61)]
    result = dca.run_window({"amount_usdt": 100, "interval_hours": 1}, minutes, fee_rate=0.0)
    assert result["last_fill_at"] == result["closed_at"] == (START + timedelta(minutes=61)).isoformat()
    assert set(statuses([result]).values()) == {ev.OK}
