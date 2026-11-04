"""Freeze the trusted server clock for fixtures that carry fixed calendar dates.

Policy, binding and envelope code deliberately reads the server clock instead of
caller-supplied ``as_of`` values. Fixtures written in July 2026 therefore
expired on their own on 2026-08-31 and turned the suite red without any code
change. Freezing the clock where those fixtures are used keeps the production
rule ("never trust caller time") and makes the tests independent of the date
they run on.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

SERVER_CLOCK_MODULES = (
    "services.cycle_risk_envelope",
    "services.strategy_control_plane",
)


def freeze_server_clock(monkeypatch: pytest.MonkeyPatch, iso: str) -> datetime:
    frozen = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    if frozen.tzinfo is None:
        frozen = frozen.replace(tzinfo=timezone.utc)

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):  # type: ignore[override]
            return frozen.astimezone(tz) if tz is not None else frozen.replace(tzinfo=None)

        @classmethod
        def utcnow(cls):  # type: ignore[override]
            return frozen.astimezone(timezone.utc).replace(tzinfo=None)

    for module in SERVER_CLOCK_MODULES:
        monkeypatch.setattr(f"{module}.datetime", FrozenDatetime)
    return frozen
