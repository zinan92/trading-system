from __future__ import annotations

import json
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from services.binance_usdm_testnet_broker_adapter import (
    TESTNET_SYMBOL,
    BinanceUsdmTestnetBrokerAdapter,
)
from services.broker_port import BrokerOrderRequest
from services.config_loader import ROOT, load_pipeline_config
from services.journal_store import load_json, write_json
from services.live_env import apply_live_env, live_env_value_present
from services.live_reconciliation import LiveBrokerReconciliation


class BinanceUsdmTestnetCanary:
    def __init__(self, output_root: Path | None = None, opener=None) -> None:
        config = load_pipeline_config()
        self.config = config
        self.output_root = output_root or ROOT / config.get("output_root", "outputs")
        self.broker_config = self._broker_config(config)
        self.testnet_config = config.get("testnet_trading", {}) or {}
        self.opener = opener or urllib.request.urlopen

    def run(self, run_date: str, *, execute_testnet: bool = False, quantity: float = 0.002) -> dict:
        checked_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        adapter = BinanceUsdmTestnetBrokerAdapter(self.output_root, self.broker_config, self.testnet_config, opener=self.opener)
        checks = [self._static_check(adapter), self._credential_check(adapter)]
        price = self._ticker_price(adapter) if self._soft_pass(checks) else None
        checks.append(self._price_check(price))
        order_receipt = {}
        reconciliation_open = {}
        close_report = {}
        lifecycle = {}
        if execute_testnet and self._soft_pass(checks):
            ticket = self._ticket(run_date, price or 0.0, checked_at)
            order = adapter.submit_order(BrokerOrderRequest(run_date, ticket, latest_price=price, actual_size=quantity))
            order_receipt = order.to_dict()
            checks.append(self._order_check(order_receipt))
            reconciliation_open = LiveBrokerReconciliation(self.output_root, adapter.broker_config, opener=self.opener).run(run_date)
            checks.append(self._protective_check(reconciliation_open, order_receipt))
            checks.append(self._open_reconciliation_check(reconciliation_open))
            close_report = adapter.close_testnet_position(
                run_date,
                order_id=str(order_receipt.get("order_id") or ""),
                confirm_close_testnet_position=True,
            )
            checks.append(self._close_check(close_report))
            checks.append(self._final_reconciliation_check(close_report.get("reconciliation_after", {})))
            lifecycle = self._lifecycle(run_date, str(order_receipt.get("order_id") or ""))
            checks.append(self._lifecycle_check(lifecycle))
        elif execute_testnet:
            checks.append(self._check("testnet_execution", "fail", "testnet execution blocked by failed preflight checks", {}))
        else:
            checks.append(self._check("testnet_execution", "skip", "validation-only mode; no testnet order was created", {}))
        payload = {
            "run_date": run_date,
            "checked_at": checked_at,
            "status": self._rollup(checks),
            "mode": "execute_testnet" if execute_testnet else "validation_only",
            "provider": "binance_usdm",
            "base_url": adapter._binance_base_url(),
            "symbol": TESTNET_SYMBOL,
            "quantity": quantity,
            "network_order_created": bool(order_receipt.get("status") in {"filled", "protective_order_missing", "protective_order_missing_closed"}),
            "network_close_created": bool(close_report.get("network_order_created")),
            "checks": checks,
            "ticker_price": price,
            "order_receipt": order_receipt,
            "reconciliation_open": reconciliation_open,
            "close_report": close_report,
            "lifecycle": lifecycle,
            "note": "Credential values are intentionally not written to artifacts.",
        }
        write_json(self.output_root / "binance_usdm_testnet_canary" / "current.json", [payload])
        write_json(self.output_root / "binance_usdm_testnet_canary" / f"{run_date}.json", [payload])
        return payload

    def _broker_config(self, config: dict) -> dict:
        profiles = config.get("broker_profiles", {}) or {}
        profile = profiles.get("binance_usdm_testnet") or {}
        broker = config.get("broker", {}) or {}
        return {**broker, **profile}

    def _static_check(self, adapter: BinanceUsdmTestnetBrokerAdapter) -> dict:
        guard = adapter._testnet_static_guard({"exchange_status": {}})
        status = "pass" if guard["ready"] else "fail"
        summary = "Binance USDM testnet endpoint and XAUUSDT lock are active" if guard["ready"] else guard["block_reason"]
        return self._check("static_guard", status, summary, guard)

    def _credential_check(self, adapter: BinanceUsdmTestnetBrokerAdapter) -> dict:
        apply_live_env()
        missing = [
            name
            for name in [
                str(adapter.broker_config.get("api_key_env", "BINANCE_API_KEY")),
                str(adapter.broker_config.get("api_secret_env", "BINANCE_API_SECRET")),
            ]
            if not live_env_value_present(name)
        ]
        status = "pass" if not missing else "fail"
        summary = "Binance USDM testnet credentials are present" if not missing else f"missing Binance USDM testnet credentials: {', '.join(missing)}"
        return self._check("credentials", status, summary, {"missing_env": missing})

    def _ticker_price(self, adapter: BinanceUsdmTestnetBrokerAdapter) -> float | None:
        params = urllib.parse.urlencode({"symbol": TESTNET_SYMBOL})
        request = urllib.request.Request(f"{adapter._binance_base_url()}/fapi/v1/ticker/price?{params}", headers={"User-Agent": "TradingOrchestrator/1.0"})
        with self.opener(request, timeout=int(adapter.broker_config.get("timeout_seconds", 10))) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return float(payload.get("price", 0) or 0) or None

    def _ticket(self, run_date: str, price: float, checked_at: str) -> dict:
        stop_loss = price * 0.997
        take_profit = price * 1.003
        nonce = checked_at.replace("-", "").replace(":", "").replace("+", "").replace(".", "")
        return {
            "ticket_id": f"testnet_xauusdt_canary_{run_date}_{nonce}",
            "signal_id": f"testnet_canary_{run_date}_{nonce}",
            "asset": "GOLD",
            "action": "prepare_buy",
            "entry_zone": f"{price:.2f}-{price:.2f}",
            "stop_loss": round(stop_loss, 2),
            "targets": [round(take_profit, 2)],
            "position_size_pct": 0.01,
            "max_loss_pct": 0.01,
            "order_type": "market",
            "time_in_force": "day",
        }

    def _order_check(self, receipt: dict) -> dict:
        if receipt.get("status") == "filled" and float(receipt.get("quantity", 0) or 0) > 0:
            return self._check("signed_entry_order", "pass", "signed testnet entry order filled and was mirrored locally", receipt)
        return self._check("signed_entry_order", "fail", "signed testnet entry order did not fill cleanly", receipt)

    def _protective_check(self, reconciliation: dict, receipt: dict) -> dict:
        known_ids = set(reconciliation.get("known_protective_order_ids") or [])
        open_protective = [
            item
            for item in reconciliation.get("exchange_open_orders", [])
            if item.get("client_order_id") in known_ids and str(item.get("type", "")).upper() in {"STOP_MARKET", "TAKE_PROFIT_MARKET"}
        ]
        if len(open_protective) >= 2:
            return self._check("server_side_protective_orders", "pass", "testnet TP/SL are exchange-resident reduceOnly protective orders", {"open_protective_orders": open_protective})
        return self._check(
            "server_side_protective_orders",
            "fail",
            "expected exchange-resident TP/SL protective orders were not visible in reconciliation",
            {"known_ids": sorted(known_ids), "receipt": receipt, "exchange_open_orders": reconciliation.get("exchange_open_orders", [])},
        )

    def _open_reconciliation_check(self, reconciliation: dict) -> dict:
        if reconciliation.get("reconciled") and reconciliation.get("confirmation_status") == "confirmed_open":
            return self._check("reconcile_open_position", "pass", "testnet open position reconciled against exchange truth", reconciliation.get("exchange_accounting", {}))
        return self._check("reconcile_open_position", "fail", "testnet open position did not reconcile cleanly", reconciliation)

    def _close_check(self, close_report: dict) -> dict:
        if close_report.get("network_order_created") and close_report.get("status") in {"submitted", "protective_cancel_failed"}:
            return self._check("reduce_only_close", "pass", "testnet reduceOnly close order was submitted", close_report.get("close_response", {}))
        return self._check("reduce_only_close", "fail", "testnet reduceOnly close order did not submit", close_report)

    def _final_reconciliation_check(self, reconciliation: dict) -> dict:
        if reconciliation.get("reconciled") and reconciliation.get("confirmation_status") == "confirmed_flat":
            return self._check("reconcile_flat_after_close", "pass", "testnet account reconciled flat after close", reconciliation.get("exchange_accounting", {}))
        return self._check("reconcile_flat_after_close", "fail", "testnet account did not reconcile flat after close", reconciliation)

    def _lifecycle(self, run_date: str, order_id: str) -> dict:
        if not order_id:
            return {}
        rows = load_json(self.output_root / "order_lifecycle" / f"{run_date}.json")
        return next((row for row in rows if isinstance(row, dict) and row.get("order_id") == order_id), {})

    def _lifecycle_check(self, lifecycle: dict) -> dict:
        if lifecycle.get("state") == "reconciled":
            return self._check("order_lifecycle_reconciled", "pass", "same order reached lifecycle reconciled after close", {"state": lifecycle.get("state")})
        return self._check("order_lifecycle_reconciled", "fail", "same order did not reach lifecycle reconciled", lifecycle)

    def _price_check(self, price: float | None) -> dict:
        if price and price > 0:
            return self._check("venue_price", "pass", "Binance USDM testnet ticker price is readable", {"price": price})
        return self._check("venue_price", "fail", "Binance USDM testnet ticker price is not readable", {})

    def _check(self, name: str, status: str, summary: str, evidence: dict) -> dict:
        return {"name": name, "status": status, "summary": summary, "evidence": evidence}

    def _soft_pass(self, checks: list[dict]) -> bool:
        return all(item["status"] in {"pass", "skip"} for item in checks)

    def _rollup(self, checks: list[dict]) -> str:
        states = {item["status"] for item in checks}
        if "fail" in states:
            return "fail"
        if "skip" in states:
            return "ready_for_testnet_execution"
        return "pass"


def run_binance_usdm_testnet_canary(
    run_date: str,
    *,
    execute_testnet: bool = False,
    quantity: float = 0.002,
    output_root: Path | None = None,
) -> dict:
    return BinanceUsdmTestnetCanary(output_root).run(run_date, execute_testnet=execute_testnet, quantity=quantity)
