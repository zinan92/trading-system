from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from services.config_loader import load_pipeline_config, load_risk_rules
from services.journal_store import JournalStore, load_json, write_json


MAINNET_CANARY_VERSION = "mainnet-canary-v1"
MAINNET_BASE_URL = "https://fapi.binance.com"
PLACEHOLDER_SINGLE_NOTIONAL = 25.0
PLACEHOLDER_TOTAL_NOTIONAL = 25.0
PLACEHOLDER_REFERENCE_EQUITY = 5000.0


class MainnetCanary:
    """Attended mainnet canary entry point: hard go/no-go check, then exactly
    one explicitly-confirmed minimum-size order through the existing journal
    'executed' path (which enforces risk limits, live preflight, activation,
    inline reconciliation, and money guardrails).

    Read-only by default. Refuses to record a journal decision unless every
    gate is green — never a silent 'executed' row without a broker order.
    """

    def __init__(self, output_root: Path, config: dict | None = None, risk_rules: dict | None = None) -> None:
        self.output_root = Path(output_root)
        self.config = config if config is not None else load_pipeline_config()
        self.risk_rules = risk_rules if risk_rules is not None else load_risk_rules()

    def check(self, run_date: str) -> dict:
        blockers: list[str] = []
        broker = self.config.get("broker", {}) if isinstance(self.config.get("broker"), dict) else {}
        mode = str(self.config.get("execution_mode", "paper")).lower()
        if mode != "live":
            blockers.append(f"execution_mode is '{mode}', must be 'live'")
        if not bool(self.config.get("live_trading_enabled", False)):
            blockers.append("live_trading_enabled is false")
        if bool(broker.get("dry_run", True)):
            blockers.append("broker.dry_run is true; no real order would be sent")
        if str(broker.get("environment", "")).lower() != "live":
            blockers.append(f"broker.environment is '{broker.get('environment', '')}', must be 'live'")
        if str(broker.get("base_url", "")) != MAINNET_BASE_URL:
            blockers.append(f"broker.base_url is '{broker.get('base_url', '')}', must be {MAINNET_BASE_URL}")

        activation = self._latest(self.output_root / "live_activation" / f"{run_date}.json") or self._latest(
            self.output_root / "live_activation" / "current.json"
        )
        if not activation or activation.get("real_money_ready") is not True:
            blockers.append(
                "live_activation is not real_money_ready — re-run python3 -m pipelines.live_activation AFTER flipping the live gates"
            )
        drill = self._latest(self.output_root / "testnet_drill" / "current.json")
        if not drill or str(drill.get("status", "")) != "pass":
            blockers.append("testnet_drill evidence missing or not pass — run python3 -m pipelines.testnet_drill --execute-testnet first")
        approval = self._latest(self.output_root / "live_approvals" / f"{run_date}.approved.json")
        if not approval:
            blockers.append(f"human approval artifact missing: outputs/live_approvals/{run_date}.approved.json")

        live_money = self._live_money_rules()
        if (
            self._float(live_money.get("single_order_max_notional")) == PLACEHOLDER_SINGLE_NOTIONAL
            or self._float(live_money.get("total_account_max_notional")) == PLACEHOLDER_TOTAL_NOTIONAL
            or self._float(live_money.get("reference_equity")) == PLACEHOLDER_REFERENCE_EQUITY
        ):
            blockers.append(
                "risk_rules live_money still holds placeholder TESTNET limits (25.0 notional / 5000 reference_equity); "
                "recalibrate from the real canary bankroll"
            )

        return {
            "schema_version": MAINNET_CANARY_VERSION,
            "run_date": run_date,
            "checked_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "go": not blockers,
            "blockers": blockers,
        }

    def submit(
        self,
        run_date: str,
        *,
        side: str,
        quantity: float,
        stop_loss: float,
        take_profit: float,
        entry_price: float,
        confirm: bool = False,
        notes: str = "attended mainnet canary",
    ) -> dict:
        if not confirm:
            raise RuntimeError("mainnet canary refused: pass --confirm-mainnet-canary (confirm=True) to submit a real order")
        report = self.check(run_date)
        if not report["go"]:
            raise RuntimeError("mainnet canary NO-GO: " + "; ".join(report["blockers"]))
        self._enforce_single_canary(run_date)
        ticket = self._build_ticket(run_date, side=side, quantity=quantity, stop_loss=stop_loss, take_profit=take_profit, entry_price=entry_price)

        tickets_path = self.output_root / "trade_tickets" / f"{run_date}.json"
        tickets = [item for item in load_json(tickets_path) if item.get("ticket_id") != ticket["ticket_id"]]
        tickets.append(ticket)
        write_json(tickets_path, tickets)

        pending_path = self.output_root / "journal_pending" / f"{run_date}.json"
        pending = [item for item in load_json(pending_path) if item.get("ticket_id") != ticket["ticket_id"]]
        pending.append(
            {
                "ticket_id": ticket["ticket_id"],
                "signal_id": ticket["signal_id"],
                "asset": ticket["asset"],
                "source": "mainnet_canary",
                "created_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            }
        )
        write_json(pending_path, pending)

        decision = JournalStore(self.output_root).record_decision(
            run_date,
            ticket["ticket_id"],
            "executed",
            notes=notes,
            actual_entry=entry_price,
            actual_size=quantity,
        )
        broker_order = decision.get("broker_order") or {}
        if not broker_order:
            raise RuntimeError(
                "mainnet canary aborted: journal recorded 'executed' but no broker order was submitted — "
                "execution_mode must be 'live' in the loaded pipeline config"
            )
        result = {"status": "submitted", "ticket_id": ticket["ticket_id"], "broker_order": broker_order}
        record = {"schema_version": MAINNET_CANARY_VERSION, "check": report, "ticket": ticket, "result": result}
        write_json(self.output_root / "mainnet_canary" / f"{run_date}.json", [record])
        write_json(self.output_root / "mainnet_canary" / "current.json", [record])
        return result

    def _enforce_single_canary(self, run_date: str) -> None:
        decisions = load_json(self.output_root / "journal_decisions" / f"{run_date}.json")
        executed = [item for item in decisions if item.get("decision_status") == "executed"]
        if executed:
            raise RuntimeError(
                f"exactly one canary order per day: an executed decision already exists ({executed[0].get('ticket_id')})"
            )

    def _build_ticket(
        self, run_date: str, *, side: str, quantity: float, stop_loss: float, take_profit: float, entry_price: float
    ) -> dict:
        normalized = str(side).lower()
        if normalized not in {"buy", "sell"}:
            raise RuntimeError(f"side must be buy or sell, got '{side}'")
        if not quantity or quantity <= 0:
            raise RuntimeError("quantity must be a positive minimum-size value")
        if normalized == "buy" and not (stop_loss < entry_price < take_profit):
            raise RuntimeError("buy canary requires stop_loss < entry_price < take_profit")
        if normalized == "sell" and not (take_profit < entry_price < stop_loss):
            raise RuntimeError("sell canary requires take_profit < entry_price < stop_loss")
        compact = run_date.replace("-", "")
        return {
            "ticket_id": f"mainnet_canary_{compact}",
            "signal_id": f"sig_mainnet_canary_{compact}",
            "asset": "GOLD",
            "asset_class": "commodity",
            "action": "prepare_buy" if normalized == "buy" else "prepare_sell",
            "entry_zone": f"{entry_price}-{entry_price}",
            "stop_loss": stop_loss,
            "targets": [take_profit],
            "position_size_pct": 0.1,
            "max_loss_pct": 0.1,
            "order_type": "market",
            "time_in_force": "day",
            "paper_only": False,
            "notes": "attended mainnet canary: exactly one minimum-size order",
        }

    def _latest(self, path: Path) -> dict:
        rows = load_json(path)
        if isinstance(rows, list) and rows:
            return rows[-1] if isinstance(rows[-1], dict) else {}
        return {}

    def _live_money_rules(self) -> dict:
        legacy = self.risk_rules.get("live_money")
        if isinstance(legacy, dict) and legacy:
            return legacy
        default = self.risk_rules.get("default")
        if not isinstance(default, dict):
            return {}
        rules = default.get("live_money_guardrails")
        return rules if isinstance(rules, dict) else {}

    def _float(self, value: Any) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return float("nan")
