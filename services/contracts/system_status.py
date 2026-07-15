"""System-status contract interface; implementation is pinned in pipelines.dashboard_server (tests monkeypatch its module globals)."""

from pipelines.dashboard_server import build_system_status_contract

__all__ = ["build_system_status_contract"]
