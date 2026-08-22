from pathlib import Path

from services.journal_store import load_json
from pipelines.live_dca_canary import run_attended_canary


def test_attended_canary_pipeline_is_durable_blocker_without_transport(tmp_path: Path) -> None:
    result = run_attended_canary(
        output_root=tmp_path / "outputs",
        plan=None,
        park_user_id="park",
        park_chat_id="chat",
        action="start",
    )
    assert result["status"] == "blocked"
    assert result["blocker"] == "live_transport_not_registered"
    assert result["network_io"] is False
    assert result["live_writes_enabled"] is False
    assert load_json(tmp_path / "outputs" / "dualtrack" / "live_dca_canary" / "current.json")[0]["blocker"] == "live_transport_not_registered"
