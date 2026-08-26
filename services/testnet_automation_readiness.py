"""Two-window Testnet automation readiness built on the existing evidence recorder."""

from __future__ import annotations

from pathlib import Path

from services.testnet_soak_readiness import TestnetSoakError, TestnetSoakReadiness


TESTNET_AUTOMATION_READINESS_SCHEMA = "testnet-automation-readiness-v1"
TESTNET_AUTOMATION_WINDOW_COUNT = 2
TESTNET_AUTOMATION_REQUIRED_DAYS = 1


class TestnetAutomationReadinessError(TestnetSoakError):
    """Stable blocker for a mode or Instrument mismatch in Testnet evidence."""

    __test__ = False


class TestnetAutomationReadiness(TestnetSoakReadiness):
    """Persist two 12-hour windows for one BTC Strategy Family."""

    __test__ = False

    def __init__(
        self,
        output_root: Path,
        *,
        strategy_family: str,
        instrument_id: str = "BTC-USD-PERP",
    ) -> None:
        family = str(strategy_family or "").strip().lower()
        if family not in {"dca", "grid"}:
            raise TestnetAutomationReadinessError("strategy_family_invalid")
        instrument = str(instrument_id or "").strip()
        if not instrument:
            raise TestnetAutomationReadinessError("instrument_id_required")
        super().__init__(
            output_root,
            window_count=TESTNET_AUTOMATION_WINDOW_COUNT,
            required_day_count=TESTNET_AUTOMATION_REQUIRED_DAYS,
            root_name=f"testnet_automation/readiness/{family}/{instrument}",
            schema_version=TESTNET_AUTOMATION_READINESS_SCHEMA,
        )
        self.strategy_family = family
        self.instrument_id = instrument

    def record_window(self, observation):
        if str(observation.get("strategy_family") or "").strip().lower() != self.strategy_family:
            raise TestnetAutomationReadinessError("strategy_family_mismatch")
        if str(observation.get("instrument_id") or "").strip() != self.instrument_id:
            raise TestnetAutomationReadinessError("instrument_id_mismatch")
        return super().record_window(observation)

    def public_status(self, *, now: str | None = None):
        return {
            **super().public_status(now=now),
            "schema_version": TESTNET_AUTOMATION_READINESS_SCHEMA,
            "strategy_family": self.strategy_family,
            "instrument_id": self.instrument_id,
            "live_enabled": False,
            "live_writes_enabled": False,
        }


__all__ = [
    "TESTNET_AUTOMATION_READINESS_SCHEMA",
    "TESTNET_AUTOMATION_REQUIRED_DAYS",
    "TESTNET_AUTOMATION_WINDOW_COUNT",
    "TestnetAutomationReadiness",
    "TestnetAutomationReadinessError",
]
