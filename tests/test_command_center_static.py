from __future__ import annotations

from pathlib import Path

from pipelines import dashboard_server


ROOT = Path(__file__).resolve().parents[1]


def _read(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def test_command_center_page_loads_global_shell_assets() -> None:
    html = _read("command-center.html")

    assert 'href="assets/shell.css"' in html
    assert 'src="assets/shell.js"' in html


def test_dashboard_root_redirects_to_command_center() -> None:
    handler = object.__new__(dashboard_server.DashboardHandler)
    handler.path = "/"
    captured: dict[str, object] = {"headers": []}
    handler.send_response = lambda status: captured.update({"status": status})
    handler.send_header = lambda key, value: captured["headers"].append((key, value))
    handler.end_headers = lambda: captured.update({"ended": True})

    handler.do_GET()

    assert captured["status"] == 302
    assert ("Location", "command-center.html") in captured["headers"]
    assert captured["ended"] is True


def test_command_center_static_empty_states_are_present() -> None:
    html = _read("command-center.html")

    assert "尚未有人轨方向分裁决记录" in html
    assert "暂无已收盘周期" in html
    assert "当前无阻塞" in html
    assert "数据不可读" in html
