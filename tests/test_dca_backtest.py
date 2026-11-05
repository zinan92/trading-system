"""定投回测 (#1335): every number can be checked by hand."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from services.dca_backtest import run_backtest, run_window, summarize, validate
from services.grid_backtest import BacktestUnavailable, Bar

T0 = datetime(2026, 9, 1, tzinfo=timezone.utc)


def minutes(prices: list[float], start: datetime = T0) -> list[Bar]:
    return [Bar(start + timedelta(minutes=i), price, price) for i, price in enumerate(prices)]


def test_buys_on_schedule_at_the_bar_close_and_values_at_the_last_close() -> None:
    # 3 hours of bars: 100 for the first hour, 50 for the second, 80 for the third.
    bars = minutes([100.0] * 60 + [50.0] * 60 + [80.0] * 60)
    row = run_window({"amount_usdt": 100, "interval_hours": 1}, bars, fee_rate=0.0)
    assert row["buys"] == 3 and row["invested"] == 300.0
    # 1 + 2 + 1.25 = 4.25 units, worth 4.25 * 80 = 340.
    assert row["value"] == 340.0 and row["net"] == 40.0 and row["return_pct"] == pytest.approx(13.3333, abs=1e-4)
    assert row["average_cost"] == pytest.approx(300 / 4.25, abs=1e-6)
    # Lowest point: 3 units at 50 against 200 invested, just after the second buy → −25%.
    assert row["max_drawdown_pct"] == -25.0
    assert row["funding"] is None  # no funding history covers this window


def test_fee_is_taken_from_each_buy() -> None:
    row = run_window({"amount_usdt": 100, "interval_hours": 1}, minutes([100.0] * 60), fee_rate=0.0004)
    assert row["fees"] == 0.04 and row["value"] == pytest.approx(99.96) and row["net"] == pytest.approx(-0.04)


def test_a_missing_minute_buys_at_the_next_bar_and_does_not_double_buy() -> None:
    bars = minutes([100.0] * 60) + minutes([200.0] * 59, start=T0 + timedelta(minutes=61))  # minute 60 missing, ends at 1:59
    row = run_window({"amount_usdt": 100, "interval_hours": 1}, bars, fee_rate=0.0)
    assert row["buys"] == 2 and row["value"] == pytest.approx((1 + 0.5) * 200)


def test_funding_counts_only_after_the_funding_history_starts() -> None:
    bars = minutes([100.0] * 120)
    funding = [{"funding_time": (T0 + timedelta(minutes=90)).isoformat(), "funding_rate": 0.001, "realized_rate": None}]
    counted = run_window({"amount_usdt": 100, "interval_hours": 1}, bars, fee_rate=0.0, funding=funding, funding_from=T0.isoformat())
    assert counted["funding"] == pytest.approx(2 * 100 * 0.001) and counted["net"] == pytest.approx(-0.2)
    early = run_window({"amount_usdt": 100, "interval_hours": 1}, bars, fee_rate=0.0, funding=funding, funding_from=(T0 + timedelta(days=1)).isoformat())
    assert early["funding"] is None and early["net"] == 0.0


def test_parameters_out_of_range_are_refused_with_the_field_named() -> None:
    assert validate({"amount_usdt": "50", "interval_hours": 24}) == {"amount_usdt": 50.0, "interval_hours": 24}
    for bad, word in (({"amount_usdt": 5, "interval_hours": 24}, "每次金额"), ({"amount_usdt": 50, "interval_hours": 200}, "间隔"),
                      ({"amount_usdt": 50, "interval_hours": 1.5}, "间隔"), ({"interval_hours": 4}, "amount_usdt")):
        with pytest.raises(ValueError, match=word):
            validate(bad)


def test_weekly_windows_and_summary() -> None:
    bars = minutes([100.0 + (i % 1440) / 100 for i in range(15 * 1440)])  # 15 days → 2 full weeks
    result = run_backtest({"amount_usdt": 20, "interval_hours": 24}, bars, fee_rate=0.0004)
    summary = result["summary"]
    assert summary["runs"] == 2 and summary["mean_buys"] == 7 and summary["invested_per_run"] == 140.0
    assert summary["funded_runs"] == 0 and sum(summary["histogram"]["counts"]) == 2
    with pytest.raises(BacktestUnavailable):
        run_backtest({"amount_usdt": 20, "interval_hours": 24}, minutes([100.0] * 60), fee_rate=0.0)
    with pytest.raises(BacktestUnavailable):
        summarize([], days=0)


class History:
    """Eight flat days of the asked series; records what was asked so a wrong source shows."""

    def __init__(self) -> None:
        self.end = datetime(2026, 11, 5, tzinfo=timezone.utc)
        self.asked: list[dict] = []

    def history_report(self, **kwargs) -> dict:
        self.asked.append(kwargs)
        start = self.end - timedelta(days=8)
        return {"requested_start": start.isoformat(), "requested_end": self.end.isoformat(), "expected_bars": 8 * 1440, "gaps": [], "fetch_failures": {}, "missing_minutes": 0}

    def history_candles(self, *, start: str, end: str, **kwargs) -> dict:
        self.asked.append(kwargs)
        first, last = datetime.fromisoformat(start), datetime.fromisoformat(end)
        rows, moment = [], first
        while moment <= last:
            rows.append({"timestamp": moment.isoformat(), "open": 100.0, "close": 100.0})
            moment += timedelta(minutes=1)
        return {"candles": rows}

    def history_funding(self, **kwargs) -> dict:
        return {"funding": []}


def test_compare_reads_the_asset_s_own_series_once_and_runs_each_variant() -> None:
    from pipelines.backtest_compare import compare

    client = History()
    result = compare({"asset": "btc", "strategy": "dca", "variants": [{"amount_usdt": 50, "interval_hours": 24}, {"amount_usdt": 50, "interval_hours": 6}]},
                     client=client, config={"paper_fee_model": {"taker_fee_rate": "0.0004"}}, now=client.end)
    assert result["status"] == "ready" and result["asset"] == "BTC" and result["instrument"] == "BTC-USDT-SWAP" and result["fee_rate"] == 0.0004
    assert {a.get("source") for a in client.asked} == {"okx_swap_crypto_public"} and {a.get("asset_class") for a in client.asked if "asset_class" in a} == {"crypto"}
    assert sum(1 for a in client.asked if "instrument" in a and "asset_class" not in a) == 1  # one report read
    assert [v["params"]["interval_hours"] for v in result["variants"]] == [24, 6]
    assert [v["summary"]["mean_buys"] for v in result["variants"]] == [7, 28] and "windows" not in result["variants"][0]


@pytest.mark.parametrize(("request_", "words"), [
    ({"asset": "ETH", "strategy": "dca", "variants": [{"amount_usdt": 50, "interval_hours": 24}]}, "品种"),
    ({"asset": "XAU", "strategy": "martingale", "variants": [{}]}, "策略只能"),
    ({"asset": "XAU", "strategy": "dca", "variants": []}, "1–3"),
    ({"asset": "XAU", "strategy": "dca", "variants": [{"amount_usdt": 50, "interval_hours": 24}] * 4}, "1–3"),
    ({"asset": "XAU", "strategy": "dca", "variants": [{"amount_usdt": 1, "interval_hours": 24}]}, "每次金额"),
])
def test_requests_outside_what_the_page_offers_are_refused(request_: dict, words: str) -> None:
    from pipelines.backtest_compare import check

    with pytest.raises(ValueError, match=words):
        check(request_)


# ---- 网格参数对比 (#1336) ------------------------------------------------------------------

GRID = {"direction": "long", "below_pct": 3, "above_pct": 3, "levels": 5, "notional_per_level": 200, "stop_pct": None}


@pytest.mark.parametrize("direction", ["long", "short", "neutral"])
def test_grid_shape_matches_the_planner_s_own_geometry(direction: str) -> None:
    from pipelines.backtest_compare import grid_shape
    from services.park_strategy_plan import _build_grid_risk_plan

    shape = grid_shape({**GRID, "direction": direction, "levels": 6}, equity=10000)
    plan = _build_grid_risk_plan({"direction": direction, "order_count": 6, "maximum_leverage": 1}, current_price=1.0, source="test",
                                 observed_at="2026-11-05T00:00:00+00:00", equity=10000, lower=shape["lower"], upper=shape["upper"])
    assert [(round(r["price"], 9), r["side"], round(r["take_profit"], 9)) for r in shape["rungs"]] == \
        [(round(r["price"], 9), r["side"], round(r["take_profit"], 9)) for r in plan["risk"]["grid_rungs"]]


def test_grid_shape_is_laid_at_price_one() -> None:
    from pipelines.backtest_compare import grid_shape

    shape = grid_shape(GRID, equity=10000)
    assert shape["lower"] == pytest.approx(0.97) and shape["upper"] == pytest.approx(1.03) and shape["stop"] is None
    assert [round(r["price"], 4) for r in shape["rungs"]] == [0.98, 0.99, 1.0, 1.01, 1.02]
    assert [round(r["take_profit"], 4) for r in shape["rungs"]] == [0.99, 1.0, 1.01, 1.02, 1.03]
    assert {r["side"] for r in shape["rungs"]} == {"buy"} and shape["quantity"] == 200 and shape["account_equity"] == 10000
    neutral = grid_shape({**GRID, "direction": "neutral", "levels": 4}, equity=10000)
    assert [r["side"] for r in neutral["rungs"]] == ["buy", "buy", "sell", "sell"]
    assert [round(r["take_profit"], 4) for r in neutral["rungs"]][2:] == [0.994, 1.006]  # a sell takes profit one rung down
    assert grid_shape({**GRID, "direction": "short", "stop_pct": 2}, equity=1)["stop"] == pytest.approx(1.02)


@pytest.mark.parametrize(("params", "words"), [
    ({**GRID, "direction": "up"}, "方向"),
    ({**GRID, "below_pct": 0.1}, "上下沿"),
    ({**GRID, "levels": 31}, "格数"),
    ({**GRID, "levels": 2.5}, "格数"),
    ({**GRID, "notional_per_level": 5}, "每格金额"),
    ({**GRID, "stop_pct": 3}, "止损"),
    ({**GRID, "direction": "neutral", "stop_pct": 1}, "双向"),
])
def test_grid_parameters_outside_the_form_are_refused(params: dict, words: str) -> None:
    from pipelines.backtest_compare import validate_grid

    with pytest.raises(ValueError, match=words):
        validate_grid(params)


def test_btc_grid_waits_for_its_own_contract() -> None:
    from pipelines.backtest_compare import check

    with pytest.raises(ValueError, match="#1337"):
        check({"asset": "BTC", "strategy": "grid", "variants": [GRID]})
    assert check({"asset": "XAU", "strategy": "grid", "variants": [GRID]})["variants"][0]["levels"] == 5


def test_grid_compare_replays_each_variant_through_the_grid_engine() -> None:
    from pipelines.backtest_compare import compare
    from services.dualtrack_config import dualtrack_config

    calls: list[dict] = []

    def replay(bundle: dict) -> dict:  # nothing fills: every window ends flat at zero
        calls.append(bundle)
        return {"fills": [], "positions": []}

    client = History()
    result = compare({"asset": "XAU", "strategy": "grid", "variants": [GRID, {**GRID, "below_pct": 5, "above_pct": 5}]},
                     client=client, config=dualtrack_config(paper_gold_venue="okx"), replay=replay, now=client.end)
    assert result["status"] == "ready" and result["strategy"] == "grid" and len(result["variants"]) == 2
    assert result["variants"][0]["summary"]["runs"] == 1 and result["variants"][0]["summary"]["median_return_pct"] == 0.0
    assert result["variants"][1]["params"]["below_pct"] == 5 and result["variants"][0]["account_equity"] == 10000
    assert len(calls) == 2 and {a.get("source") for a in client.asked} == {"okx_swap_public"}
    with pytest.raises(BacktestUnavailable, match="回放"):
        compare({"asset": "XAU", "strategy": "grid", "variants": [GRID]}, client=History(), config=dualtrack_config(paper_gold_venue="okx"), now=client.end)
