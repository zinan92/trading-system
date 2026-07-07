from __future__ import annotations

import os
from pathlib import Path

from services.journal_store import load_json
from services.tiger_openapi_paper_order_drill import DRILL_PROPS_ENV, TigerOpenApiPaperOrderDrill


def test_tiger_paper_order_drill_writes_fake_client_evidence_without_real_network(tmp_path: Path):
    output_root = tmp_path / "outputs"

    result = TigerOpenApiPaperOrderDrill(output_root=output_root, run_id="drilltest").run("2026-07-05")

    assert result["status"] == "pass"
    assert result["mode"] == "local_fake_tradeclient"
    assert result["real_tiger_network_call_attempted"] is False
    assert result["safety"]["real_tiger_sdk_client_created"] is False
    assert result["safety"]["uses_injected_fake_trade_client"] is True
    assert result["safety"]["checked_in_profile_dry_run"] is True
    assert result["safety"]["checked_in_confirm_tiger_paper_orders"] is False
    assert [item["name"] for item in result["scenarios"]] == ["default_guardrail_block", "simulated_green_order"]

    guardrail = result["scenarios"][0]
    assert guardrail["status"] == "pass"
    assert guardrail["evidence"]["guardrail_status"] == "BLOCKED_SINGLE_ORDER_NOTIONAL_LIMIT"
    assert guardrail["evidence"]["no_preview_or_place"] is True
    assert guardrail["evidence"]["network_order_created"] is False
    assert guardrail["evidence"]["call_sequence"] == ["get_positions", "get_open_orders", "get_prime_assets"]

    green = result["scenarios"][1]
    assert green["status"] == "pass"
    assert green["evidence"]["call_sequence"] == ["get_positions", "get_open_orders", "get_prime_assets", "preview_order", "place_order"]
    assert green["evidence"]["guardrail_status"] == "READY"
    assert green["evidence"]["protective_status"] == "attached_in_parent_order"
    assert green["evidence"]["protective_leg_count"] == 2
    assert green["evidence"]["lifecycle_state"] == "accepted"
    assert green["evidence"]["real_tiger_network_call_attempted"] is False

    current = load_json(output_root / "tiger_paper_order_drill" / "current.json")[-1]
    run_id = load_json(output_root / "tiger_paper_order_drill" / "drilltest.json")[-1]
    assert current["run_id"] == "drilltest"
    assert run_id["status"] == "pass"
    assert (output_root / "tiger_paper_order_drill" / "drilltest.md").exists()
    assert (output_root / "tiger_paper_order_drill_runtime" / "drilltest" / "simulated_green_order" / "tiger_order_requests" / "2026-07-05.json").exists()


def test_tiger_paper_order_drill_restores_temporary_props_env(tmp_path: Path, monkeypatch):
    monkeypatch.setenv(DRILL_PROPS_ENV, "/tmp/existing-tiger-drill-props")

    result = TigerOpenApiPaperOrderDrill(output_root=tmp_path / "outputs", run_id="drilltest").run(
        "2026-07-05",
        scenarios=["simulated_green_order"],
    )

    assert result["status"] == "pass"
    assert os.environ[DRILL_PROPS_ENV] == "/tmp/existing-tiger-drill-props"


def test_tiger_paper_order_drill_clears_temporary_props_env_when_absent(tmp_path: Path, monkeypatch):
    monkeypatch.delenv(DRILL_PROPS_ENV, raising=False)

    result = TigerOpenApiPaperOrderDrill(output_root=tmp_path / "outputs", run_id="drilltest").run(
        "2026-07-05",
        scenarios=["default_guardrail_block"],
    )

    assert result["status"] == "pass"
    assert DRILL_PROPS_ENV not in os.environ
