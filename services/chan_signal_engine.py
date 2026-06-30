"""Chan-theory (缠论) signal engine — enters on 二买 / 类二买 (segment-level
second-buy and its variant), wrapping the vendored chan core.

This is a SECOND engine type alongside the MA `SignalEngine`. It is selected via
a strategy's `engine: chan` config. Requires Python >=3.11 + pandas (the chan
core), so chan strategies run under the 3.13 strategies job, not the 3.9 runtime.

Recipe (proven on real 1m gold): drive the chan core in step mode with the
`area` MACD divergence metric (reads a per-symbol CSV we write), collect
segment-level buy/sell points whose type is `2` (二买/二卖) or `2s` (类二买/类二卖),
and emit a long (buy) / short (sell) signal when such a point forms on a recent
bar. 二买 on gold is genuinely sparse, so the config is loosened
(`max_bs2_rate`, `divergence_rate`) to surface 类二买 — tunable per strategy.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import warnings
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from schemas.signal import Signal

_CHAN_DIR = Path(__file__).resolve().parent / "chan_vendor"

# Proven default config (14d 1m gold → ~42 二买/类二买). Overridable per strategy.
_DEFAULT_CHAN_CONFIG = {
    "zs_combine": True,
    "zs_combine_mode": "zs",
    "bi_strict": True,
    "mean_metrics": [],
    "trigger_step": True,
    "seg_algo": "chan",
    "divergence_rate": 1.1,
    "min_zs_cnt": 1,
    "max_bs2_rate": 1.0,
    "bs1_peak": True,
    "macd_algo": "area",
    "one_bi_zs": True,
    "bs_type": "1,1p,2,2s,3a,3b",
    "zs_algo": "normal",
}

_TARGET_TYPES = {"2", "2s"}  # 二买/二卖 (2) and 类二买/类二卖 (2s)
_DETECT_POINTS_CACHE: dict[str, list[dict]] = {}
_HISTORICAL_SIGNALS_CACHE: dict[str, list[dict]] = {}
_CACHE_MAX_ITEMS = 16


class ChanSignalEngine:
    def __init__(self, params: dict | None = None) -> None:
        self.params = params or {}
        signal_cfg = self.params.get("signal", {}) or {}
        self.symbol_code = str(self.params.get("symbol", "GOLD"))
        self.fresh_bars = int(signal_cfg.get("fresh_bars", 3))
        self.min_bars = int(signal_cfg.get("min_bars", 300))
        self.data_dir = Path(signal_cfg.get("chan_data_dir") or os.getenv("CHAN_DATA_DIR") or "/tmp/chan_data")
        self.chan_config = {**_DEFAULT_CHAN_CONFIG, **(signal_cfg.get("chan_config") or {})}
        # Which segment buy/sell-point types this strategy trades. Default is
        # 二买/类二买 (byte-identical to the original engine); set `bsp_types` to
        # ["1","1p"] for a 一买 strategy, etc. — the chan core computes all of
        # `bs_type`, we just filter which ones become signals here.
        self.target_types = set(signal_cfg.get("bsp_types") or _TARGET_TYPES)

    # ------------------------------------------------------------------ detection
    def _new_chan(self, candles: list):
        """Build a fresh CChan over `candles`: load the chan core, reset its
        class-level caches (so repeated constructions in one process don't crash
        on stale state), write the per-symbol CSV the area-MACD reads, and return
        (chan, to_klu, KL_TYPE). Bars are NOT fed yet — callers choose per-bar
        (causal, live) or feed-all (fast, backtest)."""
        if str(_CHAN_DIR) not in sys.path:
            sys.path.insert(0, str(_CHAN_DIR))
        from Chan import CChan
        from ChanConfig import CChanConfig
        from Common.CEnum import AUTYPE, KL_TYPE
        from Common.CTime import CTime
        from KLine.KLine_Unit import CKLine_Unit
        from Bi.Bi import CBi
        from Seg.Seg import CSeg

        for cls in (CBi, CSeg):
            if hasattr(cls, "_use_cache"):
                cls._use_cache = False
            if hasattr(cls, "_data_cache"):
                cls._data_cache = None

        self.data_dir.mkdir(parents=True, exist_ok=True)
        os.environ["CHAN_DATA_DIR"] = str(self.data_dir)
        csv_path = self.data_dir / f"{self.symbol_code}.csv"
        with csv_path.open("w", encoding="utf-8") as handle:
            handle.write("timestamp,open,high,low,close,volume\n")
            for bar in candles:
                dt = self._parse(bar.timestamp)
                handle.write(f"{dt.strftime('%Y-%m-%d %H:%M:%S')},{bar.open},{bar.high},{bar.low},{bar.close},{bar.volume}\n")

        def to_klu(bar):
            dt = self._parse(bar.timestamp)
            return CKLine_Unit(
                {"time": CTime(dt.year, dt.month, dt.day, dt.hour, dt.minute, auto=False),
                 "open": bar.open, "high": bar.high, "low": bar.low, "close": bar.close, "volume": bar.volume},
                autofix=True,
            )

        # coin = the symbol the area-MACD reads (<coin>.csv, written above).
        # code = a distinct, intentionally-absent data-source key so csvAPI yields
        # nothing (we feed klines via trigger_load), avoiding any double-feed.
        # Fresh dict copy: CChanConfig mutates (pops from) the dict it's given,
        # so reusing self.chan_config across constructions silently drops keys
        # (e.g. trigger_step) and flips later runs into non-step mode → crash.
        chan = CChan(coin=self.symbol_code, code=f"{self.symbol_code}__feed", data_src="custom:csvAPI.CSV_API",
                     lv_list=[KL_TYPE.K_1M], config=CChanConfig(dict(self.chan_config)), autype=AUTYPE.NONE)
        return chan, to_klu, KL_TYPE

    def detect_points(self, candles: list) -> list[dict]:
        """Return every detected 二买/类二买 point: {time, is_buy, types, close}.
        Causal: scans after each bar (matching live step-mode), so this is what
        `generate` uses for fresh-signal detection."""
        cache_key = self._cache_key("detect", candles)
        if cache_key in _DETECT_POINTS_CACHE:
            return [dict(item) for item in _DETECT_POINTS_CACHE[cache_key]]
        with self._suppress_vendor_parse_warning():
            chan, to_klu, KL_TYPE = self._new_chan(candles)
            seen: dict[str, dict] = {}
            for bar in candles:
                try:
                    chan.trigger_load({KL_TYPE.K_1M: [to_klu(bar)]})
                    for point in chan.get_seg_bsp():
                        types = [t.value for t in point.type]
                        if self.target_types.intersection(types):
                            seen[str(point.klu.time)] = {
                                "time": str(point.klu.time),
                                "is_buy": bool(point.is_buy),
                                "types": types,
                                "close": float(point.klu.close),
                            }
                except Exception:
                    continue
        points = sorted(seen.values(), key=lambda x: x["time"])
        self._cache_put(_DETECT_POINTS_CACHE, cache_key, points)
        return [dict(item) for item in points]

    def historical_signals(self, candles: list) -> list[dict]:
        """Every matched buy/sell point over the series as {index, direction},
        via feed-all-then-scan-once (no per-bar rescans) — far faster than
        `detect_points`, for the backtester. NOTE: segment confirmation uses the
        full window, so entries carry a mild look-ahead vs the causal live engine;
        acceptable for first-cut triage, not a substitute for forward paper."""
        if len(candles) < self.min_bars:
            return []
        cache_key = self._cache_key("historical", candles)
        if cache_key in _HISTORICAL_SIGNALS_CACHE:
            return [dict(item) for item in _HISTORICAL_SIGNALS_CACHE[cache_key]]
        with self._suppress_vendor_parse_warning():
            chan, to_klu, KL_TYPE = self._new_chan(candles)
            for bar in candles:
                try:
                    chan.trigger_load({KL_TYPE.K_1M: [to_klu(bar)]})
                except Exception:
                    continue
            index_by_time = {self._norm(bar.timestamp): i for i, bar in enumerate(candles)}
            out = []
            for point in chan.get_seg_bsp():
                types = [t.value for t in point.type]
                if not self.target_types.intersection(types):
                    continue
                index = index_by_time.get(self._norm_chan(str(point.klu.time)))
                if index is not None:
                    out.append({"index": index, "direction": "long" if point.is_buy else "short"})
        signals = sorted(out, key=lambda item: item["index"])
        self._cache_put(_HISTORICAL_SIGNALS_CACHE, cache_key, signals)
        return [dict(item) for item in signals]

    # ------------------------------------------------------------------ signal
    def generate(self, asset, candles, events=None, run_date: str = "", factor_context=None) -> Signal:
        if len(candles) < self.min_bars:
            return self._no_signal(asset, run_date, f"need >= {self.min_bars} bars for chan structure")
        points = self.detect_points(candles)
        last = candles[-1]
        if not points:
            return self._no_signal(asset, run_date, "no 二买/类二买 detected", last)
        latest = points[-1]
        # Fresh only if the point formed within the last `fresh_bars` candles.
        recent_times = {self._norm(b.timestamp) for b in candles[-self.fresh_bars:]}
        if self._norm_chan(latest["time"]) not in recent_times:
            return self._no_signal(asset, run_date, f"latest 二买/类二买 at {latest['time']} not fresh", last)
        direction = "long" if latest["is_buy"] else "short"
        kind, regime, invalid_if = self._bsp_meta(latest["types"])
        return Signal(
            signal_id=self._sig_id(asset.symbol, run_date, direction, last.close),
            asset=asset.symbol, asset_class=asset.asset_class,
            direction=direction, strength=70, confidence=65, horizon="chan_1m",
            thesis=f"{asset.symbol} 缠论{kind}信号({','.join(latest['types'])}) 形成,{'多' if direction=='long' else '空'}头入场。",
            evidence=[f"chan seg bsp types={latest['types']}", f"point time={latest['time']}", f"detected points={len(points)}"],
            regime=regime, factor_scores={"chan": 100, "trend": 0, "macro": 0, "volatility": 0},
            source_artifacts=[f"clean_bars/{run_date}/{asset.symbol}_{last.timeframe}.json"],
            invalid_if=invalid_if,
            generated_at=datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            expires_at=(datetime.now(timezone.utc) + timedelta(days=1)).replace(microsecond=0).isoformat(),
            status="new",
        )

    def _bsp_meta(self, point_types: list) -> tuple[str, str, str]:
        """Label (kind, regime, invalid_if) for the buy/sell-point type this
        strategy matched. Resolved against `target_types` so a 一买 strategy is
        labelled 一买 even if the point also carries other types. The default
        (二买/类二买) path is byte-identical to the original engine."""
        matched = self.target_types.intersection(point_types)
        if matched & {"2", "2s"}:
            kind = "类二买/类二卖" if "2s" in matched and "2" not in matched else "二买/二卖"
            return kind, "chan_second_buy", "价格跌破二买参考低点(中枢/前低)。"
        if matched & {"1", "1p"}:
            kind = "盘整背驰一买/一卖" if "1p" in matched and "1" not in matched else "一买/一卖"
            return kind, "chan_first_buy", "价格跌破一买背驰段低点。"
        if matched & {"3a", "3b"}:
            return "三买/三卖", "chan_third_buy", "价格跌回中枢内,三买失败。"
        return "买卖点", "chan_bsp", ""

    # ------------------------------------------------------------------ helpers
    def _parse(self, ts: str) -> datetime:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))

    def _norm(self, ts: str) -> str:
        return self._parse(ts).strftime("%Y%m%d%H%M")

    def _norm_chan(self, chan_time: str) -> str:
        # chan CTime str like "2026/01/02 03:04" or "2026-01-02 03:04:00"
        digits = "".join(ch for ch in str(chan_time) if ch.isdigit())
        return digits[:12]

    def _sig_id(self, symbol: str, run_date: str, direction: str, close: float) -> str:
        digest = hashlib.sha256(f"chan:{symbol}:{run_date}:{direction}:{close}".encode()).hexdigest()[:10]
        return f"sig_chan_{symbol.lower()}_{run_date.replace('-', '')}_{digest}"

    def _cache_key(self, purpose: str, candles: list) -> str:
        payload = {
            "purpose": purpose,
            "symbol": self.symbol_code,
            "target_types": sorted(self.target_types),
            "chan_config": self.chan_config,
            "bars": self._candles_digest(candles),
            "bar_count": len(candles),
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")).hexdigest()

    def _candles_digest(self, candles: list) -> str:
        digest = hashlib.sha256()
        for bar in candles:
            digest.update(
                f"{getattr(bar, 'timestamp', '')}|{getattr(bar, 'timeframe', '')}|"
                f"{getattr(bar, 'open', '')}|{getattr(bar, 'high', '')}|"
                f"{getattr(bar, 'low', '')}|{getattr(bar, 'close', '')}|{getattr(bar, 'volume', '')};".encode("utf-8")
            )
        return digest.hexdigest()

    def _cache_put(self, cache: dict[str, list[dict]], key: str, value: list[dict]) -> None:
        if len(cache) >= _CACHE_MAX_ITEMS and key not in cache:
            cache.pop(next(iter(cache)))
        cache[key] = [dict(item) for item in value]

    def _no_signal(self, asset, run_date: str, reason: str, last=None) -> Signal:
        now = datetime.now(timezone.utc).replace(microsecond=0)
        return Signal(
            signal_id=self._sig_id(asset.symbol, run_date, "watch", last.close if last else 0),
            asset=asset.symbol, asset_class=asset.asset_class, direction="watch", strength=0, confidence=0,
            horizon="chan_1m", thesis=reason, evidence=[reason], regime="no_trade",
            factor_scores={"chan": 0, "trend": 0, "macro": 0, "volatility": 0}, source_artifacts=[],
            invalid_if="", generated_at=now.isoformat(), expires_at=now.isoformat(), status="no_signal",
        )

    @contextmanager
    def _suppress_vendor_parse_warning(self):
        with warnings.catch_warnings():
            warnings.filterwarnings(
                "ignore",
                message=r"Could not infer format, so each element will be parsed individually.*",
                category=UserWarning,
            )
            yield
