from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from services.config_loader import ROOT, load_pipeline_config
from services.journal_store import write_json
from services.live_env import apply_live_env, live_env_value_present


DEMO_BASE_URL = "https://demo-fapi.binance.com"
DEMO_SYMBOL = "XAUUSDT"


class BinanceDemoCanary:
    """Safe Binance Futures Demo canary.

    The default run validates account access and `/fapi/v1/order/test` only.
    `execute_demo=True` is required before it creates a demo exchange order.
    Even then, it is locked to Binance's demo endpoint and XAUUSDT, and it
    immediately sends a reduce-only close order after the entry.
    """

    def __init__(self, output_root: Path | None = None, opener=None, clock=None) -> None:
        config = load_pipeline_config()
        self.config = config
        self.output_root = output_root or ROOT / config.get("output_root", "outputs")
        self.broker_config = config.get("broker", {})
        self.opener = opener or urllib.request.urlopen
        self.clock = clock or (lambda: int(time.time() * 1000))

    def run(self, run_date: str, *, execute_demo: bool = False, quantity: float = 0.002) -> dict:
        checked_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        base_url = self._base_url()
        symbol = self._symbol()
        checks = [
            self._static_guard_check(base_url, symbol),
            self._credential_check(),
        ]
        if self._soft_pass(checks):
            exchange = self._exchange_info(symbol)
            checks.append(self._exchange_check(exchange))
            account = self._account_snapshot()
            checks.append(self._account_check(account))
            position_before = self._position_snapshot(symbol)
        else:
            exchange = {}
            account = {"ok": False, "status": None, "error": {"message": "static guard or credential check failed before network preflight"}}
            position_before = {"ok": False, "status": None, "error": {"message": "static guard or credential check failed before position preflight"}}
            checks.append(self._check("exchange_info", "skip", "exchange check skipped because static guard or credentials failed", {}))
            checks.append(self._check("account_access", "skip", "account check skipped because static guard or credentials failed", {}))
        order_test = self._order_test(symbol, quantity) if self._soft_pass(checks) else {"ok": False, "error": {"message": "preflight checks failed"}}
        checks.append(self._order_test_check(order_test))
        if self._soft_pass(checks):
            checks.append(self._position_check(position_before))
        else:
            checks.append(self._check("position_precheck", "skip", "position check skipped because earlier preflight checks failed", {}))
        demo_order = {}
        if execute_demo and self._soft_pass(checks):
            demo_order = self._execute_demo_round_trip(symbol, quantity)
            checks.append(self._demo_execution_check(demo_order))
        elif execute_demo:
            checks.append(self._check("demo_execution", "fail", "demo execution blocked by failed preflight checks", {}))
        else:
            checks.append(self._check("demo_execution", "skip", "validation-only mode; no demo order was created", {}))
        payload = {
            "run_date": run_date,
            "checked_at": checked_at,
            "status": self._rollup(checks),
            "mode": "execute_demo" if execute_demo else "validation_only",
            "provider": "binance_usdm",
            "base_url": base_url,
            "symbol": symbol,
            "quantity": quantity,
            "network_order_created": bool(demo_order.get("entry", {}).get("ok")),
            "network_close_created": bool(demo_order.get("close", {}).get("ok")),
            "checks": checks,
            "exchange": exchange,
            "account": self._safe_account(account),
            "position_before": self._safe_position(position_before),
            "order_test": order_test,
            "demo_order": demo_order,
            "note": "Credential values are intentionally not written to artifacts.",
        }
        write_json(self.output_root / "binance_demo_canary" / "current.json", [payload])
        write_json(self.output_root / "binance_demo_canary" / f"{run_date}.json", [payload])
        return payload

    def _static_guard_check(self, base_url: str, symbol: str) -> dict:
        if self.broker_config.get("provider") != "binance_usdm":
            return self._check("static_guard", "fail", "active broker provider is not binance_usdm", {"provider": self.broker_config.get("provider")})
        if base_url != DEMO_BASE_URL:
            return self._check("static_guard", "fail", "base_url is not Binance Futures Demo endpoint", {"base_url": base_url})
        if symbol != DEMO_SYMBOL:
            return self._check("static_guard", "fail", "canary is locked to XAUUSDT", {"symbol": symbol})
        return self._check("static_guard", "pass", "demo endpoint and XAUUSDT lock are active", {"base_url": base_url, "symbol": symbol})

    def _credential_check(self) -> dict:
        apply_live_env()
        missing = [name for name in [self._api_key_env(), self._secret_env()] if not live_env_value_present(name)]
        status = "pass" if not missing else "fail"
        summary = "Binance demo credentials are present" if not missing else f"missing Binance demo credentials: {', '.join(missing)}"
        return self._check("credentials", status, summary, {"missing_env": missing})

    def _exchange_info(self, symbol: str) -> dict:
        response = self._public_request("/fapi/v1/exchangeInfo")
        if not response.get("ok"):
            return response
        item = next((row for row in response.get("body", {}).get("symbols", []) if row.get("symbol") == symbol), {})
        filters = {row.get("filterType"): row for row in item.get("filters", []) if row.get("filterType")}
        lot = filters.get("LOT_SIZE", {})
        market_lot = filters.get("MARKET_LOT_SIZE", {})
        min_notional = filters.get("MIN_NOTIONAL", {})
        return {
            "ok": bool(item),
            "symbol": item.get("symbol", symbol),
            "status": item.get("status", "UNKNOWN"),
            "contract_type": item.get("contractType", ""),
            "margin_asset": item.get("marginAsset", ""),
            "filters": {
                "lot_min_qty": lot.get("minQty", ""),
                "lot_step_size": lot.get("stepSize", ""),
                "market_min_qty": market_lot.get("minQty") or lot.get("minQty", ""),
                "market_step_size": market_lot.get("stepSize") or lot.get("stepSize", ""),
                "min_notional": min_notional.get("notional") or min_notional.get("minNotional") or "5",
            },
        }

    def _exchange_check(self, exchange: dict) -> dict:
        if exchange.get("ok") and exchange.get("symbol") == DEMO_SYMBOL and exchange.get("status") == "TRADING":
            return self._check("exchange_info", "pass", "XAUUSDT demo contract is trading", exchange)
        return self._check("exchange_info", "fail", "XAUUSDT demo contract is not ready", exchange)

    def _account_snapshot(self) -> dict:
        return self._signed_request("GET", "/fapi/v2/account", {})

    def _account_check(self, account: dict) -> dict:
        if not account.get("ok"):
            return self._check("account_access", "fail", "cannot read Binance demo futures account", self._safe_account(account))
        available = float(account.get("body", {}).get("availableBalance", 0) or 0)
        if available <= 0:
            return self._check("account_access", "fail", "Binance demo futures account has no available balance", self._safe_account(account))
        return self._check("account_access", "pass", "Binance demo futures account is readable and funded", self._safe_account(account))

    def _position_snapshot(self, symbol: str) -> dict:
        return self._signed_request("GET", "/fapi/v2/positionRisk", {"symbol": symbol})

    def _position_check(self, position: dict) -> dict:
        safe = self._safe_position(position)
        if not position.get("ok"):
            return self._check("position_precheck", "fail", "cannot read XAUUSDT demo position before canary", safe)
        amount = float(safe.get("positionAmt", 0) or 0)
        if amount != 0:
            return self._check("position_precheck", "fail", "XAUUSDT demo position is already open; canary will not touch existing exposure", safe)
        return self._check("position_precheck", "pass", "XAUUSDT demo position is flat before canary", safe)

    def _order_test(self, symbol: str, quantity: float) -> dict:
        return self._signed_request(
            "POST",
            "/fapi/v1/order/test",
            {
                "symbol": symbol,
                "side": "BUY",
                "type": "MARKET",
                "quantity": self._format_quantity(quantity),
                "newClientOrderId": self._client_order_id("test"),
            },
        )

    def _order_test_check(self, order_test: dict) -> dict:
        if order_test.get("ok"):
            return self._check("order_test", "pass", "/fapi/v1/order/test accepted the XAUUSDT canary payload", {"status": order_test.get("status")})
        return self._check("order_test", "fail", "Binance demo order validation failed", self._safe_error(order_test))

    def _execute_demo_round_trip(self, symbol: str, quantity: float) -> dict:
        entry = self._signed_request(
            "POST",
            "/fapi/v1/order",
            {
                "symbol": symbol,
                "side": "BUY",
                "type": "MARKET",
                "quantity": self._format_quantity(quantity),
                "newOrderRespType": "RESULT",
                "newClientOrderId": self._client_order_id("entry"),
            },
        )
        close = {}
        if entry.get("ok"):
            executed_qty = self._executed_quantity(entry) or quantity
            close = self._signed_request(
                "POST",
                "/fapi/v1/order",
                {
                    "symbol": symbol,
                    "side": "SELL",
                    "type": "MARKET",
                    "quantity": self._format_quantity(executed_qty),
                    "reduceOnly": "true",
                    "newOrderRespType": "RESULT",
                    "newClientOrderId": self._client_order_id("close"),
                },
            )
        return {"entry": self._safe_order_response(entry), "close": self._safe_order_response(close)}

    def _demo_execution_check(self, demo_order: dict) -> dict:
        if demo_order.get("entry", {}).get("ok") and demo_order.get("close", {}).get("ok"):
            return self._check("demo_execution", "pass", "demo canary entry and reduce-only close both succeeded", demo_order)
        return self._check("demo_execution", "fail", "demo canary did not complete both entry and reduce-only close", demo_order)

    def _public_request(self, endpoint: str) -> dict:
        request = urllib.request.Request(f"{self._base_url()}{endpoint}", headers={"User-Agent": "TradingOrchestrator/1.0"})
        return self._open_json(request)

    def _signed_request(self, method: str, endpoint: str, params: dict) -> dict:
        apply_live_env()
        api_key = os.getenv(self._api_key_env())
        api_secret = os.getenv(self._secret_env())
        if not api_key or not api_secret:
            return {"ok": False, "status": None, "error": {"message": "missing Binance credentials"}}
        signed = {**params, "timestamp": self.clock(), "recvWindow": int(self.broker_config.get("recv_window_ms", 5000))}
        query = urllib.parse.urlencode(signed)
        signature = hmac.new(str(api_secret).encode("utf-8"), query.encode("utf-8"), hashlib.sha256).hexdigest()
        body = f"{query}&signature={signature}".encode("utf-8")
        if method.upper() == "GET":
            request = urllib.request.Request(
                f"{self._base_url()}{endpoint}?{query}&signature={signature}",
                method="GET",
                headers={"X-MBX-APIKEY": str(api_key), "User-Agent": "TradingOrchestrator/1.0"},
            )
        else:
            request = urllib.request.Request(
                f"{self._base_url()}{endpoint}",
                data=body,
                method=method.upper(),
                headers={
                    "X-MBX-APIKEY": str(api_key),
                    "Content-Type": "application/x-www-form-urlencoded",
                    "User-Agent": "TradingOrchestrator/1.0",
                },
            )
        return self._open_json(request)

    def _open_json(self, request: urllib.request.Request) -> dict:
        try:
            with self.opener(request, timeout=int(self.broker_config.get("timeout_seconds", 10))) as response:
                text = response.read().decode("utf-8")
                return {"ok": True, "status": getattr(response, "status", 200), "body": json.loads(text) if text else {}}
        except urllib.error.HTTPError as exc:
            text = exc.read().decode("utf-8", errors="replace")
            try:
                body = json.loads(text)
            except json.JSONDecodeError:
                body = {"raw": text[:500]}
            return {"ok": False, "status": exc.code, "error": body}
        except (OSError, TimeoutError, json.JSONDecodeError, ValueError) as exc:
            return {"ok": False, "status": None, "error": {"error_type": type(exc).__name__, "message": str(exc)}}

    def _safe_account(self, account: dict) -> dict:
        if not account.get("ok"):
            return self._safe_error(account)
        body = account.get("body", {})
        usdt = next((item for item in body.get("assets", []) if item.get("asset") == "USDT"), {})
        return {
            "ok": True,
            "status": account.get("status"),
            "totalWalletBalance": body.get("totalWalletBalance"),
            "availableBalance": body.get("availableBalance"),
            "USDT": {key: usdt.get(key) for key in ["walletBalance", "availableBalance", "maxWithdrawAmount"]},
        }

    def _safe_order_response(self, response: dict) -> dict:
        if not response:
            return {}
        if not response.get("ok"):
            return self._safe_error(response)
        body = response.get("body", {})
        return {
            "ok": True,
            "status": response.get("status"),
            "orderId": body.get("orderId"),
            "symbol": body.get("symbol"),
            "clientOrderId": body.get("clientOrderId"),
            "side": body.get("side"),
            "type": body.get("type"),
            "origQty": body.get("origQty"),
            "executedQty": body.get("executedQty"),
            "avgPrice": body.get("avgPrice"),
            "reduceOnly": body.get("reduceOnly"),
            "updateTime": body.get("updateTime"),
        }

    def _safe_position(self, response: dict) -> dict:
        if not response.get("ok"):
            return self._safe_error(response)
        body = response.get("body", [])
        item = body[0] if isinstance(body, list) and body else (body if isinstance(body, dict) else {})
        return {
            "ok": True,
            "status": response.get("status"),
            "symbol": item.get("symbol"),
            "positionAmt": item.get("positionAmt", "0"),
            "entryPrice": item.get("entryPrice", "0"),
            "unRealizedProfit": item.get("unRealizedProfit", "0"),
            "leverage": item.get("leverage"),
        }

    def _safe_error(self, response: dict) -> dict:
        error = response.get("error") or {}
        return {
            "ok": False,
            "status": response.get("status"),
            "error": {key: error.get(key) for key in ["code", "msg", "error_type", "message", "raw"] if error.get(key) is not None},
        }

    def _executed_quantity(self, response: dict) -> float | None:
        try:
            value = float(response.get("body", {}).get("executedQty", 0) or 0)
        except (TypeError, ValueError):
            value = 0
        return value or None

    def _base_url(self) -> str:
        return str(self.broker_config.get("base_url", "")).rstrip("/")

    def _symbol(self) -> str:
        mapping = self.broker_config.get("instrument_map", {})
        return str(mapping.get("GOLD", DEMO_SYMBOL)).upper()

    def _api_key_env(self) -> str:
        return str(self.broker_config.get("api_key_env", "BINANCE_API_KEY"))

    def _secret_env(self) -> str:
        return str(self.broker_config.get("api_secret_env", "BINANCE_API_SECRET"))

    def _client_order_id(self, suffix: str) -> str:
        return f"codex_xau_demo_{suffix}_{self.clock()}"[:36]

    def _format_quantity(self, quantity: float) -> str:
        return f"{quantity:.6f}".rstrip("0").rstrip(".")

    def _check(self, name: str, status: str, summary: str, evidence: dict) -> dict:
        return {"name": name, "status": status, "summary": summary, "evidence": evidence}

    def _soft_pass(self, checks: list[dict]) -> bool:
        return all(item["status"] in {"pass", "skip"} for item in checks)

    def _rollup(self, checks: list[dict]) -> str:
        states = {item["status"] for item in checks}
        if "fail" in states:
            return "fail"
        if "skip" in states:
            return "ready_for_demo_execution"
        return "pass"


def run_binance_demo_canary(run_date: str, *, execute_demo: bool = False, quantity: float = 0.002, output_root: Path | None = None) -> dict:
    return BinanceDemoCanary(output_root).run(run_date, execute_demo=execute_demo, quantity=quantity)
