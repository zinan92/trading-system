from __future__ import annotations

from schemas.market_data import Bar, CleanDatasetManifest


class DataQualityGate:
    def __init__(self, config: dict | None = None) -> None:
        self.config = config or {}
        self.enabled = bool(self.config.get("enabled", True))
        self.max_missing_ratio = float(self.config.get("max_missing_ratio", 0.05))
        self.max_synthetic_ratio = float(self.config.get("max_synthetic_ratio", 0.2))
        self.max_spike_flags = int(self.config.get("max_spike_flags", 0))
        self.min_clean_rows = int(self.config.get("min_clean_rows", 200))
        self.allow_latest_synthetic = bool(self.config.get("allow_latest_synthetic", False))

    def evaluate(self, manifest: CleanDatasetManifest, bars: list[Bar]) -> dict:
        clean_rows = int(manifest.clean_rows)
        missing_ratio = (float(manifest.missing_bars) / clean_rows) if clean_rows else 1.0
        synthetic_rows = sum(1 for bar in bars if "synthetic_seed" in bar.quality_flags or bar.provider == "local_synthetic_seed")
        synthetic_ratio = (float(synthetic_rows) / clean_rows) if clean_rows else 1.0
        latest_bar = bars[-1] if bars else None
        latest_is_synthetic = bool(
            latest_bar
            and ("synthetic_seed" in latest_bar.quality_flags or latest_bar.provider == "local_synthetic_seed")
        )
        latest_is_quote_derived = bool(latest_bar and "quote_derived_bar" in latest_bar.quality_flags)
        latest_fields = {
            "latest_provider": latest_bar.provider if latest_bar else "",
            "latest_timestamp": latest_bar.timestamp if latest_bar else "",
            "latest_is_synthetic": latest_is_synthetic,
            "latest_is_quote_derived": latest_is_quote_derived,
            "allow_latest_synthetic": self.allow_latest_synthetic,
        }
        reasons = []
        if not self.enabled:
            return {
                "enabled": False,
                "allows_trading": True,
                "reasons": [],
                "clean_rows": clean_rows,
                "missing_bars": manifest.missing_bars,
                "missing_ratio": round(missing_ratio, 4),
                "synthetic_rows": synthetic_rows,
                "synthetic_ratio": round(synthetic_ratio, 4),
                "spike_flags": manifest.spike_flags,
                **latest_fields,
            }
        if clean_rows < self.min_clean_rows:
            reasons.append(f"clean rows {clean_rows} below minimum {self.min_clean_rows}")
        if missing_ratio > self.max_missing_ratio:
            reasons.append(f"missing ratio {missing_ratio:.2%} above max {self.max_missing_ratio:.2%}")
        if synthetic_ratio > self.max_synthetic_ratio:
            reasons.append(f"synthetic ratio {synthetic_ratio:.2%} above max {self.max_synthetic_ratio:.2%}")
        if latest_is_synthetic and not self.allow_latest_synthetic:
            reasons.append("latest bar is synthetic; import official/recent GOLD 5m bars before trading")
        return {
            "enabled": True,
            "allows_trading": not reasons,
            "reasons": reasons,
            "clean_rows": clean_rows,
            "missing_bars": manifest.missing_bars,
            "missing_ratio": round(missing_ratio, 4),
            "synthetic_rows": synthetic_rows,
            "synthetic_ratio": round(synthetic_ratio, 4),
            "spike_flags": manifest.spike_flags,
            **latest_fields,
            "thresholds": {
                "max_missing_ratio": self.max_missing_ratio,
                "max_synthetic_ratio": self.max_synthetic_ratio,
                "max_spike_flags": self.max_spike_flags,
                "spike_flags_blocking": False,
                "min_clean_rows": self.min_clean_rows,
                "allow_latest_synthetic": self.allow_latest_synthetic,
            },
        }
