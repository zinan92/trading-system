from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from services.park_cutover_guard import REQUIRED_SAFETY_GATES, evaluate_park_cutover, load_default_config


ROOT = Path(__file__).resolve().parents[1]


def _evidence() -> dict[str, object]:
    return {"release_sha": "a" * 40, "boot_verified": True, **{gate: True for gate in REQUIRED_SAFETY_GATES}}


def test_default_config_is_disabled_and_fail_closed() -> None:
    config = load_default_config()
    assert config["feature_enabled"] is False
    result = evaluate_park_cutover(config, safety_evidence=_evidence())
    assert result["status"] == "blocked"
    assert "park_track_disabled" in result["blockers"]
    assert result["mutations"] == []
    assert result["runtime_ready_claim"] is False


def test_explicit_paper_telegram_single_track_with_all_gates_passes_read_only() -> None:
    config = {**load_default_config(), "feature_enabled": True}
    result = evaluate_park_cutover(config, safety_evidence=_evidence())
    assert result["status"] == "pass"
    assert result["blockers"] == []
    assert result["execution_track_count"] == 1
    assert result["mutations"] == []
    assert result["runtime_ready_claim"] is False


@pytest.mark.parametrize("field, value, blocker", [
    ("runtime_mode", "live", "paper_only_required"),
    ("execution_track_count", 2, "execution_track_count_not_one"),
    ("control_plane", "feishu", "telegram_only_required"),
    ("autonomous", True, "autonomous_forbidden"),
    ("shadow_mutation", True, "shadow_mutation_forbidden"),
    ("feishu_control", True, "feishu_control_forbidden"),
])
def test_forbidden_paths_are_blocked(field: str, value: object, blocker: str) -> None:
    config = {**load_default_config(), "feature_enabled": True, field: value}
    result = evaluate_park_cutover(config, safety_evidence=_evidence())
    assert result["status"] == "blocked"
    assert blocker in result["blockers"]


def test_missing_release_boot_or_safety_evidence_blocks() -> None:
    config = {**load_default_config(), "feature_enabled": True}
    result = evaluate_park_cutover(config, safety_evidence={})
    assert result["status"] == "blocked"
    assert "release_sha_missing" in result["blockers"]
    assert "boot_unverified" in result["blockers"]
    assert any(code.startswith("safety_gate_failed:") for code in result["blockers"])


def test_config_is_versioned_json_and_park_modules_have_no_forbidden_imports() -> None:
    config = json.loads((ROOT / "configs" / "park_strategy_track.json").read_text(encoding="utf-8"))
    assert config["feature_enabled"] is False
    forbidden = {"urllib", "urllib.request", "requests", "httpx", "aiohttp", "services.dualtrack_feishu", "services.feishu_report_sender", "services.live_money_guardrails", "services.broker_adapter"}
    violations: list[str] = []
    for path in sorted((ROOT / "services").glob("park_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                modules = [node.module or ""]
            else:
                continue
            for module in modules:
                if module in forbidden or any(module.startswith(prefix + ".") for prefix in forbidden):
                    violations.append(f"{path.name}:{module}")
    assert violations == []
