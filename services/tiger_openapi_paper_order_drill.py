from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from services.broker_adapter import BrokerOrderRequest
from services.config_loader import ROOT, load_pipeline_config
from services.journal_store import load_json, write_json
from services.tiger_openapi_broker_adapter import TigerOpenApiPaperBrokerAdapter


DEFAULT_SCENARIOS = ("default_guardrail_block", "simulated_green_order")
DRILL_PROPS_ENV = "TIGER_OPENAPI_PAPER_ORDER_DRILL_CONFIG_PATH"


class TigerOpenApiPaperOrderDrill:
    """Local Tiger paper order-path drill using fake SDK/client objects only."""

    def __init__(self, output_root: Path | None = None, run_id: str | None = None, runtime_root: Path | None = None) -> None:
        config = load_pipeline_config()
        self.config = config
        self.output_root = Path(output_root or ROOT / str(config.get("output_root", "outputs")))
        self.run_id = run_id or self._run_id()
        self.runtime_root = Path(runtime_root or self.output_root / "tiger_paper_order_drill_runtime" / self.run_id)
        self.evidence_dir = self.output_root / "tiger_paper_order_drill"

    def run(self, run_date: str, scenarios: list[str] | None = None) -> dict:
        selected = scenarios or list(DEFAULT_SCENARIOS)
        started = self._now()
        self.runtime_root.mkdir(parents=True, exist_ok=True)
        scenario_results = [self._run_named_scenario(name, run_date) for name in selected]
        payload = {
            "schema_version": "tiger-openapi-paper-order-drill-v1",
            "run_id": self.run_id,
            "run_date": run_date,
            "started_at": started,
            "finished_at": self._now(),
            "status": "pass" if all(item.get("status") == "pass" for item in scenario_results) else "fail",
            "mode": "local_fake_tradeclient",
            "provider": "tiger_openapi",
            "environment": "paper",
            "runtime_root": str(self.runtime_root),
            "real_tiger_network_call_attempted": False,
            "scenarios": scenario_results,
            "safety": {
                "real_tiger_sdk_client_created": False,
                "uses_injected_fake_trade_client": True,
                "uses_injected_fake_sdk": True,
                "checked_in_profile_dry_run": bool(self._profile().get("dry_run", True)),
                "checked_in_profile_network_mode": str(self._profile().get("network_order_submission", "")),
                "checked_in_confirm_tiger_paper_orders": bool(self._profile().get("confirm_tiger_paper_orders", False)),
                "runtime_props_are_dummy": True,
            },
            "evidence_paths": {
                "current": str(self.evidence_dir / "current.json"),
                "run_id": str(self.evidence_dir / f"{self.run_id}.json"),
                "markdown": str(self.evidence_dir / f"{self.run_id}.md"),
                "runtime_root": str(self.runtime_root),
            },
            "notes": [
                "This drill never creates a real Tiger TradeClient and never opens the real Tiger SDK network path.",
                "The simulated green order uses a small synthetic price so live-money notional limits can pass inside an isolated fake-client runtime.",
                "The default operational Tiger profile remains dry_run and fail-closed unless a human explicitly changes it.",
            ],
        }
        self._write_payload(payload)
        return payload

    def _run_named_scenario(self, name: str, run_date: str) -> dict:
        try:
            if name == "default_guardrail_block":
                return self._default_guardrail_block(run_date)
            if name == "simulated_green_order":
                return self._simulated_green_order(run_date)
            return self._scenario(name, "fail", f"unknown Tiger paper order drill scenario: {name}", {})
        except Exception as exc:  # noqa: BLE001 - drill evidence must capture failure.
            return self._scenario(name, "fail", f"{name} failed: {type(exc).__name__}: {exc}", {"error_type": type(exc).__name__, "message": str(exc)})

    def _default_guardrail_block(self, run_date: str) -> dict:
        root = self._scenario_root("default_guardrail_block")
        client = _FakeTigerTradeClient()
        previous_env = os.environ.get(DRILL_PROPS_ENV)
        try:
            adapter = TigerOpenApiPaperBrokerAdapter(root, self._armed_config(root), trade_client=client, sdk=_FakeTigerSdk())
            ticket = self._ticket(ticket_id="ticket_tiger_drill_guardrail", entry_zone="4180-4190", stop_loss=4170.0, targets=[4200.0])
            try:
                adapter.submit_order(BrokerOrderRequest(run_date, ticket, latest_price=4186.0, actual_size=1))
                return self._scenario("default_guardrail_block", "fail", "default MGC notional unexpectedly passed live-money guardrails", {"calls": client.call_names()})
            except RuntimeError as exc:
                request = self._latest_request(root, run_date)
                guardrails = request.get("readiness", {}).get("live_money_guardrails", {}) if isinstance(request.get("readiness"), dict) else {}
                no_preview_place = not any(name in {"preview_order", "place_order"} for name in client.call_names())
                passed = (
                    request.get("receipt", {}).get("status") == "blocked"
                    and guardrails.get("status") == "BLOCKED_SINGLE_ORDER_NOTIONAL_LIMIT"
                    and no_preview_place
                )
                return self._scenario(
                    "default_guardrail_block",
                    "pass" if passed else "fail",
                    "default MGC one-contract notional blocked before fake preview/place" if passed else "guardrail block did not match expected invariant",
                    {
                        "error": str(exc),
                        "call_sequence": client.call_names(),
                        "no_preview_or_place": no_preview_place,
                        "request_artifact": str(root / "tiger_order_requests" / f"{run_date}.json"),
                        "receipt_status": request.get("receipt", {}).get("status"),
                        "guardrail_status": guardrails.get("status"),
                        "primary_blocker": guardrails.get("primary_blocker", {}),
                        "network_order_created": request.get("request", {}).get("network_order_created"),
                    },
                )
        finally:
            self._restore_props_env(previous_env)

    def _simulated_green_order(self, run_date: str) -> dict:
        root = self._scenario_root("simulated_green_order")
        client = _FakeTigerTradeClient()
        previous_env = os.environ.get(DRILL_PROPS_ENV)
        try:
            adapter = TigerOpenApiPaperBrokerAdapter(root, self._armed_config(root), trade_client=client, sdk=_FakeTigerSdk())
            ticket = self._ticket(ticket_id="ticket_tiger_drill_green", entry_zone="0.4-0.6", stop_loss=0.45, targets=[0.6])
            order = adapter.submit_order(BrokerOrderRequest(run_date, ticket, latest_price=0.5, actual_size=1))
            request = self._latest_request(root, run_date)
            lifecycle = self._latest_lifecycle(root, run_date)
            guardrails = request.get("readiness", {}).get("live_money_guardrails", {}) if isinstance(request.get("readiness"), dict) else {}
            protective = request.get("readiness", {}).get("protective_order_precheck", {}) if isinstance(request.get("readiness"), dict) else {}
            passed = (
                order.status == "submitted_to_tiger_paper"
                and client.call_names() == ["get_positions", "get_open_orders", "get_prime_assets", "preview_order", "place_order"]
                and request.get("request", {}).get("protective_status") == "attached_in_parent_order"
                and request.get("request", {}).get("protective_leg_count") == 2
                and guardrails.get("status") == "READY"
                and protective.get("ready") is True
                and lifecycle.get("state") == "accepted"
            )
            return self._scenario(
                "simulated_green_order",
                "pass" if passed else "fail",
                "fake TradeClient accepted a fully protected Tiger paper order path" if passed else "fake TradeClient order path did not satisfy all assertions",
                {
                    "call_sequence": client.call_names(),
                    "receipt": order.to_dict(),
                    "request_artifact": str(root / "tiger_order_requests" / f"{run_date}.json"),
                    "lifecycle_artifact": str(root / "order_lifecycle" / f"{run_date}.json"),
                    "guardrail_status": guardrails.get("status"),
                    "protective_order_precheck": protective,
                    "protective_status": request.get("request", {}).get("protective_status"),
                    "protective_leg_count": request.get("request", {}).get("protective_leg_count"),
                    "fake_broker_response": request.get("broker_response", {}),
                    "lifecycle_state": lifecycle.get("state"),
                    "real_tiger_network_call_attempted": False,
                },
            )
        finally:
            self._restore_props_env(previous_env)

    def _armed_config(self, root: Path) -> dict:
        props = root / "dummy_tiger_openapi_config.properties"
        props.parent.mkdir(parents=True, exist_ok=True)
        props.write_text("tiger_id=local-drill-placeholder\n", encoding="utf-8")
        props.chmod(0o600)
        os.environ[DRILL_PROPS_ENV] = str(props)
        profile = self._profile()
        return {
            **profile,
            "dry_run": False,
            "props_path_env": DRILL_PROPS_ENV,
            "network_order_submission": "paper_tradeclient",
            "confirm_tiger_paper_orders": True,
            "require_reconciliation_before_entry": True,
            "require_live_money_guardrails_before_entry": True,
            "require_attached_protection_before_entry": True,
            "request_dir": "tiger_order_requests",
        }

    def _restore_props_env(self, previous_env: str | None) -> None:
        if previous_env is None:
            os.environ.pop(DRILL_PROPS_ENV, None)
        else:
            os.environ[DRILL_PROPS_ENV] = previous_env

    def _ticket(self, *, ticket_id: str, entry_zone: str, stop_loss: float, targets: list[float]) -> dict:
        return {
            "ticket_id": ticket_id,
            "signal_id": f"{ticket_id}_signal",
            "asset": "MGC2608",
            "asset_class": "future",
            "action": "prepare_buy",
            "entry_zone": entry_zone,
            "stop_loss": stop_loss,
            "targets": targets,
            "position_size_pct": 8,
            "max_loss_pct": 0.5,
            "order_type": "limit",
            "time_in_force": "day",
            "paper_only": True,
        }

    def _profile(self) -> dict:
        profile = (self.config.get("broker_profiles", {}) or {}).get("tiger_openapi_paper", {})
        if not isinstance(profile, dict) or not profile:
            raise RuntimeError("broker_profiles.tiger_openapi_paper is required for Tiger paper order drill")
        return dict(profile)

    def _scenario_root(self, name: str) -> Path:
        path = self.runtime_root / name
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _latest_request(self, root: Path, run_date: str) -> dict:
        rows = load_json(root / "tiger_order_requests" / f"{run_date}.json")
        return rows[-1] if rows and isinstance(rows[-1], dict) else {}

    def _latest_lifecycle(self, root: Path, run_date: str) -> dict:
        rows = load_json(root / "order_lifecycle" / f"{run_date}.json")
        return rows[-1] if rows and isinstance(rows[-1], dict) else {}

    def _scenario(self, name: str, status: str, summary: str, evidence: dict) -> dict:
        return {"name": name, "status": status, "summary": summary, "evidence": evidence}

    def _write_payload(self, payload: dict) -> None:
        write_json(self.evidence_dir / "current.json", [payload])
        write_json(self.evidence_dir / f"{self.run_id}.json", [payload])
        self._write_markdown(payload)

    def _write_markdown(self, payload: dict) -> None:
        lines = [
            f"# Tiger Paper Order Drill - {payload['run_date']}",
            "",
            f"- Status: {payload['status']}",
            f"- Mode: {payload['mode']}",
            f"- Real Tiger network call attempted: {payload['real_tiger_network_call_attempted']}",
            f"- Runtime root: `{payload['runtime_root']}`",
            "",
            "## Scenarios",
        ]
        for item in payload["scenarios"]:
            lines.append(f"- {item['status']}: {item['name']} - {item['summary']}")
        lines.extend(["", "## Safety"])
        for key, value in payload["safety"].items():
            lines.append(f"- {key}: {value}")
        path = self.evidence_dir / f"{self.run_id}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def _run_id(self) -> str:
        return "tigerdrill-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    def _now(self) -> str:
        return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class _FakeTigerSdk:
    def future_contract(self, symbol, currency, **kwargs):
        return {"symbol": symbol, "currency": currency, **kwargs}

    def order_leg(self, leg_type, price=None, time_in_force="DAY", **kwargs):
        return {"leg_type": leg_type, "price": price, "time_in_force": time_in_force, **kwargs}

    def limit_order_with_legs(self, account, contract, action, quantity, limit_price, order_legs=None, time_in_force="DAY"):
        return SimpleNamespace(
            account=account,
            contract=contract,
            action=action,
            order_type="LMT",
            quantity=quantity,
            limit_price=limit_price,
            order_legs=order_legs or [],
            time_in_force=time_in_force,
        )

    def limit_order(self, account, contract, action, quantity, limit_price, time_in_force="DAY"):
        return SimpleNamespace(account=account, contract=contract, action=action, order_type="LMT", quantity=quantity, limit_price=limit_price, order_legs=[], time_in_force=time_in_force)

    def market_order(self, account, contract, action, quantity, time_in_force="DAY"):
        return SimpleNamespace(account=account, contract=contract, action=action, order_type="MKT", quantity=quantity, time_in_force=time_in_force)

    def stop_order(self, account, contract, action, quantity, aux_price, time_in_force="DAY"):
        return SimpleNamespace(account=account, contract=contract, action=action, order_type="STP", quantity=quantity, aux_price=aux_price, time_in_force=time_in_force)

    def stop_limit_order(self, account, contract, action, quantity, limit_price, aux_price, time_in_force="DAY"):
        return SimpleNamespace(account=account, contract=contract, action=action, order_type="STP_LMT", quantity=quantity, limit_price=limit_price, aux_price=aux_price, time_in_force=time_in_force)


class _FakeTigerTradeClient:
    def __init__(self) -> None:
        self._account = "LOCAL_DRILL_ACCOUNT"
        self.calls: list[tuple[str, object]] = []

    def get_positions(self, **kwargs):
        self.calls.append(("get_positions", kwargs))
        return []

    def get_open_orders(self, **kwargs):
        self.calls.append(("get_open_orders", kwargs))
        return []

    def get_prime_assets(self, **kwargs):
        self.calls.append(("get_prime_assets", kwargs))
        return SimpleNamespace(
            segments={
                "C": SimpleNamespace(
                    currency="USD",
                    net_liquidation=25000.0,
                    cash_available_for_trade=25000.0,
                    realized_pl=0.0,
                    unrealized_pl=0.0,
                )
            }
        )

    def preview_order(self, order):
        self.calls.append(("preview_order", order))
        return {"ok": True, "margin_change": 2300, "source": "local_fake_tradeclient"}

    def place_order(self, order):
        self.calls.append(("place_order", order))
        order.id = 987654321
        order.sub_ids = [987654322, 987654323]
        order.orders = [{"id": 987654321, "source": "local_fake_tradeclient"}]
        return 987654321

    def call_names(self) -> list[str]:
        return [name for name, _ in self.calls]
