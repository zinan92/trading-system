from pathlib import Path

from pipelines import dashboard_server


class _RecordingGate:
    instances: list["_RecordingGate"] = []

    def __init__(self, output_root: Path) -> None:
        self.output_root = Path(output_root)
        self.services: list[str] = []
        self.instances.append(self)

    def verify(self, service: str) -> dict:
        self.services.append(service)
        return {"ok": True, "output_root": str(self.output_root)}


def test_local_dashboard_boot_uses_configured_read_model_output_root(
    monkeypatch,
    tmp_path: Path,
) -> None:
    output_root = tmp_path / "park-paper-output"
    _RecordingGate.instances = []
    monkeypatch.setenv("TRADING_ORCHESTRATOR_OUTPUT_ROOT", str(output_root))
    monkeypatch.delenv("GRIDMIND_RUNTIME_MODE", raising=False)
    monkeypatch.setattr(dashboard_server, "PaperServiceBootGate", _RecordingGate)

    result = dashboard_server._verify_dashboard_boot()

    gate = _RecordingGate.instances[-1]
    assert gate.output_root == output_root
    assert gate.services == ["dashboard"]
    assert result == {"ok": True, "output_root": str(output_root)}


def test_cloud_dashboard_boot_uses_the_same_configured_output_root(
    monkeypatch,
    tmp_path: Path,
) -> None:
    output_root = tmp_path / "park-paper-output"
    _RecordingGate.instances = []
    monkeypatch.setenv("TRADING_ORCHESTRATOR_OUTPUT_ROOT", str(output_root))
    monkeypatch.setenv("GRIDMIND_RUNTIME_MODE", "cloud")
    monkeypatch.setattr(
        dashboard_server,
        "CloudPaperServiceBootGate",
        _RecordingGate,
    )

    result = dashboard_server._verify_dashboard_boot()

    gate = _RecordingGate.instances[-1]
    assert gate.output_root == output_root
    assert gate.services == ["dashboard"]
    assert result == {"ok": True, "output_root": str(output_root)}
