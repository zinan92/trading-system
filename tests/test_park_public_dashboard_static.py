from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_park_public_dashboard_is_observer_only() -> None:
    html = (ROOT / "park-paper-dashboard.html").read_text(encoding="utf-8")
    lower = html.lower()

    assert "Public read-only" in html
    assert ">Paper<" in html
    assert "Telegram only" in html
    assert "/api/park-paper/read-model" in html
    assert "setInterval(refresh,15000)" in html
    assert "<button" not in lower
    assert "<form" not in lower
    assert 'method:"post"' not in lower
    assert "/api/strategy-console/control" not in html
    assert "/api/dualtrack/orders" not in html
    assert "refresh_recommendation" not in html


def test_park_public_dashboard_is_self_contained() -> None:
    html = (ROOT / "park-paper-dashboard.html").read_text(encoding="utf-8")

    assert "<style>" in html
    assert "<script>" in html
    assert "<script src=" not in html
    assert "<link rel=\"stylesheet\"" not in html
