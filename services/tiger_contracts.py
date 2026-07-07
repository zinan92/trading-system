from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from services.config_loader import ROOT, load_pipeline_config
from services.journal_store import write_json


@dataclass(frozen=True)
class TigerContractResolution:
    requested_symbol: str
    execution_symbol: str
    root_symbol: str
    contract_spec: dict[str, Any]
    status: str
    block_reason: str
    mapped: bool
    continuous_input: bool
    rollover_policy: dict[str, Any]
    days_to_contract_month: int | None

    @property
    def ready(self) -> bool:
        return self.status == "ready"

    def to_dict(self) -> dict[str, Any]:
        return {
            "requested_symbol": self.requested_symbol,
            "execution_symbol": self.execution_symbol,
            "root_symbol": self.root_symbol,
            "contract_spec": self.contract_spec,
            "status": self.status,
            "ready": self.ready,
            "block_reason": self.block_reason,
            "mapped": self.mapped,
            "continuous_input": self.continuous_input,
            "rollover_policy": self.rollover_policy,
            "days_to_contract_month": self.days_to_contract_month,
        }


class TigerContractResolver:
    """Resolve Tiger continuous/research symbols to dated tradable contracts.

    The resolver is config-driven. It does not call Tiger or CME, and it avoids
    guessing last-trading dates. The rollover guard uses the first day of the
    configured contract month as a conservative deadline.
    """

    def __init__(self, broker_config: dict | None = None) -> None:
        self.broker_config = broker_config or self._default_tiger_broker_config()

    def resolve(self, symbol: str, *, as_of: str | None = None) -> TigerContractResolution:
        requested = str(symbol or "")
        execution_symbol = self._execution_symbol(requested)
        spec = self._contract_spec(execution_symbol)
        root = str(spec.get("symbol") or self._root_symbol(execution_symbol))
        policy = self._rollover_policy()
        continuous = self._is_continuous(requested)
        mapped = execution_symbol != requested
        status = "ready"
        block_reason = ""
        days_to_contract_month = self._days_to_contract_month(spec, as_of)

        if not requested:
            status, block_reason = "blocked", "Tiger contract symbol is required"
        elif not spec:
            status, block_reason = "blocked", f"Tiger contract spec is missing for {execution_symbol}"
        elif self._is_continuous(execution_symbol):
            status, block_reason = "blocked", f"Tiger execution contract must be dated, got {execution_symbol}"
        elif not self._contract_month(spec):
            status, block_reason = "blocked", f"Tiger contract_month is missing for {execution_symbol}"
        elif days_to_contract_month is None:
            status, block_reason = "blocked", f"Tiger contract_month is invalid for {execution_symbol}"
        elif days_to_contract_month < 0:
            status, block_reason = "blocked", f"Tiger contract {execution_symbol} contract_month is in the past"
        elif days_to_contract_month < int(policy["rollover_days_before_contract_month"]):
            status = "rollover_required"
            block_reason = (
                f"Tiger contract {execution_symbol} is inside rollover window: "
                f"{days_to_contract_month} days to contract month, "
                f"threshold {policy['rollover_days_before_contract_month']}"
            )

        return TigerContractResolution(
            requested_symbol=requested,
            execution_symbol=execution_symbol,
            root_symbol=root,
            contract_spec=spec,
            status=status,
            block_reason=block_reason,
            mapped=mapped,
            continuous_input=continuous,
            rollover_policy=policy,
            days_to_contract_month=days_to_contract_month,
        )

    def write_status(self, output_root: Path, run_date: str, symbol: str, *, as_of: str | None = None) -> dict[str, Any]:
        resolution = self.resolve(symbol, as_of=as_of).to_dict()
        report = {"run_date": run_date, "provider": "tiger_openapi", "mode": "paper", "resolution": resolution, "checked_at": self._now()}
        root = Path(output_root)
        write_json(root / "tiger_contracts" / "current.json", [report])
        write_json(root / "tiger_contracts" / f"{run_date}.json", [report])
        return report

    def _execution_symbol(self, symbol: str) -> str:
        mapping = self.broker_config.get("execution_contract_map", {}) if isinstance(self.broker_config.get("execution_contract_map"), dict) else {}
        if symbol in mapping:
            return str(mapping[symbol])
        mapping = self.broker_config.get("contract_map", {}) if isinstance(self.broker_config.get("contract_map"), dict) else {}
        return str(mapping.get(symbol, symbol))

    def _contract_spec(self, symbol: str) -> dict[str, Any]:
        specs = self.broker_config.get("contract_specs", {}) if isinstance(self.broker_config.get("contract_specs"), dict) else {}
        spec = specs.get(symbol, {})
        if isinstance(spec, dict) and spec:
            return {**spec, "local_symbol": spec.get("local_symbol", symbol)}
        return {}

    def _rollover_policy(self) -> dict[str, Any]:
        policy = self.broker_config.get("rollover_policy", {}) if isinstance(self.broker_config.get("rollover_policy"), dict) else {}
        days = int(policy.get("rollover_days_before_contract_month", self.broker_config.get("rollover_days_before_contract_month", 14)))
        return {
            "rollover_days_before_contract_month": days,
            "deadline_basis": "first_day_of_contract_month",
        }

    def _days_to_contract_month(self, spec: dict[str, Any], as_of: str | None) -> int | None:
        month = self._contract_month(spec)
        if not month or len(month) != 6 or not month.isdigit():
            return None
        year = int(month[:4])
        month_num = int(month[4:])
        if month_num < 1 or month_num > 12:
            return None
        now = self._parse_as_of(as_of)
        contract_start = datetime(year, month_num, 1, tzinfo=timezone.utc)
        return (contract_start.date() - now.date()).days

    def _contract_month(self, spec: dict[str, Any]) -> str:
        return str(spec.get("contract_month") or "")

    def _parse_as_of(self, as_of: str | None) -> datetime:
        if not as_of:
            return datetime.now(timezone.utc)
        parsed = datetime.fromisoformat(str(as_of).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    def _root_symbol(self, symbol: str) -> str:
        prefix = ""
        for char in str(symbol):
            if char.isalpha() or (char.isdigit() and not prefix):
                prefix += char
            else:
                break
        return prefix or str(symbol)

    def _is_continuous(self, symbol: str) -> bool:
        return str(symbol).lower().endswith("main")

    def _default_tiger_broker_config(self) -> dict:
        config = load_pipeline_config()
        profile = (config.get("broker_profiles", {}) or {}).get("tiger_openapi_paper")
        return dict(profile) if isinstance(profile, dict) else {}

    def _now(self) -> str:
        return datetime.now(timezone.utc).replace(microsecond=0).isoformat()
