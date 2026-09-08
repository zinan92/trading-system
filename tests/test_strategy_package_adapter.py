from __future__ import annotations

import ast
from pathlib import Path

import pytest

from services import strategy_package_adapter as adapter


def _market() -> dict:
    bars = [
        {"timestamp": f"2026-01-01T00:{i:02d}:00Z", "open": 100 + i * 0.1, "high": 101 + i * 0.1, "low": 99 + i * 0.1, "close": 100.2 + i * 0.1, "volume": 10}
        for i in range(32)
    ]
    contexts = {tf: {"provider": "fixture", "is_synthetic": False, "bars": bars} for tf in ("1d", "4h", "1m")}
    return {"status": "ready", "fresh": True, "is_synthetic": False, "provider": "fixture", "symbol": "XAUUSD", "latest_close": 103.2, "bars": bars, "strategy_timeframes": contexts}


def _config() -> dict:
    return {"cost_per_side_bp": 0.5, "max_leverage": 10.0, "execution_contract": {"price_decimals": 2, "price_increment": "0.01", "quantity_increment": "0.01"}, "strategy_grid": {"range_timeframe": "1d", "spacing_timeframe": "4h", "execution_timeframe": "1m", "range_atr_period": 14, "spacing_atr_period": 14, "min_grid_count": 2, "max_grid_count": 12, "capital_utilization_cap": 1.0, "min_net_profit_per_grid_usd": 0.01, "styles": {"steady": {"range_atr_multiple": 2, "spacing_atr_multiple": 1}, "aggressive": {"range_atr_multiple": 3, "spacing_atr_multiple": 0.5}}}}


def _dca_payload() -> dict:
    return {"direction": "long", "dca": {"entry_levels": [102.0, 101.0, 100.0], "target_price": 105.0, "stop_price": 98.0, "notional_per_addition": 100.0, "max_additions": 3, "loop_enabled": False}}


def test_default_mode_is_internal_and_does_not_load_package(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv(adapter.MODE_ENV, raising=False)
    monkeypatch.setattr(adapter, "PACKAGE_ROOT", str(tmp_path / "missing"))
    result = adapter.build_dca_preview("cycle", _dca_payload(), market=_market(), account={"equity": 10_000}, config=_config(), output_root=tmp_path)
    assert result["strategy_type"] == "dca"
    assert not (tmp_path / adapter.RECEIPT_PATH).exists()


@pytest.mark.parametrize("mode", ["internal", "shadow"])
def test_both_modes_return_authoritative_internal_preview(monkeypatch, tmp_path: Path, mode: str) -> None:
    monkeypatch.setenv(adapter.MODE_ENV, mode)
    result = adapter.build_grid_preview("cycle", {"direction": "neutral", "style": "steady"}, market=_market(), account={"equity": 10_000}, config=_config(), output_root=tmp_path)
    assert "grid" in result and "orders" in result
    assert result["preview_id"].startswith("grid-preview-")


def test_shadow_writes_only_a_mismatch_receipt(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv(adapter.MODE_ENV, "shadow")
    adapter._run("probe", lambda: {"value": 1}, lambda: {"value": 2}, (), {}, output_root=tmp_path)
    receipt = tmp_path / adapter.RECEIPT_PATH
    assert receipt.exists()
    assert receipt.read_text(encoding="utf-8").count('"operation": "probe"') == 1


def test_adapter_import_boundary_has_no_risk_lifecycle_or_authorization_imports() -> None:
    tree = ast.parse(Path(adapter.__file__).read_text(encoding="utf-8"))
    banned = ("risk", "lifecycle", "authorization")
    imported = [node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
    assert not any(any(term in module.lower() for term in banned) for module in imported)
