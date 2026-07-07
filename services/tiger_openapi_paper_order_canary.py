from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from services.broker_adapter import BrokerOrderRequest
from services.config_loader import ROOT, load_pipeline_config
from services.journal_store import load_json, write_json
from services.live_money_guardrails import LiveMoneyGuardrails
from services.tiger_openapi_account_sync import TigerOpenApiAccountSync
from services.tiger_openapi_broker_adapter import TigerOpenApiPaperBrokerAdapter


ACKNOWLEDGEMENT = "I_UNDERSTAND_TIGER_PAPER_TRADECLIENT_WILL_PREVIEW_AND_PLACE_AN_ORDER"
CANARY_SCHEMA_VERSION = "tiger-openapi-paper-order-canary-v1"


class TigerOpenApiPaperOrderCanary:
    """Attended Tiger paper order canary entry point.

    Default use is artifact-only. A real Tiger paper TradeClient path is reached
    only when the operator passes both explicit submit confirmation flags and a
    fixed acknowledgement phrase.
    """

    def __init__(
        self,
        output_root: Path | None = None,
        *,
        config: dict | None = None,
        adapter_factory: Callable[..., TigerOpenApiPaperBrokerAdapter] = TigerOpenApiPaperBrokerAdapter,
        trade_client: Any | None = None,
        sdk: Any | None = None,
    ) -> None:
        self.config = config if config is not None else load_pipeline_config()
        self.output_root = Path(output_root) if output_root else ROOT / str(self.config.get("output_root", "outputs"))
        self.adapter_factory = adapter_factory
        self.trade_client = trade_client
        self.sdk = sdk

    def check(
        self,
        run_date: str,
        *,
        ticket_id: str,
        asset: str,
        side: str,
        quantity: int,
        entry_price: float,
        stop_loss: float,
        take_profit: float,
        operator: str = "manual",
        notes: str = "attended Tiger paper order canary",
        use_attended_canary_risk_limits: bool = False,
    ) -> dict:
        ticket = self._ticket(
            run_date,
            ticket_id=ticket_id,
            asset=asset,
            side=side,
            entry_price=entry_price,
            stop_loss=stop_loss,
            take_profit=take_profit,
            notes=notes,
        )
        checks = [
            self._readiness_check(run_date),
            self._single_canary_check(run_date),
            self._ticket_shape_check(ticket, quantity, entry_price, stop_loss, take_profit),
            self._attended_canary_risk_package_check(
                use_attended_canary_risk_limits,
                ticket,
                quantity,
                entry_price,
                stop_loss,
            ),
            self._money_guardrail_check(run_date, ticket, quantity, entry_price, use_attended_canary_risk_limits),
        ]
        blockers = [item for item in checks if item["status"] != "pass"]
        payload = {
            "schema_version": CANARY_SCHEMA_VERSION,
            "run_date": run_date,
            "checked_at": self._now(),
            "status": "ready_for_operator_authorization" if not blockers else "blocked",
            "submit_requested": False,
            "can_submit_without_explicit_operator_authorization": False,
            "real_tiger_network_call_attempted": False,
            "provider": "tiger_openapi",
            "environment": "paper",
            "operator": operator,
            "use_attended_canary_risk_limits": use_attended_canary_risk_limits,
            "ticket": ticket,
            "actual_size": quantity,
            "latest_price": entry_price,
            "checks": checks,
            "blockers": blockers,
            "required_acknowledgement": ACKNOWLEDGEMENT,
            "submit_command": self._submit_command(
                run_date,
                ticket_id=ticket_id,
                asset=asset,
                side=side,
                quantity=quantity,
                entry_price=entry_price,
                stop_loss=stop_loss,
                take_profit=take_profit,
                operator=operator,
                use_attended_canary_risk_limits=use_attended_canary_risk_limits,
            ),
            "evidence_paths": self._evidence_paths(run_date),
        }
        self._write_payload(run_date, payload)
        return payload

    def submit(
        self,
        run_date: str,
        *,
        ticket_id: str,
        asset: str,
        side: str,
        quantity: int,
        entry_price: float,
        stop_loss: float,
        take_profit: float,
        confirm: bool = False,
        acknowledgement: str = "",
        operator: str = "manual",
        notes: str = "attended Tiger paper order canary",
        use_attended_canary_risk_limits: bool = False,
    ) -> dict:
        check = self.check(
            run_date,
            ticket_id=ticket_id,
            asset=asset,
            side=side,
            quantity=quantity,
            entry_price=entry_price,
            stop_loss=stop_loss,
            take_profit=take_profit,
            operator=operator,
            notes=notes,
            use_attended_canary_risk_limits=use_attended_canary_risk_limits,
        )
        auth_check = self._operator_authorization_check(confirm, acknowledgement, operator)
        blockers = list(check["blockers"])
        if auth_check["status"] != "pass":
            blockers.append(auth_check)
        if blockers:
            payload = {
                **check,
                "checked_at": self._now(),
                "status": "blocked_operator_authorization_missing" if auth_check["status"] != "pass" else "blocked",
                "submit_requested": True,
                "real_tiger_network_call_attempted": False,
                "checks": [*check["checks"], auth_check],
                "blockers": blockers,
                "result": {"status": "not_submitted", "reason": blockers[0]["summary"]},
            }
            self._write_payload(run_date, payload)
            return payload

        ticket = check["ticket"]
        adapter = self.adapter_factory(
            self.output_root,
            self._armed_profile(use_attended_canary_risk_limits),
            trade_client=self.trade_client,
            sdk=self.sdk,
        )
        try:
            receipt = adapter.submit_order(BrokerOrderRequest(run_date, ticket, latest_price=entry_price, actual_size=quantity))
            request_artifact = self._latest(self.output_root / "tiger_order_requests" / f"{run_date}.json")
            payload = {
                **check,
                "checked_at": self._now(),
                "status": receipt.status,
                "submit_requested": True,
                "real_tiger_network_call_attempted": self.trade_client is None and self.sdk is None,
                "checks": [*check["checks"], auth_check],
                "blockers": [],
                "result": {
                    "status": receipt.status,
                    "receipt": receipt.to_dict(),
                    "request_artifact": str(self.output_root / "tiger_order_requests" / f"{run_date}.json"),
                    "network_order_created": bool(
                        request_artifact.get("request", {}).get("network_order_created")
                        if isinstance(request_artifact.get("request"), dict)
                        else receipt.status == "submitted_to_tiger_paper"
                    ),
                },
            }
            self._write_payload(run_date, payload)
            return payload
        except Exception as exc:  # noqa: BLE001 - attended canary must persist the block reason.
            request_artifact = self._latest(self.output_root / "tiger_order_requests" / f"{run_date}.json")
            payload = {
                **check,
                "checked_at": self._now(),
                "status": "blocked_by_tiger_adapter",
                "submit_requested": True,
                "real_tiger_network_call_attempted": self.trade_client is None and self.sdk is None,
                "checks": [*check["checks"], auth_check],
                "blockers": [
                    {
                        "name": "tiger_adapter",
                        "status": "fail",
                        "summary": f"{type(exc).__name__}: {exc}",
                        "evidence": {"request_artifact": request_artifact},
                    }
                ],
                "result": {
                    "status": "not_submitted",
                    "error_type": type(exc).__name__,
                    "message": str(exc),
                    "network_order_created": bool(
                        request_artifact.get("request", {}).get("network_order_created")
                        if isinstance(request_artifact.get("request"), dict)
                        else False
                    ),
                },
            }
            self._write_payload(run_date, payload)
            return payload

    def _readiness_check(self, run_date: str) -> dict:
        readiness = self._latest(self.output_root / "tiger_paper_order_readiness" / "current.json")
        passed = (
            readiness.get("run_date") == run_date
            and readiness.get("status") == "ready_for_attended_paper_order"
            and readiness.get("ready_for_attended_paper_order") is True
            and readiness.get("can_submit_without_explicit_operator_authorization") is False
        )
        return self._check(
            "paper_order_readiness",
            passed,
            "M14 readiness artifact is ready and still requires operator authorization.",
            "M14 readiness artifact is missing, stale, or blocked.",
            readiness,
        )

    def _single_canary_check(self, run_date: str) -> dict:
        rows = load_json(self.output_root / "tiger_paper_order_canary" / f"{run_date}.json")
        submitted = [
            item
            for item in rows
            if isinstance(item, dict)
            and item.get("submit_requested") is True
            and item.get("status") == "submitted_to_tiger_paper"
        ]
        return self._check(
            "single_canary_per_day",
            not submitted,
            "No Tiger paper canary has been submitted for this run date.",
            "A Tiger paper canary has already been submitted for this run date.",
            {"submitted_count": len(submitted), "submitted": submitted[:1]},
        )

    def _ticket_shape_check(self, ticket: dict, quantity: int, entry_price: float, stop_loss: float, take_profit: float) -> dict:
        side = "buy" if self._is_buy(ticket) else "sell"
        allowed = set(str(item) for item in self._profile().get("allowed_symbols", []))
        errors = []
        if ticket["asset"] not in allowed:
            errors.append(f"asset {ticket['asset']} is not in broker profile allowed_symbols")
        if str(ticket["asset"]).lower().endswith("main"):
            errors.append("attended Tiger paper canary requires a dated execution contract, not a continuous main contract")
        if int(quantity) != quantity or quantity <= 0:
            errors.append("quantity must be a positive whole-contract integer")
        if quantity > int(self._profile().get("max_attended_canary_contracts", 1)):
            errors.append("quantity exceeds max_attended_canary_contracts")
        if side == "buy" and not (stop_loss < entry_price < take_profit):
            errors.append("buy canary requires stop_loss < entry_price < take_profit")
        if side == "sell" and not (take_profit < entry_price < stop_loss):
            errors.append("sell canary requires take_profit < entry_price < stop_loss")
        return self._check(
            "ticket_shape",
            not errors,
            "Ticket is a one-contract protected dated LIMIT order.",
            "Ticket shape is not safe for the attended Tiger paper canary.",
            {
                "errors": errors,
                "asset": ticket["asset"],
                "side": side,
                "quantity": quantity,
                "entry_price": entry_price,
                "stop_loss": stop_loss,
                "take_profit": take_profit,
                "order_type": ticket["order_type"],
                "paper_only": ticket["paper_only"],
            },
        )

    def _attended_canary_risk_package_check(
        self,
        use_attended_canary_risk_limits: bool,
        ticket: dict,
        quantity: int,
        entry_price: float,
        stop_loss: float,
    ) -> dict:
        package = self._attended_canary_package()
        multiplier = self._contract_multiplier(str(ticket["asset"]))
        stop_risk = abs(float(entry_price) - float(stop_loss)) * float(quantity) * multiplier
        equity = self._latest(self.output_root / "tiger_account_sync" / "current.json").get("exchange_balance", {}).get("balance")
        try:
            equity_value = float(equity)
        except (TypeError, ValueError):
            equity_value = 0.0
        max_loss_pct = float(package.get("max_candidate_stop_loss_pct_of_equity", 0.0) or 0.0)
        stop_risk_pct = stop_risk / equity_value * 100 if equity_value > 0 else 0.0
        evidence = {
            "requested": use_attended_canary_risk_limits,
            "enabled": package.get("enabled") is True,
            "scope": package.get("scope", "attended_tiger_paper_canary_only"),
            "candidate_stop_loss": round(stop_risk, 8),
            "candidate_stop_loss_pct_of_equity": round(stop_risk_pct, 8),
            "max_candidate_stop_loss_pct_of_equity": max_loss_pct,
            "contract_multiplier": multiplier,
            "live_money_guardrails": package.get("live_money_guardrails", {}),
        }
        if not use_attended_canary_risk_limits:
            return self._check(
                "attended_canary_risk_package",
                True,
                "Attended canary risk package was not requested; default live-money limits remain active.",
                "Attended canary risk package was not requested.",
                evidence,
            )
        if package.get("enabled") is not True:
            return self._check(
                "attended_canary_risk_package",
                False,
                "Attended canary risk package is configured.",
                "Attended canary risk package was requested but is disabled in config.",
                evidence,
            )
        if equity_value <= 0:
            return self._check(
                "attended_canary_risk_package",
                False,
                "Attended canary stop-risk budget is known.",
                "Tiger account equity is unavailable for attended canary stop-risk check.",
                evidence,
            )
        if max_loss_pct <= 0 or stop_risk_pct > max_loss_pct:
            return self._check(
                "attended_canary_risk_package",
                False,
                "Attended canary stop-risk budget allows this ticket.",
                "Ticket stop-risk exceeds attended canary risk package.",
                evidence,
            )
        return self._check(
            "attended_canary_risk_package",
            True,
            "Attended canary risk package was explicitly requested and allows this ticket.",
            "Attended canary risk package blocks this ticket.",
            evidence,
        )

    def _money_guardrail_check(
        self,
        run_date: str,
        ticket: dict,
        quantity: int,
        entry_price: float,
        use_attended_canary_risk_limits: bool,
    ) -> dict:
        reconciliation = self._latest(self.output_root / "tiger_reconciliation" / "current.json")
        account = self._latest(self.output_root / "tiger_account_sync" / "current.json")
        profile = self._armed_profile(use_attended_canary_risk_limits)
        merged = TigerOpenApiAccountSync(self.output_root, profile).merge_into_reconciliation(reconciliation, account)
        side = "BUY" if self._is_buy(ticket) else "SELL"
        result = LiveMoneyGuardrails(self.output_root, broker_config=profile).evaluate_order(
            run_date,
            ticket=ticket,
            symbol=str(ticket["asset"]),
            side=side,
            requested_price=entry_price,
            quantity=quantity,
            source="tiger_openapi:paper_canary_precheck",
            reconciliation=merged,
            persist=False,
        )
        return self._check(
            "ticket_specific_money_guardrail",
            result.get("allows_new_order") is True,
            "Ticket-specific live-money guardrail would allow this canary.",
            "Ticket-specific live-money guardrail blocks this canary before Tiger preview/place.",
            result,
        )

    def _operator_authorization_check(self, confirm: bool, acknowledgement: str, operator: str) -> dict:
        passed = confirm and acknowledgement == ACKNOWLEDGEMENT and bool(str(operator).strip())
        return self._check(
            "operator_authorization",
            passed,
            "Operator explicitly authorized the attended Tiger paper TradeClient canary.",
            "Operator authorization is missing or acknowledgement phrase does not match.",
            {
                "confirm_tiger_paper_canary": confirm,
                "acknowledgement_matches": acknowledgement == ACKNOWLEDGEMENT,
                "operator_present": bool(str(operator).strip()),
                "required_acknowledgement": ACKNOWLEDGEMENT,
            },
        )

    def _ticket(
        self,
        run_date: str,
        *,
        ticket_id: str,
        asset: str,
        side: str,
        entry_price: float,
        stop_loss: float,
        take_profit: float,
        notes: str,
    ) -> dict:
        normalized = side.lower()
        if normalized not in {"buy", "sell"}:
            raise RuntimeError(f"side must be buy or sell, got {side}")
        return {
            "ticket_id": ticket_id or f"tiger_paper_canary_{run_date.replace('-', '')}",
            "signal_id": f"sig_{ticket_id or 'tiger_paper_canary'}",
            "asset": asset,
            "asset_class": "future",
            "action": "prepare_buy" if normalized == "buy" else "prepare_sell",
            "entry_zone": f"{entry_price}-{entry_price}",
            "stop_loss": stop_loss,
            "targets": [take_profit],
            "position_size_pct": 0.1,
            "max_loss_pct": 0.1,
            "order_type": "limit",
            "time_in_force": "day",
            "paper_only": True,
            "notes": notes,
        }

    def _profile(self) -> dict:
        profile = (self.config.get("broker_profiles", {}) or {}).get("tiger_openapi_paper", {})
        if not isinstance(profile, dict) or not profile:
            raise RuntimeError("broker_profiles.tiger_openapi_paper is required")
        return dict(profile)

    def _armed_profile(self, use_attended_canary_risk_limits: bool = False) -> dict:
        profile = self._profile()
        risk_package = self._attended_canary_package()
        live_money_guardrails = {}
        if use_attended_canary_risk_limits and risk_package.get("enabled") is True:
            configured = risk_package.get("live_money_guardrails", {})
            live_money_guardrails = dict(configured) if isinstance(configured, dict) else {}
        return {
            **profile,
            "dry_run": False,
            "network_order_submission": "paper_tradeclient",
            "confirm_tiger_paper_orders": True,
            "require_reconciliation_before_entry": True,
            "require_live_money_guardrails_before_entry": True,
            "require_attached_protection_before_entry": True,
            "request_dir": "tiger_order_requests",
            **({"live_money_guardrails": live_money_guardrails} if live_money_guardrails else {}),
            "attended_canary_risk_package_requested": use_attended_canary_risk_limits,
        }

    def _attended_canary_package(self) -> dict:
        package = self._profile().get("attended_paper_canary", {})
        if not isinstance(package, dict):
            return {}
        return dict(package)

    def _contract_multiplier(self, symbol: str) -> float:
        profile = self._profile()
        specs = profile.get("contract_specs", {}) if isinstance(profile.get("contract_specs"), dict) else {}
        spec = specs.get(symbol, {}) if isinstance(specs.get(symbol, {}), dict) else {}
        try:
            return float(spec.get("multiplier") or profile.get("contract_multiplier") or 1.0)
        except (TypeError, ValueError):
            return 1.0

    def _submit_command(
        self,
        run_date: str,
        *,
        ticket_id: str,
        asset: str,
        side: str,
        quantity: int,
        entry_price: float,
        stop_loss: float,
        take_profit: float,
        operator: str,
        use_attended_canary_risk_limits: bool,
    ) -> str:
        risk_flag = " --use-attended-canary-risk-limits" if use_attended_canary_risk_limits else ""
        return (
            "python3 -m pipelines.tiger_openapi_paper_order_canary "
            f"--date {run_date} --ticket-id {ticket_id} --asset {asset} --side {side} "
            f"--quantity {quantity} --entry-price {entry_price} --stop-loss {stop_loss} "
            f"--take-profit {take_profit} --operator {operator}{risk_flag} "
            "--submit-tiger-paper-canary --confirm-tiger-paper-canary "
            f"--acknowledge-tiger-paper-network-submission {ACKNOWLEDGEMENT} --json"
        )

    def _evidence_paths(self, run_date: str) -> dict:
        return {
            "canary": str(self.output_root / "tiger_paper_order_canary" / f"{run_date}.json"),
            "current": str(self.output_root / "tiger_paper_order_canary" / "current.json"),
            "readiness": str(self.output_root / "tiger_paper_order_readiness" / "current.json"),
            "reconciliation": str(self.output_root / "tiger_reconciliation" / "current.json"),
            "account_sync": str(self.output_root / "tiger_account_sync" / "current.json"),
            "request_artifact": str(self.output_root / "tiger_order_requests" / f"{run_date}.json"),
        }

    def _write_payload(self, run_date: str, payload: dict) -> None:
        base = self.output_root / "tiger_paper_order_canary"
        write_json(base / "current.json", [payload])
        dated = base / f"{run_date}.json"
        rows = load_json(dated)
        rows.append(payload)
        write_json(dated, rows)
        self._write_markdown(run_date, payload)

    def _write_markdown(self, run_date: str, payload: dict) -> None:
        lines = [
            f"# Tiger Paper Order Canary - {run_date}",
            "",
            f"- Status: {payload['status']}",
            f"- Submit requested: {payload['submit_requested']}",
            f"- Can submit without explicit operator authorization: {payload['can_submit_without_explicit_operator_authorization']}",
            f"- Real Tiger network call attempted: {payload['real_tiger_network_call_attempted']}",
            "",
            "## Checks",
        ]
        for item in payload["checks"]:
            lines.append(f"- {item['status']}: {item['name']} - {item['summary']}")
        lines.extend(["", "## Blockers"])
        if payload["blockers"]:
            for item in payload["blockers"]:
                lines.append(f"- {item['name']}: {item['summary']}")
        else:
            lines.append("- none")
        lines.extend(["", "## Submit Command"])
        lines.append(f"- `{payload['submit_command']}`")
        path = self.output_root / "tiger_paper_order_canary" / f"{run_date}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def _latest(self, path: Path) -> dict:
        rows = load_json(path)
        if isinstance(rows, list) and rows:
            latest = rows[-1]
            return latest if isinstance(latest, dict) else {}
        return {}

    def _check(self, name: str, passed: bool, pass_summary: str, fail_summary: str, evidence: dict) -> dict:
        return {
            "name": name,
            "status": "pass" if passed else "fail",
            "summary": pass_summary if passed else fail_summary,
            "evidence": evidence,
        }

    def _is_buy(self, ticket: dict) -> bool:
        return str(ticket.get("action", "")).lower() in {"prepare_buy", "buy", "long"}

    def _now(self) -> str:
        return datetime.now(timezone.utc).replace(microsecond=0).isoformat()
