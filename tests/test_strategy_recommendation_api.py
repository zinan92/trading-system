from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import pipelines.dashboard_server as dashboard_server
import services.cycle_risk_envelope as risk_envelope_module
from pipelines.dashboard_server import build_strategy_console_control_response
from services.cycle_risk_envelope import CycleRiskEnvelopeStore
from services.cycle_decision import CycleDecisionCoordinator
from services.journal_store import load_json, write_json
from services.strategy_control_plane import StrategyControlPlane


def _bars(timeframe: str, count: int, close: float, span: float) -> list[dict]:
    rows = []
    step = {"1d": timedelta(days=1), "4h": timedelta(hours=4), "1h": timedelta(hours=1), "15m": timedelta(minutes=15)}[timeframe]
    started = datetime(2026, 6, 1, tzinfo=timezone.utc)
    for index in range(count):
        value = close - 3 + index * 0.15
        rows.append({
            "timestamp": (started + step * index).isoformat(),
            "open": value - 0.1,
            "high": value + span / 2,
            "low": value - span / 2,
            "close": value,
        })
    return rows


def _market() -> dict:
    execution = _bars("1h", 20, 4050, 2)
    return {
        "status": "ready",
        "fresh": True,
        "is_synthetic": False,
        "provider": "binance_usdm",
        "symbol": "GOLD",
        "timeframe": "1m",
        "latest_close": execution[-1]["close"],
        "latest_timestamp": execution[-1]["timestamp"],
        "bars": execution,
        "strategy_timeframes": {
            # Keep the longer D1 history aligned with the fixed 1m execution
            # price used by this API fixture.
            "1d": {"provider": "derived:binance_usdm", "is_synthetic": False, "bars": _bars("1d", 220, 4020, 20)},
            "4h": {"provider": "derived:binance_usdm", "is_synthetic": False, "bars": _bars("4h", 60, 4050, 8)},
            "1h": {"provider": "derived:binance_usdm", "is_synthetic": False, "bars": _bars("1h", 60, 4050, 3)},
            "15m": {"provider": "derived:binance_usdm", "is_synthetic": False, "bars": _bars("15m", 80, 4050, 2)},
        },
    }


def _bind_outer_policy(
    output: Path,
    monkeypatch,
    *,
    strategy_type: str = "grid",
    direction: str = "short",
) -> dict:
    monkeypatch.setenv("GOLDBOT_ACCESS_EMAIL", "park@example.com")
    monkeypatch.setattr(
        risk_envelope_module,
        "authenticated_access_identity",
        lambda _headers: {
            "email": "park@example.com",
            "subject": "park-subject",
            "issued_at": 1782871200,
            "expires_at": 1788141600,
            "issuer": "https://park.cloudflareaccess.com",
        },
    )
    limits = (
        {
            "max_actual_leverage": "20",
            "max_full_depth_loss": "100000",
            "max_notional_per_grid": "100000",
            "min_grid_count": "1",
            "max_grid_count": "200",
        }
        if strategy_type == "grid"
        else {
            "max_actual_leverage": "20",
            "max_full_depth_loss": "100000",
            "max_notional_per_addition": "2000",
            "max_total_possible_notional": "12000",
            "min_additions": "1",
            "max_additions": "6",
        }
    )
    store = CycleRiskEnvelopeStore(output)
    actor = {
        "email": "park@example.com",
        "transport": "public_gateway",
        "_access_assertion": "signed-park-assertion",
    }
    policy = store.authorize_outer_policy(
        payload={
            "policy_id": f"park-{strategy_type}-{direction}",
            "version": 1,
            "strategy_type": strategy_type,
            "direction": direction,
            "summary": f"Test Park {strategy_type} boundary",
            "expires_at": "2026-08-31T00:00:00+00:00",
            "limits": limits,
        },
        actor=actor,
        now="2026-07-01T00:00:00+00:00",
    )
    binding = store.bind_supervisor_outer_policy(
        payload={
            "binding_id": f"paper-supervisor-{strategy_type}",
            "binding_version": 1,
            "policy_id": policy["policy_id"],
            "policy_version": policy["version"],
            "policy_digest": policy["policy_digest"],
            "summary": "Test exact binding",
        },
        actor=actor,
        now="2026-07-01T00:01:00+00:00",
    )
    monkeypatch.setenv(
        "GRIDMIND_PAPER_SUPERVISOR_POLICY_BINDING_ID",
        binding["binding_id"],
    )
    monkeypatch.setenv(
        "GRIDMIND_PAPER_SUPERVISOR_POLICY_BINDING_VERSION",
        str(binding["binding_version"]),
    )
    monkeypatch.setenv(
        "GRIDMIND_PAPER_SUPERVISOR_POLICY_BINDING_DIGEST",
        binding["binding_digest"],
    )
    return binding


def test_refresh_recommendation_saves_ai_proposal_without_mutating_production(
    tmp_path: Path,
    monkeypatch,
) -> None:
    output = tmp_path / "outputs"
    monkeypatch.setenv("GOLDBOT_ACCESS_EMAIL", "park@example.com")
    monkeypatch.setattr(
        risk_envelope_module,
        "authenticated_access_identity",
        lambda _headers: {
            "email": "park@example.com",
            "subject": "park-subject",
            "issued_at": 1782871200,
            "expires_at": 1788141600,
            "issuer": "https://park.cloudflareaccess.com",
        },
    )
    policy_store = CycleRiskEnvelopeStore(output)
    actor = {
        "email": "park@example.com",
        "transport": "public_gateway",
        "_access_assertion": "signed-park-assertion",
    }
    policy = policy_store.authorize_outer_policy(
        payload={
            "policy_id": "park-grid-short",
            "version": 1,
            "strategy_type": "grid",
            "direction": "short",
            "summary": "Test Park Grid boundary",
            "expires_at": "2026-08-31T00:00:00+00:00",
            "limits": {
                "max_actual_leverage": "20",
                "max_full_depth_loss": "100000",
                "max_notional_per_grid": "100000",
                "min_grid_count": "1",
                "max_grid_count": "200",
            },
        },
        actor=actor,
        now="2026-07-01T00:00:00+00:00",
    )
    binding = policy_store.bind_supervisor_outer_policy(
        payload={
            "binding_id": "paper-supervisor-grid",
            "binding_version": 1,
            "policy_id": policy["policy_id"],
            "policy_version": policy["version"],
            "policy_digest": policy["policy_digest"],
            "summary": "Test binding",
        },
        actor=actor,
        now="2026-07-01T00:01:00+00:00",
    )
    monkeypatch.setenv(
        "GRIDMIND_PAPER_SUPERVISOR_POLICY_BINDING_ID",
        binding["binding_id"],
    )
    monkeypatch.setenv(
        "GRIDMIND_PAPER_SUPERVISOR_POLICY_BINDING_VERSION",
        str(binding["binding_version"]),
    )
    monkeypatch.setenv(
        "GRIDMIND_PAPER_SUPERVISOR_POLICY_BINDING_DIGEST",
        binding["binding_digest"],
    )
    plane = StrategyControlPlane(output)
    cycle_id = "2026-07-05_DAY"
    original = plane.upsert_proposal({
        "cycle_id": cycle_id,
        "source": "human",
        "direction": "neutral",
        "style": "steady",
        "range": {"low": 4000, "high": 4100},
        "grid": {"count": 30},
    })
    active = plane.lock_production_plan(cycle_id, selected_proposal_id=original["proposal_id"])

    result = build_strategy_console_control_response(
        {"cycle_id": cycle_id, "action": "refresh_recommendation", "as_of": "2026-07-05T02:00:00+00:00"},
        output_root=output,
        market=_market(),
        account={"equity": 100_000},
        recommendation_provider=lambda prompt: {
            "direction": "short",
            "style": "aggressive",
            "rationale": "D1 与 4H 走弱，1H 反弹不足。",
            "key_levels": [4040, 4080],
            "ai_self_assessment": 6,
            "evidence_used": ["D1", "4H", "1H"],
        },
    )

    assert result["production_plan_unchanged"] is True
    assert result["proposal"]["source"] == "ai"
    assert result["proposal"]["signal"]["calibration_status"] == "uncalibrated"
    assert result["proposal"]["analysis"]["framework"]["strategy"]["recommended_strategy_type"] == "grid"
    assert result["proposal"]["analysis"]["framework"]["position"]["lookback_bars"] == 200
    assert result["proposal"]["evaluation_receipt"]["status"] == "success"
    assert result["proposal"]["evaluation_receipt"]["input"]["contexts"]["15m"]["indicators"]["ema20"] is not None
    assert (output / result["proposal"]["evaluation_receipt"]["archive"]["relative_path"]).exists()
    assert result["preview"]["strategy_timeframes"] == {"range": "1d", "spacing": "4h", "execution": "1m"}
    assert plane.active_plan(cycle_id)["strategy_plan_id"] == active["strategy_plan_id"]
    assert not (output / "dualtrack" / "orders" / f"{cycle_id}_human.json").exists()


def test_refresh_provider_failure_never_promotes_legacy_ai_proposal(
    tmp_path: Path,
    monkeypatch,
) -> None:
    output = tmp_path / "outputs"
    _bind_outer_policy(output, monkeypatch)
    plane = StrategyControlPlane(output)
    cycle_id = "2026-07-05_DAY"
    legacy = plane.upsert_proposal(
        {
            "cycle_id": cycle_id,
            "source": "ai",
            "direction": "short",
            "style": "steady",
            "range": {"low": 4000, "high": 4100},
            "grid": {"count": 30},
        }
    )

    with pytest.raises(
        ValueError,
        match="AI recommendation unavailable",
    ):
        build_strategy_console_control_response(
            {
                "cycle_id": cycle_id,
                "action": "refresh_recommendation",
                "as_of": "2026-07-05T02:00:00+00:00",
            },
            output_root=output,
            market=_market(),
            account={"equity": 100_000},
            recommendation_provider=lambda _prompt: (
                (_ for _ in ()).throw(RuntimeError("provider down"))
            ),
        )

    assert plane.active_plan(cycle_id) is None
    assert plane.proposals(cycle_id) == [legacy]
    assert not plane._plans_path(cycle_id).exists()
    assert not list((output / "dualtrack" / "orders").glob("*"))


def test_dca_ai_refresh_reaches_candidate_envelope_then_human_confirmation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    output = tmp_path / "outputs"
    _bind_outer_policy(
        output,
        monkeypatch,
        strategy_type="dca",
        direction="long",
    )
    market = _market()
    long_term = _bars("1d", 220, 5000, 20)
    long_term[-1].update(
        {
            "open": 4049.9,
            "high": 4051.0,
            "low": 4049.0,
            "close": 4050.0,
        }
    )
    market["strategy_timeframes"]["1d"][
        "long_term_position_bars"
    ] = long_term
    cycle_id = "2026-07-30_DAY"
    now = "2026-07-30T03:00:00+00:00"
    refresh = build_strategy_console_control_response(
        {
            "cycle_id": cycle_id,
            "action": "refresh_recommendation",
            "as_of": now,
        },
        output_root=output,
        market=market,
        account={"equity": 100_000},
        recommendation_provider=lambda _prompt: {
            "direction": "long",
            "style": "steady",
            "rationale": "D1 与 4H 趋势向上且长期位置偏低。",
            "key_levels": [],
            "ai_self_assessment": 7,
            "evidence_used": ["D1", "4H"],
        },
    )
    assert refresh["recommendation"]["strategy_type"] == "dca"
    assert refresh["preview"]["manual_confirmation"]["required"] is True
    assert refresh["proposal"]["dca"]["max_additions"] == 6

    write_json(
        output / "dualtrack" / "runner" / f"{cycle_id}.json",
        [
            {
                "ts": now,
                "cycle_id": cycle_id,
                "event": "live_tick_heartbeat",
                "detail": {
                    "runner": "dualtrack-live-tick",
                    "ledger_refreshed": True,
                },
            }
        ],
    )
    plane = StrategyControlPlane(
        output,
        authorization_clock=lambda: now,
    )
    plane.config["execution_engine"] = {
        "authoritative": "legacy_paper",
        "shadow": "none",
        "real_money_eligible": False,
    }
    result = CycleDecisionCoordinator(output).ensure(
        cycle_id,
        now=now,
        plane=plane,
        execution_snapshot={"orders": [], "positions": []},
        refresh_recommendation=lambda: refresh,
        control=lambda action, payload: plane.control(
            cycle_id,
            action,
            payload,
            market=market,
            account={"equity": 100_000},
            now=now,
            actor={"type": "scheduler"},
        ),
    )

    assert result["decision"]["reason_code"] == (
        "risk_confirmation_required"
    )
    assert result["decision"]["orders_created"] == 0
    assert plane.active_plan(cycle_id) is None
    prepared_rows = load_json(plane._prepared_starts_path(cycle_id))
    prepared = prepared_rows[-1]
    assert prepared["cycle_risk_envelope_id"] == result["decision"][
        "cycle_risk_envelope_id"
    ]
    with pytest.raises(ValueError, match="prepared_start_changed"):
        plane.control(
            cycle_id,
            "start",
            {
                "direction": "long",
                "style": "steady",
                "strategy_type": "dca",
                "dca": dict(refresh["proposal"]["dca"]),
                "risk_budget": {
                    "leverage": refresh["preview"]["risk"][
                        "selected_leverage"
                    ],
                },
                "cycle_risk_envelope_id": "different-envelope",
                "prepared_start_id": prepared["prepared_start_id"],
                "expected_preview_id": prepared["preview"][
                    "preview_id"
                ],
            },
            market=market,
            account={"equity": 100_000},
            now=now,
        )
    envelopes = list(
        (
            output
            / "dualtrack"
            / "supervisor"
            / "risk_envelopes"
        ).glob(f"{cycle_id}.json")
    )
    assert len(envelopes) == 1
    assert not list((output / "dualtrack" / "orders").glob("*"))


def test_grid_preview_requires_only_the_d1_and_4h_planning_timeframes(tmp_path: Path, monkeypatch) -> None:
    market = _market()
    contexts = market.pop("strategy_timeframes")
    requested = []

    def partial_contexts(*, timeframes=None, **_kwargs):
        requested.append(tuple(timeframes or ()))
        return {timeframe: contexts[timeframe] for timeframe in timeframes}

    monkeypatch.setattr(dashboard_server, "build_strategy_timeframes_response", partial_contexts)
    result = build_strategy_console_control_response(
        {
            "cycle_id": "2026-07-05_DAY",
            "action": "preview",
            "direction": "neutral",
            "style": "steady",
        },
        output_root=tmp_path / "outputs",
        market=market,
        account={"equity": 100_000},
    )

    assert requested == [("1d", "4h")]
    assert result["preview"]["strategy_timeframes"] == {"range": "1d", "spacing": "4h", "execution": "1m"}


def test_strategy_timeframes_retry_one_transient_same_source_failure(monkeypatch) -> None:
    calls = []

    class FlakyFeed:
        def snapshot(self, *, timeframe, **_kwargs):
            calls.append(timeframe)
            if timeframe == "1d" and calls.count("1d") == 1:
                return {
                    "status": "blocked",
                    "is_synthetic": False,
                    "access_issues": ["datafeed unavailable: temporary connection reset"],
                    "bars": [],
                }
            return {
                "status": "ready",
                "fresh": True,
                "is_synthetic": False,
                "provider": "binance_usdm_futures",
                "bars": _bars(timeframe, 300 if timeframe == "1d" else 64, 4050, 20 if timeframe == "1d" else 8),
            }

    monkeypatch.setattr(dashboard_server, "DualTrackMarketFeed", lambda **_kwargs: FlakyFeed())
    result = dashboard_server.build_strategy_timeframes_response(
        as_of="2027-07-05T12:00:00+00:00",
        timeframes=("1d", "4h"),
    )

    assert calls == ["1d", "1d", "4h"]
    assert result["1d"]["provider"] == "binance_usdm_futures"
    assert result["4h"]["provider"] == "binance_usdm_futures"
    assert len(result["1d"]["long_term_position_bars"]) >= 200
    assert result["1d"]["weekends_excluded"] is True
    assert result["1d"]["long_term_position_weekends_excluded"] is False


def test_strategy_timeframes_never_retry_or_accept_synthetic_data(monkeypatch) -> None:
    calls = []

    class SyntheticFeed:
        def snapshot(self, *, timeframe, **_kwargs):
            calls.append(timeframe)
            return {
                "status": "ready",
                "fresh": True,
                "is_synthetic": True,
                "provider": "synthetic_fixture",
                "bars": _bars(timeframe, 32, 4050, 20),
            }

    monkeypatch.setattr(dashboard_server, "DualTrackMarketFeed", lambda **_kwargs: SyntheticFeed())
    with pytest.raises(ValueError, match="strategy timeframe 1d is unavailable or untrusted"):
        dashboard_server.build_strategy_timeframes_response(
            as_of="2026-07-05T12:00:00+00:00",
            timeframes=("1d",),
        )

    assert calls == ["1d"]
