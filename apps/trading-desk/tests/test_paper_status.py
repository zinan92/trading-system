"""#1367: idle Paper is not a fault. The Dashboard answers 503 with a full body whenever no strategy runs."""
from __future__ import annotations

from trading_desk.app import paper_status_text

IDLE = {"schema_version": "park-paper-public-read-model-v1", "status": "blocked", "_http_status": 503,
        "blockers": ["active_strategy_missing", "grid_geometry_invalid"], "strategy": {"active": False, "state": "IDLE_CLEAN"}}


def test_idle_reads_as_idle() -> None:
    assert paper_status_text(IDLE) == "纸面盘：空闲，没有运行中的策略"


def test_any_other_blocker_is_named_even_when_idle() -> None:
    model = {**IDLE, "blockers": [*IDLE["blockers"], "identity_journal_invalid"]}
    assert paper_status_text(model) == "纸面盘：blocked（active_strategy_missing, grid_geometry_invalid, identity_journal_invalid）"


def test_a_blocked_running_strategy_is_not_called_idle() -> None:
    model = {**IDLE, "blockers": ["active_strategy_missing"], "strategy": {"active": True, "state": "ACTIVE_LOCKED"}}
    assert paper_status_text(model) == "纸面盘：blocked（active_strategy_missing）"


def test_ok_and_missing_body() -> None:
    assert paper_status_text({"status": "ok", "blockers": []}) == "纸面盘：ok"
    assert paper_status_text({}) == "纸面盘：None"
