from __future__ import annotations

import json
import sqlite3
import subprocess
from pathlib import Path

import pytest

from services.cloud_paper_preflight import CloudPaperPreflight
from services.config_loader import load_dualtrack_config, load_pipeline_config
from services.datafeed_market_client import DatafeedUnavailable


SHA = "a" * 40
TREE = "b" * 40


class FakeDatafeed:
    def __init__(self, *, fail_health: bool = False) -> None:
        self.fail_health = fail_health
        self.calls: list[dict] = []

    def health(self) -> dict:
        if self.fail_health:
            raise DatafeedUnavailable("connection refused")
        return {"status": "ok", "storage": {"status": "ok"}}

    def candles(self, **kwargs) -> dict:
        self.calls.append(kwargs)
        return {
            "selected_source": "binance_usdm_futures",
            "execution_venue": True,
            "is_synthetic": False,
            "fresh": True,
            "latest_timestamp": "2026-07-27T00:00:00+00:00",
            "candles": [{"timestamp": "2026-07-27T00:00:00+00:00"}],
        }


def configs() -> tuple[dict, dict]:
    pipeline = {
        "output_root": "outputs",
        "execution_mode": "paper",
        "live_trading_enabled": False,
        "datafeed": {
            "base_url": "http://127.0.0.1:8100",
            "timeout_seconds": 1,
        },
    }
    dualtrack = {
        "execution_engine": {
            "authoritative": "nautilus_paper",
            "real_money_eligible": False,
            "shadow_runtime_path": "/opt/gridmind/venvs/nautilus/bin/python",
        }
    }
    return pipeline, dualtrack


def passing_runner(*_args, **_kwargs) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=[],
        returncode=0,
        stdout=json.dumps({"version": [3, 13, 7]}),
        stderr="",
    )


def build(tmp_path: Path, **overrides) -> CloudPaperPreflight:
    pipeline, dualtrack = configs()
    paper_env = tmp_path / "paper.env"
    paper_env.write_text("# fixture has no secrets\n", encoding="utf-8")
    paper_env.chmod(0o600)
    environment = {
        "TRADING_ORCHESTRATOR_OUTPUT_ROOT": str(tmp_path / "outputs"),
        "TRADING_ORCHESTRATOR_DATAFEED_URL": "http://127.0.0.1:8100",
        "TRADING_ORCHESTRATOR_DASHBOARD_URL": "http://127.0.0.1:8765",
        "TRADING_ORCHESTRATOR_NAUTILUS_PYTHON": (
            "/opt/gridmind/venvs/nautilus/bin/python"
        ),
        "KLINE_DB_PATH": str(tmp_path / "datafeed" / "kline.db"),
        "TRADING_ORCHESTRATOR_LIVE_ENV": str(paper_env),
    }
    defaults = {
        "output_root": tmp_path / "outputs",
        "environment": environment,
        "pipeline_config": pipeline,
        "dualtrack_config": dualtrack,
        "datafeed_client": FakeDatafeed(),
        "platform_name": "Linux",
        "source_attestation": lambda: {
            "source_sha": SHA,
            "source_tree_sha": TREE,
            "tracked_tree_clean": True,
        },
        "command_runner": passing_runner,
    }
    defaults.update(overrides)
    return CloudPaperPreflight(**defaults)


def test_cloud_preflight_passes_without_control_actions(tmp_path: Path):
    datafeed = FakeDatafeed()
    result = build(tmp_path, datafeed_client=datafeed).run()

    assert result["status"] == "pass"
    assert result["control_actions_executed"] == 0
    assert result["failed_check_ids"] == []
    assert datafeed.calls[0]["cache_policy"] == "bypass"
    assert datafeed.calls[0]["quality"] == "strict"
    assert datafeed.calls[1]["cache_policy"] == "allow"
    receipt = json.loads(
        (tmp_path / "outputs" / "cloud" / "preflight" / "current.json").read_text()
    )[-1]
    assert receipt["status"] == "pass"


def test_cloud_preflight_rejects_live_capable_config(tmp_path: Path):
    pipeline, dualtrack = configs()
    pipeline["live_trading_enabled"] = True

    result = build(
        tmp_path,
        pipeline_config=pipeline,
        dualtrack_config=dualtrack,
    ).run()

    assert result["status"] == "blocked"
    assert "paper_only" in result["failed_check_ids"]
    assert result["control_actions_executed"] == 0


def test_cloud_preflight_preserves_datafeed_failure(tmp_path: Path):
    result = build(
        tmp_path,
        datafeed_client=FakeDatafeed(fail_health=True),
    ).run()

    assert result["status"] == "blocked"
    assert "datafeed_health" in result["failed_check_ids"]
    health = next(row for row in result["checks"] if row["id"] == "datafeed_health")
    assert "connection refused" in health["detail"]


def test_cloud_preflight_accepts_legacy_health_with_owner_sqlite_proof(
    tmp_path: Path,
):
    class LegacyDatafeed(FakeDatafeed):
        def health(self) -> dict:
            return {"status": "ok"}

        def candles(self, **kwargs) -> dict:
            payload = super().candles(**kwargs)
            payload["source_mode"] = payload.pop("selected_source")
            return payload

    db_path = tmp_path / "datafeed" / "kline.db"
    db_path.parent.mkdir(parents=True)
    with sqlite3.connect(db_path) as connection:
        connection.execute("CREATE TABLE receipt (value TEXT)")

    result = build(tmp_path, datafeed_client=LegacyDatafeed()).run()

    assert result["status"] == "pass"
    health = next(row for row in result["checks"] if row["id"] == "datafeed_health")
    assert health["storage_health_source"] == "owner_sqlite_quick_check"
    latest = next(
        row for row in result["checks"] if row["id"] == "latest_execution_venue_candle"
    )
    assert latest["selected_source"] == "binance_usdm_futures"


def test_cloud_preflight_rejects_legacy_health_without_owner_db(tmp_path: Path):
    class LegacyDatafeed(FakeDatafeed):
        def health(self) -> dict:
            return {"status": "ok"}

    result = build(tmp_path, datafeed_client=LegacyDatafeed()).run()

    assert result["status"] == "blocked"
    assert "datafeed_health" in result["failed_check_ids"]


def test_cloud_preflight_blocks_unwritable_persistence_shape(tmp_path: Path):
    file_parent = tmp_path / "not-a-directory"
    file_parent.write_text("occupied", encoding="utf-8")
    preflight = build(tmp_path)
    preflight.environment["KLINE_DB_PATH"] = str(file_parent / "kline.db")

    result = preflight.run()

    assert result["status"] == "blocked"
    assert "persistent_paths" in result["failed_check_ids"]


def test_cloud_preflight_blocks_nautilus_import_failure(tmp_path: Path):
    def failed_runner(*_args, **_kwargs):
        return subprocess.CompletedProcess(
            args=[],
            returncode=1,
            stdout="",
            stderr="ModuleNotFoundError: nautilus_trader",
        )

    result = build(tmp_path, command_runner=failed_runner).run()

    assert result["status"] == "blocked"
    assert "nautilus_runtime" in result["failed_check_ids"]


def test_cloud_config_environment_overrides_are_loopback_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    pipeline_path = tmp_path / "pipeline.json"
    dualtrack_path = tmp_path / "dualtrack.json"
    pipeline_path.write_text(
        json.dumps({"datafeed": {"base_url": "http://old.test:8100"}}),
        encoding="utf-8",
    )
    dualtrack_path.write_text(
        json.dumps(
            {
                "execution_engine": {"shadow_runtime_path": "/mac/python"},
                "execution_shadow": {
                    "nautilus": {"instrument_endpoint": "http://old.test/instrument"}
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("TRADING_ORCHESTRATOR_DATAFEED_URL", "http://127.0.0.1:8100")
    monkeypatch.setenv(
        "TRADING_ORCHESTRATOR_NAUTILUS_PYTHON",
        "/opt/gridmind/venvs/nautilus/bin/python",
    )

    pipeline = load_pipeline_config(pipeline_path)
    dualtrack = load_dualtrack_config(dualtrack_path)

    assert pipeline["datafeed"]["base_url"] == "http://127.0.0.1:8100"
    assert dualtrack["execution_engine"]["shadow_runtime_path"].startswith("/opt/gridmind")
    assert dualtrack["execution_shadow"]["nautilus"]["instrument_endpoint"].startswith(
        "http://127.0.0.1:8100/"
    )

    monkeypatch.setenv("TRADING_ORCHESTRATOR_DATAFEED_URL", "http://public.example")
    with pytest.raises(ValueError, match="loopback"):
        load_pipeline_config(pipeline_path)
