from __future__ import annotations

import hashlib
import json
import os
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from schemas.market_data import PaperOrder
from services.broker_port import (
    BrokerOrderRequest,
    BrokerPortDescriptor,
    broker_port_descriptor,
    execution_capabilities_for,
)
from services.journal_store import load_json, write_json
from services.live_env import apply_live_env, live_env_value_present


class OandaRestBrokerAdapter:
    """OANDA REST execution adapter behind the engine-neutral Broker Port."""

    name = "oanda_rest"
    provider = "oanda_rest"

    def __init__(
        self,
        output_root: Path,
        live_trading_enabled: bool,
        broker_config: dict | None = None,
        opener=None,
    ) -> None:
        self.output_root = Path(output_root)
        self.live_trading_enabled = live_trading_enabled
        self.broker_config = broker_config or {}
        self.dry_run = bool(self.broker_config.get("dry_run", True))
        self.opener = opener or urllib.request.urlopen
        self._capabilities = execution_capabilities_for(
            provider=self.provider,
            adapter_name=self.name,
        )

    @property
    def capabilities(self):
        return self._capabilities

    @property
    def descriptor(self) -> BrokerPortDescriptor:
        return broker_port_descriptor(self)

    def preflight(self) -> dict:
        env = apply_live_env()
        token_env = str(
            self.broker_config.get("api_key_env")
            or self.broker_config.get("token_env")
            or "OANDA_API_TOKEN"
        )
        account_id_env = str(
            self.broker_config.get("account_id_env", "OANDA_ACCOUNT_ID")
        )
        missing = [
            name
            for name in (token_env, account_id_env)
            if not live_env_value_present(name)
        ]
        ready = bool(
            self.live_trading_enabled and (self.dry_run or not missing)
        )
        block_reason = ""
        if not self.live_trading_enabled:
            block_reason = "live_trading_enabled is false"
        elif missing and not self.dry_run:
            block_reason = (
                f"missing OANDA environment variables: {', '.join(missing)}"
            )
        elif self.dry_run:
            block_reason = "OANDA dry_run enabled; request artifact only"
        else:
            block_reason = "OANDA REST broker ready; real orders can be submitted"
        return {
            "provider": self.provider,
            "mode": "live",
            "dry_run": self.dry_run,
            "live_trading_enabled": self.live_trading_enabled,
            "ready": ready,
            "block_reason": block_reason,
            "api_key_env": token_env,
            "account_id_env": account_id_env,
            "missing_env": missing,
            "env_file": env["path"],
            "env_file_exists": env["exists"],
            "allowed_symbols": list(self.broker_config.get("allowed_symbols", [])),
            "base_url": self._base_url(),
            "account_id_present": live_env_value_present(account_id_env),
            "instrument": self._instrument("GOLD"),
            "checked_at": datetime.now(timezone.utc)
            .replace(microsecond=0)
            .isoformat(),
        }

    def submit_order(self, request: BrokerOrderRequest) -> PaperOrder:
        if not self.live_trading_enabled:
            raise RuntimeError(
                "live trading is disabled; set live_trading_enabled only after wiring a real broker adapter"
            )
        readiness = self.preflight()
        if not readiness["ready"] and not self.dry_run:
            raise RuntimeError(
                f"live broker preflight failed: {readiness['block_reason']}"
            )
        if not self.dry_run:
            activation = self._live_activation(request.run_date)
            if activation.get("real_money_ready") is not True:
                raise RuntimeError(
                    "live activation gate is not real_money_ready; real broker submission is blocked"
                )
        return self._submit_with_readiness(request, readiness)

    def _submit_with_readiness(
        self,
        request: BrokerOrderRequest,
        readiness: dict,
    ) -> PaperOrder:
        ticket = request.ticket
        requested_price = float(
            request.latest_price or self._entry_midpoint(ticket["entry_zone"])
        )
        quantity = float(
            request.actual_size or self._quantity(ticket, requested_price)
        )
        now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        order_id = self._order_id(
            ticket["ticket_id"],
            request.run_date,
            requested_price,
        )
        payload = self._order_payload(
            ticket,
            order_id,
            requested_price,
            quantity,
        )
        if self.dry_run:
            receipt = PaperOrder(
                order_id=order_id,
                ticket_id=ticket["ticket_id"],
                status="oanda_dry_run",
                requested_price=round(requested_price, 4),
                fill_price=None,
                quantity=round(quantity, 6),
                filled_at="",
                rejection_reason=(
                    "OANDA dry-run request recorded locally; no broker order sent"
                ),
            )
            self._record_live_request(
                request,
                order_id,
                now,
                ticket,
                payload,
                readiness,
                receipt,
                broker_response={},
            )
            return receipt
        broker_response = self._post_order(payload)
        fill = broker_response.get("orderFillTransaction") or {}
        create = broker_response.get("orderCreateTransaction") or {}
        fill_price = float(fill["price"]) if fill.get("price") else None
        status = "filled" if fill_price is not None else "submitted_to_oanda"
        receipt = PaperOrder(
            order_id=order_id,
            ticket_id=ticket["ticket_id"],
            status=status,
            requested_price=round(requested_price, 4),
            fill_price=fill_price,
            quantity=round(quantity, 6),
            filled_at=fill.get("time", ""),
            rejection_reason=str(
                create.get("id")
                or broker_response.get("lastTransactionID")
                or "submitted to OANDA REST"
            ),
        )
        self._record_live_request(
            request,
            order_id,
            now,
            ticket,
            payload,
            readiness,
            receipt,
            broker_response=broker_response,
        )
        return receipt

    def _order_payload(
        self,
        ticket: dict,
        order_id: str,
        requested_price: float,
        quantity: float,
    ) -> dict:
        raw_type = str(ticket.get("order_type", "limit")).lower()
        order_type = "MARKET" if raw_type == "market" else "LIMIT"
        units = (
            quantity
            if self._is_buy_action(str(ticket.get("action", "")))
            else -quantity
        )
        order = {
            "type": order_type,
            "instrument": self._instrument(str(ticket.get("asset", "GOLD"))),
            "units": self._format_decimal(units),
            "timeInForce": self._time_in_force(
                order_type,
                str(ticket.get("time_in_force", "")),
            ),
            "positionFill": str(
                self.broker_config.get("position_fill", "DEFAULT")
            ),
            "clientExtensions": {
                "id": order_id[:128],
                "tag": "trading_orchestrator",
                "comment": str(ticket.get("ticket_id", ""))[:128],
            },
        }
        if order_type == "LIMIT":
            order["price"] = self._format_price(requested_price)
        if ticket.get("stop_loss") is not None:
            order["stopLossOnFill"] = {
                "price": self._format_price(float(ticket["stop_loss"]))
            }
        targets = ticket.get("targets") or []
        if targets:
            order["takeProfitOnFill"] = {
                "price": self._format_price(float(targets[0]))
            }
        return {"order": order}

    def _post_order(self, payload: dict) -> dict:
        apply_live_env()
        token_env = str(
            self.broker_config.get("api_key_env")
            or self.broker_config.get("token_env")
            or "OANDA_API_TOKEN"
        )
        account_id_env = str(
            self.broker_config.get("account_id_env", "OANDA_ACCOUNT_ID")
        )
        token = os.getenv(token_env)
        account_id = os.getenv(account_id_env)
        if not live_env_value_present(token_env) or not live_env_value_present(
            account_id_env
        ):
            raise RuntimeError(
                f"missing OANDA environment variables: {token_env}, {account_id_env}"
            )
        url = (
            f"{self._base_url()}/v3/accounts/"
            f"{urllib.parse.quote(account_id, safe='')}/orders"
        )
        body = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept-Datetime-Format": "RFC3339",
                "Content-Type": "application/json",
                "User-Agent": "TradingOrchestrator/1.0",
            },
        )
        with self.opener(
            request,
            timeout=int(self.broker_config.get("timeout_seconds", 10)),
        ) as response:
            return json.loads(response.read().decode("utf-8"))

    def _record_live_request(
        self,
        request: BrokerOrderRequest,
        order_id: str,
        requested_at: str,
        ticket: dict,
        order_payload: dict,
        readiness: dict,
        receipt: PaperOrder,
        broker_response: dict,
    ) -> None:
        request_dir = str(
            self.broker_config.get("request_dir", "live_order_requests")
        )
        path = self.output_root / request_dir / f"{request.run_date}.json"
        rows = [
            item for item in load_json(path) if item.get("order_id") != order_id
        ]
        rows.append(
            {
                "order_id": order_id,
                "requested_at": requested_at,
                "provider": self.provider,
                "run_date": request.run_date,
                "ticket": ticket,
                "request": order_payload,
                "readiness": readiness,
                "broker_response": broker_response,
                "receipt": receipt.to_dict(),
            }
        )
        write_json(path, rows)

    def _base_url(self) -> str:
        if self.broker_config.get("base_url"):
            return str(self.broker_config["base_url"]).rstrip("/")
        environment = str(
            self.broker_config.get("environment", "practice")
        ).lower()
        return (
            "https://api-fxtrade.oanda.com"
            if environment == "live"
            else "https://api-fxpractice.oanda.com"
        )

    def _instrument(self, asset: str) -> str:
        mapping = self.broker_config.get(
            "instrument_map",
            {"GOLD": "XAU_USD", "XAUUSD": "XAU_USD"},
        )
        return str(mapping.get(asset, asset))

    def _time_in_force(self, order_type: str, raw: str) -> str:
        value = raw.strip().upper()
        if order_type == "MARKET":
            return value if value in {"FOK", "IOC"} else "FOK"
        mapping = {"DAY": "GFD", "GTC": "GTC", "GFD": "GFD", "GTD": "GTD"}
        return mapping.get(value, "GTC")

    def _entry_midpoint(self, entry_zone: str) -> float:
        low, high = [float(part) for part in entry_zone.split("-", 1)]
        return (low + high) / 2

    def _quantity(self, ticket: dict, price: float) -> float:
        account_equity = float(
            self.broker_config.get("dry_run_account_equity", 100_000)
        )
        notional = account_equity * (
            float(ticket.get("position_size_pct", 0)) / 100
        )
        return notional / price if price else 0.0

    def _order_id(
        self,
        ticket_id: str,
        run_date: str,
        requested_price: float,
    ) -> str:
        del requested_price
        raw = f"{ticket_id}:{run_date}:{self.provider}:entry".encode("utf-8")
        return f"live_dryrun_{hashlib.sha256(raw).hexdigest()[:10]}"

    def _live_activation(self, run_date: str) -> dict:
        rows = load_json(self.output_root / "live_activation" / f"{run_date}.json")
        if not rows:
            rows = load_json(self.output_root / "live_activation" / "current.json")
        return rows[-1] if rows else {}

    def _is_buy_action(self, action: str) -> bool:
        return action.lower() in {"buy", "prepare_buy", "long", "open_long"}

    def _format_decimal(self, value: float) -> str:
        return f"{value:.6f}".rstrip("0").rstrip(".")

    def _format_price(self, value: float) -> str:
        return f"{value:.3f}".rstrip("0").rstrip(".")
