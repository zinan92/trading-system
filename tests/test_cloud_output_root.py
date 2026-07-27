from pathlib import Path

from services.contracts.dualtrack import _dualtrack_output_root
from services.contracts.system import dashboard_output_root


def test_cloud_output_root_environment_is_authoritative(
    tmp_path: Path,
    monkeypatch,
):
    root = tmp_path / "persistent"
    monkeypatch.setenv("TRADING_ORCHESTRATOR_OUTPUT_ROOT", str(root))

    assert _dualtrack_output_root() == root
    assert dashboard_output_root() == root
