from __future__ import annotations

import json
import subprocess
import textwrap
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _node_json(script: str) -> dict:
    result = subprocess.run(
        ["node", "-e", textwrap.dedent(script)],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def test_standard_kline_adapter_handles_tiger_binance_and_synthetic_payloads() -> None:
    result = _node_json(
        """
        const kline = require("./packages/standard-kline/standard-kline.js");
        function payload(provider, sourceMode, flags, synthetic) {
          return {
            schema_version:"dualtrack-market-bars-v1",
            status: synthetic ? "seeded" : "ready",
            source_mode: sourceMode,
            symbol: provider.startsWith("tiger") ? "MGCmain" : "GOLD",
            timeframe:"1m",
            provider,
            quality_flags: flags,
            is_synthetic: synthetic,
            bars:[
              {symbol:"GOLD", timeframe:"1m", timestamp:"2026-07-05T01:00:00+00:00", open:4100, high:4102, low:4099, close:4101, volume:12, provider, quality_flags:flags},
              {symbol:"GOLD", timeframe:"1m", timestamp:"2026-07-05T01:01:00+00:00", open:4101, high:4104, low:4100, close:4103, volume:18, provider, quality_flags:flags},
            ],
          };
        }
        const tiger = kline.adaptBarPayload(payload("tiger_openapi:COMEX", "tiger_openapi", [], false));
        const binance = kline.adaptBarPayload(payload("binance_usdm", "binance_usdm_fallback", [], false));
        const seeded = kline.adaptBarPayload(payload("synthetic_seed:dualtrack_dashboard", "synthetic_fallback", ["synthetic_seed","display_only","not_for_trading_signal"], true));
        console.log(JSON.stringify({
          tiger:{time:tiger.candles[0].time, provider:tiger.meta.provider, mode:tiger.meta.source_mode, flags:tiger.meta.quality_flags, synthetic:tiger.meta.is_synthetic},
          binance:{time:binance.candles[1].time, provider:binance.meta.provider, mode:binance.meta.source_mode, synthetic:binance.meta.is_synthetic},
          seeded:{provider:seeded.meta.provider, mode:seeded.meta.source_mode, flags:seeded.meta.quality_flags, synthetic:seeded.meta.is_synthetic},
          volumes:seeded.volumes.map(row => row.value),
        }));
        """
    )

    assert result["tiger"] == {
        "time": 1783213200,
        "provider": "tiger_openapi:COMEX",
        "mode": "tiger_openapi",
        "flags": [],
        "synthetic": False,
    }
    assert result["binance"] == {
        "time": 1783213260,
        "provider": "binance_usdm",
        "mode": "binance_usdm_fallback",
        "synthetic": False,
    }
    assert result["seeded"]["synthetic"] is True
    assert result["seeded"]["flags"] == ["synthetic_seed", "display_only", "not_for_trading_signal"]
    assert result["volumes"] == [12, 18]


def test_standard_kline_nearest_time_and_synthetic_flags_opt_in() -> None:
    result = _node_json(
        """
        const kline = require("./packages/standard-kline/standard-kline.js");
        const candles = [
          {time:1783213200, open:4100, high:4102, low:4099, close:4101},
          {time:1783213260, open:4101, high:4104, low:4100, close:4103},
        ];
        const nearest = kline.nearestTime(candles, "2026-07-05T01:01:20+00:00");
        const payload = {quality_flags:["demo_only"], bars:[{timestamp:"2026-07-05T01:00:00+00:00", open:4100, high:4102, low:4099, close:4101}]};
        const withoutOptIn = kline.adaptBarPayload(payload);
        const withOptIn = kline.adaptBarPayload(payload, {syntheticFlags:["demo_only"]});
        console.log(JSON.stringify({
          nearest,
          syntheticWithoutOptIn: withoutOptIn.meta.is_synthetic,
          syntheticWithOptIn: withOptIn.meta.is_synthetic,
        }));
        """
    )

    assert result["nearest"] == 1783213260
    assert result["syntheticWithoutOptIn"] is False
    assert result["syntheticWithOptIn"] is True


def test_standard_kline_clamps_extreme_logical_ranges() -> None:
    result = _node_json(
        """
        const kline = require("./packages/standard-kline/standard-kline.js");
        const wide = kline.clampLogicalRange({from:-50, to:200}, 96, {rightOffset:8});
        const shifted = kline.clampLogicalRange({from:80, to:220}, 96, {bufferBars:12, rightOffset:8});
        const tiny = kline.clampLogicalRange({from:50, to:50.1}, 96, {bufferBars:12, minVisibleBars:6});
        console.log(JSON.stringify({
          wide,
          shifted,
          tinyWidth:Number((tiny.to - tiny.from).toFixed(6)),
        }));
        """
    )

    assert result["wide"] == {"from": -12, "to": 107}
    assert result["shifted"]["to"] <= 107
    assert result["shifted"]["from"] >= -12
    assert result["tinyWidth"] >= 6
