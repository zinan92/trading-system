"""Provider-neutral configuration for the legacy local signal backtester."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class LocalBacktestConfig:
    stop_pct: float = 0.006
    target_pct: float = 0.012
    max_hold_bars: int = 24
    min_sample_size: int = 20
    supportive_min_win_rate: float = 0.52
    supportive_min_avg_r: float = 0.0
    supportive_min_profit_factor: float = 1.15
    supportive_max_drawdown_pct: float = 8.0
    mixed_min_profit_factor: float = 1.0
    mixed_min_avg_r: float = -0.05

    @classmethod
    def from_strategy_config(cls, config: dict | None = None) -> LocalBacktestConfig:
        config = config or {}
        if "gold_5m_v1" in config:
            config = config["gold_5m_v1"]
        backtest = config.get("backtest", config)
        supportive = backtest.get("supportive", {})
        mixed = backtest.get("mixed", {})
        return cls(
            stop_pct=float(backtest.get("stop_pct", cls.stop_pct)),
            target_pct=float(backtest.get("target_pct", cls.target_pct)),
            max_hold_bars=int(backtest.get("max_hold_bars", cls.max_hold_bars)),
            min_sample_size=int(backtest.get("min_sample_size", cls.min_sample_size)),
            supportive_min_win_rate=float(
                supportive.get("min_win_rate", cls.supportive_min_win_rate)
            ),
            supportive_min_avg_r=float(
                supportive.get("min_avg_r", cls.supportive_min_avg_r)
            ),
            supportive_min_profit_factor=float(
                supportive.get("min_profit_factor", cls.supportive_min_profit_factor)
            ),
            supportive_max_drawdown_pct=float(
                supportive.get("max_drawdown_pct", cls.supportive_max_drawdown_pct)
            ),
            mixed_min_profit_factor=float(
                mixed.get("min_profit_factor", cls.mixed_min_profit_factor)
            ),
            mixed_min_avg_r=float(mixed.get("min_avg_r", cls.mixed_min_avg_r)),
        )

    def to_strategy_config(self) -> dict:
        """Return the exact request mapping consumed by any compatible adapter."""

        return {
            "stop_pct": self.stop_pct,
            "target_pct": self.target_pct,
            "max_hold_bars": self.max_hold_bars,
            "min_sample_size": self.min_sample_size,
            "supportive": {
                "min_win_rate": self.supportive_min_win_rate,
                "min_avg_r": self.supportive_min_avg_r,
                "min_profit_factor": self.supportive_min_profit_factor,
                "max_drawdown_pct": self.supportive_max_drawdown_pct,
            },
            "mixed": {
                "min_profit_factor": self.mixed_min_profit_factor,
                "min_avg_r": self.mixed_min_avg_r,
            },
        }
