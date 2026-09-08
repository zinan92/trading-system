import json
from pathlib import Path

from tools.strategy_diff.contract import CONTRACT_VERSION, build_report, fixtures


def test_fixture_coverage_and_stable_report_digest():
    cases = fixtures()
    assert sum(c["family"] == "dca" for c in cases) >= 20
    assert sum(c["family"] == "grid" for c in cases) >= 20
    first = build_report()
    second = build_report()
    assert first["contract_version"] == CONTRACT_VERSION
    assert first["envelope_digest"] == second["envelope_digest"]
    assert first["case_counts"]["dca"] >= 20
    assert first["case_counts"]["grid"] >= 20
    assert all("match" in row and "differences" in row for row in first["cases"])


def test_report_json_is_serializable_and_has_field_level_diff_contract():
    report = build_report()
    encoded = json.dumps(report, sort_keys=True)
    decoded = json.loads(encoded)
    assert decoded["trading_strategy_sha"] == "d4daae915c549fe657a98b6cbf0e539886af355c"
    for row in decoded["cases"]:
        for diff in row["differences"]:
            assert diff["path"].startswith("$")
            assert diff["classification"] in {"semantic", "precision", "schema"}
            assert diff["recommendation"]
