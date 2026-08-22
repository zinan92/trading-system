from __future__ import annotations

import hashlib
import os
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from schemas.market_data import PaperOrder
from services.binance_usdm_broker_adapter import BinanceUsdmBrokerAdapter
from services.broker_port import (
    BrokerCancelRequest,
    BrokerCapability,
    BrokerExecutionPort,
    BrokerOrderRequest,
    BrokerPortDescriptor,
    BrokerProtectiveRecoveryRequest,
    broker_port_descriptor,
    execution_capabilities_for,
    require_broker_capability,
)
from services.config_loader import ROOT, load_pipeline_config
from services.journal_store import load_json, write_json
from services.live_env import apply_live_env, live_env_value_present
from services.live_activation_gate import LiveActivationGate as SourceBoundLiveActivationGate
from services.paper_executor import PaperExecutor

BrokerAdapter = BrokerExecutionPort


class PaperBrokerAdapter:
    name = "paper"
    provider = "paper"

    def __init__(self, output_root: Path) -> None:
        self.output_root = Path(output_root)
        self.broker_config = {
            "provider": "paper",
            "environment": "paper",
            "dry_run": True,
        }
        self._capabilities = execution_capabilities_for(
            provider=self.provider,
            adapter_name=self.name,
        )
        self.executor = PaperExecutor(output_root)

    @property
    def capabilities(self):
        return self._capabilities

    @property
    def descriptor(self) -> BrokerPortDescriptor:
        return broker_port_descriptor(self)

    def preflight(self) -> dict:
        return {
            "provider": self.provider,
            "mode": "paper",
            "dry_run": True,
            "live_trading_enabled": False,
            "ready": True,
            "block_reason": "paper mode active; live broker is not used",
            "allowed_symbols": [],
            "checked_at": datetime.now(timezone.utc)
            .replace(microsecond=0)
            .isoformat(),
        }

    def submit_order(self, request: BrokerOrderRequest) -> PaperOrder:
        return self.executor.execute_ticket(
            run_date=request.run_date,
            ticket=request.ticket,
            latest_price=request.latest_price,
            actual_size=request.actual_size,
        )


class LiveBrokerAdapter:
    """Legacy multi-provider facade retained for direct-call compatibility."""

    name = "live"

    def __init__(
        self,
        output_root: Path,
        live_trading_enabled: bool,
        broker_config: dict | None = None,
        opener=None,
    ) -> None:
        self.output_root = output_root
        self.live_trading_enabled = live_trading_enabled
        self.broker_config = broker_config or {}
        self.provider = str(
            self.broker_config.get("provider", "manual_gateway")
        )
        self.dry_run = bool(self.broker_config.get("dry_run", True))
        self.opener = opener or urllib.request.urlopen
        self._capabilities = execution_capabilities_for(
            provider=self.provider,
            adapter_name=self.name,
        )
        self._binance_adapter_instance = None
        self._oanda_adapter_instance = None
        self._mt5_adapter_instance = None
        self._tiger_adapter_instance = None

    @property
    def capabilities(self):
        return self._capabilities

    @property
    def descriptor(self) -> BrokerPortDescriptor:
        return broker_port_descriptor(self)

    def submit_order(self, request: BrokerOrderRequest) -> PaperOrder:
        if self.provider == "binance_usdm":
            return self._binance_adapter().submit_order(request)
        if not self.live_trading_enabled:
            raise RuntimeError(
                "live trading is disabled; set live_trading_enabled only "
                "after wiring a real broker adapter"
            )
        readiness = self.preflight()
        if not readiness["ready"] and not self.dry_run:
            raise RuntimeError(
                f"live broker preflight failed: {readiness['block_reason']}"
            )
        if not self.dry_run:
            activation = SourceBoundLiveActivationGate(self.output_root, park_user_id=os.getenv("PARK_TELEGRAM_USER_ID", "")).canary_status()
            if activation.get("ready") is not True:
                raise RuntimeError(
                    "source-bound Live activation/canary is not ready; "
                    "real broker submission is blocked"
                )
        if self.provider == "mt5_file_bridge":
            return self._record_mt5_file_bridge_request(request, readiness)
        if self.provider == "oanda_rest":
            return self._submit_oanda_order(request, readiness)
        if self.provider == "tiger_openapi":
            if not self.dry_run:
                raise NotImplementedError(
                    "Tiger OpenAPI network order submission is not implemented; "
                    "adapter is fail-closed"
                )
            return self._submit_tiger_order(request, readiness)
        if not self.dry_run:
            raise NotImplementedError(
                f"live broker provider {self.provider} is not wired for real "
                "order submission yet"
            )
        return self._record_dry_run_request(request, readiness)

    def preflight(self) -> dict:
        if self.provider == "mt5_file_bridge":
            return self._mt5_file_bridge_preflight()
        if self.provider == "oanda_rest":
            return self._oanda_preflight()
        if self.provider == "binance_usdm":
            return self._binance_adapter().preflight()
        if self.provider == "tiger_openapi":
            return self._tiger_preflight()
        env = apply_live_env()
        api_key_env = str(
            self.broker_config.get("api_key_env", "BROKER_API_KEY")
        )
        account_id_env = str(
            self.broker_config.get("account_id_env", "BROKER_ACCOUNT_ID")
        )
        missing = [
            name
            for name in (api_key_env, account_id_env)
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
                f"missing broker environment variables: {', '.join(missing)}"
            )
        elif self.dry_run:
            block_reason = "dry_run enabled; no real broker order will be sent"
        return {
            "provider": self.provider,
            "mode": "live",
            "dry_run": self.dry_run,
            "live_trading_enabled": self.live_trading_enabled,
            "ready": ready,
            "block_reason": block_reason,
            "api_key_env": api_key_env,
            "account_id_env": account_id_env,
            "missing_env": missing,
            "env_file": env["path"],
            "env_file_exists": env["exists"],
            "allowed_symbols": list(
                self.broker_config.get("allowed_symbols", [])
            ),
            "checked_at": datetime.now(timezone.utc)
            .replace(microsecond=0)
            .isoformat(),
        }

    def cancel_binance_order(
        self,
        symbol: str,
        *,
        orig_client_order_id: str = "",
        order_id: str = "",
    ) -> dict:
        require_broker_capability(self, BrokerCapability.CANCEL_ORDER)
        return self._binance_adapter().cancel_binance_order(
            symbol,
            orig_client_order_id=orig_client_order_id,
            order_id=order_id,
        )

    def cancel_order(self, request: BrokerCancelRequest) -> dict:
        require_broker_capability(self, BrokerCapability.CANCEL_ORDER)
        return self.cancel_binance_order(
            self._binance_adapter()._binance_symbol(request.asset),
            orig_client_order_id=request.client_order_id,
            order_id=request.broker_order_id,
        )

    def recover_missing_protective_orders(
        self,
        run_date: str,
        lifecycle_record: dict,
        exchange_position: dict | None = None,
        *,
        source: str = "order_recovery",
    ) -> dict:
        require_broker_capability(self, BrokerCapability.PROTECTIVE_RECOVERY)
        return self._binance_adapter().recover_missing_protective_orders(
            run_date,
            lifecycle_record,
            exchange_position,
            source=source,
        )

    def recover_protective_orders(
        self,
        request: BrokerProtectiveRecoveryRequest,
    ) -> dict:
        require_broker_capability(self, BrokerCapability.PROTECTIVE_RECOVERY)
        return self.recover_missing_protective_orders(
            request.run_date,
            request.lifecycle_record,
            request.exchange_position,
            source=request.source,
        )

    def _record_dry_run_request(
        self,
        request: BrokerOrderRequest,
        readiness: dict,
    ) -> PaperOrder:
        ticket = request.ticket
        requested_price = float(
            request.latest_price
            or self._entry_midpoint(ticket["entry_zone"])
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
        receipt = PaperOrder(
            order_id=order_id,
            ticket_id=ticket["ticket_id"],
            status="dry_run",
            requested_price=round(requested_price, 4),
            fill_price=None,
            quantity=round(quantity, 6),
            filled_at="",
            rejection_reason=(
                "live dry-run request recorded locally; no broker order sent"
            ),
        )
        payload = {
            "order_id": order_id,
            "requested_at": now,
            "provider": self.provider,
            "run_date": request.run_date,
            "ticket": ticket,
            "request": {
                "symbol": ticket.get("asset"),
                "action": ticket.get("action"),
                "order_type": ticket.get("order_type", "limit"),
                "time_in_force": ticket.get("time_in_force", "day"),
                "requested_price": receipt.requested_price,
                "quantity": receipt.quantity,
                "stop_loss": ticket.get("stop_loss"),
                "targets": ticket.get("targets", []),
            },
            "readiness": readiness,
            "receipt": receipt.to_dict(),
        }
        request_dir = str(
            self.broker_config.get("request_dir", "live_order_requests")
        )
        path = self.output_root / request_dir / f"{request.run_date}.json"
        rows = [
            item
            for item in load_json(path)
            if item.get("order_id") != order_id
        ]
        rows.append(payload)
        write_json(path, rows)
        return receipt

    def _tiger_preflight(self) -> dict:
        return self._tiger_adapter()._tiger_preflight()

    def _submit_tiger_order(
        self,
        request: BrokerOrderRequest,
        readiness: dict,
    ) -> PaperOrder:
        return self._tiger_adapter()._submit_tiger_order(request, readiness)

    def _tiger_contract_quantity(self, raw_quantity: float) -> int:
        return self._tiger_adapter()._tiger_contract_quantity(raw_quantity)

    def _tiger_order_payload(
        self,
        ticket: dict,
        order_id: str,
        requested_price: float,
        quantity: int,
    ) -> dict:
        return self._tiger_adapter()._tiger_order_payload(
            ticket,
            order_id,
            requested_price,
            quantity,
        )

    def _oanda_preflight(self) -> dict:
        return self._oanda_adapter().preflight()

    def _mt5_file_bridge_preflight(self) -> dict:
        return self._mt5_adapter().preflight()

    def _record_mt5_file_bridge_request(
        self,
        request: BrokerOrderRequest,
        readiness: dict,
    ) -> PaperOrder:
        return self._mt5_adapter()._submit_with_readiness(request, readiness)

    def _submit_oanda_order(
        self,
        request: BrokerOrderRequest,
        readiness: dict,
    ) -> PaperOrder:
        return self._oanda_adapter()._submit_with_readiness(request, readiness)

    def _oanda_order_payload(
        self,
        ticket: dict,
        order_id: str,
        requested_price: float,
        quantity: float,
    ) -> dict:
        return self._oanda_adapter()._order_payload(
            ticket,
            order_id,
            requested_price,
            quantity,
        )

    def _post_oanda_order(self, payload: dict) -> dict:
        return self._oanda_adapter()._post_order(payload)

    def _oanda_base_url(self) -> str:
        return self._oanda_adapter()._base_url()

    def _oanda_instrument(self, asset: str) -> str:
        return self._oanda_adapter()._instrument(asset)

    def _oanda_time_in_force(self, order_type: str, raw: str) -> str:
        return self._oanda_adapter()._time_in_force(order_type, raw)

    def _format_price(self, value: float) -> str:
        return self._oanda_adapter()._format_price(value)

    def _mt5_outbox_dir(self) -> Path:
        return self._mt5_adapter()._outbox_dir()

    def _mt5_inbox_dir(self) -> Path:
        return self._mt5_adapter()._inbox_dir()

    def _ensure_mt5_bridge_docs(
        self,
        outbox_dir: Path,
        inbox_dir: Path,
    ) -> dict:
        return self._mt5_adapter()._ensure_bridge_docs(outbox_dir, inbox_dir)

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
        rows = load_json(
            self.output_root / "live_activation" / f"{run_date}.json"
        )
        if not rows:
            rows = load_json(
                self.output_root / "live_activation" / "current.json"
            )
        return rows[-1] if rows else {}

    def _binance_adapter(self) -> BinanceUsdmBrokerAdapter:
        adapter = self._binance_adapter_instance
        if (
            adapter is None
            or adapter.broker_config is not self.broker_config
            or adapter.opener is not self.opener
            or adapter.live_trading_enabled != self.live_trading_enabled
            or adapter.dry_run != self.dry_run
        ):
            adapter = BinanceUsdmBrokerAdapter(
                self.output_root,
                self.live_trading_enabled,
                self.broker_config,
                opener=self.opener,
            )
            self._binance_adapter_instance = adapter
        for name, value in self.__dict__.items():
            if callable(value) and hasattr(BinanceUsdmBrokerAdapter, name):
                setattr(adapter, name, value)
        return adapter

    def __getattr__(self, name: str):
        if hasattr(BinanceUsdmBrokerAdapter, name):
            return getattr(self._binance_adapter(), name)
        raise AttributeError(
            f"{type(self).__name__!s} object has no attribute {name!r}"
        )

    def _oanda_adapter(self):
        adapter = self._oanda_adapter_instance
        if (
            adapter is None
            or adapter.broker_config is not self.broker_config
            or adapter.opener is not self.opener
            or adapter.live_trading_enabled != self.live_trading_enabled
            or adapter.dry_run != self.dry_run
        ):
            from services.oanda_rest_broker_adapter import OandaRestBrokerAdapter

            adapter = OandaRestBrokerAdapter(
                self.output_root,
                self.live_trading_enabled,
                self.broker_config,
                opener=self.opener,
            )
            self._oanda_adapter_instance = adapter
        return adapter

    def _mt5_adapter(self):
        adapter = self._mt5_adapter_instance
        if (
            adapter is None
            or adapter.broker_config is not self.broker_config
            or adapter.live_trading_enabled != self.live_trading_enabled
            or adapter.dry_run != self.dry_run
        ):
            from services.mt5_file_bridge_broker_adapter import (
                Mt5FileBridgeBrokerAdapter,
            )

            adapter = Mt5FileBridgeBrokerAdapter(
                self.output_root,
                self.live_trading_enabled,
                self.broker_config,
            )
            self._mt5_adapter_instance = adapter
        return adapter

    def _tiger_adapter(self):
        adapter = self._tiger_adapter_instance
        if (
            adapter is None
            or adapter._source_broker_config is not self.broker_config
            or adapter._source_config_snapshot != self.broker_config
            or adapter.live_trading_enabled != self.live_trading_enabled
            or adapter.dry_run != self.dry_run
        ):
            from services.tiger_openapi_broker_adapter import (
                TigerOpenApiPaperBrokerAdapter,
            )

            adapter = TigerOpenApiPaperBrokerAdapter(
                self.output_root,
                self.broker_config,
                live_trading_enabled=self.live_trading_enabled,
            )
            self._tiger_adapter_instance = adapter
        return adapter


def resolve_broker_config(config: dict | None = None) -> dict:
    config = config or load_pipeline_config()
    from services.broker_composition import resolve_broker_profile_config

    return resolve_broker_profile_config(config)


def _live_adapter_for_provider(
    output_root: Path,
    live_trading_enabled: bool,
    broker_config: dict,
) -> BrokerAdapter:
    from services.broker_composition import (
        build_configured_live_broker_execution_port,
    )

    return build_configured_live_broker_execution_port(
        output_root,
        live_trading_enabled,
        broker_config,
    )


def build_live_broker_adapter(
    output_root: Path,
    live_trading_enabled: bool,
    broker_config: dict,
) -> BrokerAdapter:
    return _live_adapter_for_provider(
        output_root,
        live_trading_enabled,
        broker_config,
    )


def broker_preflight(output_root: Path | None = None) -> dict:
    config = load_pipeline_config()
    root = output_root or Path(
        os.getenv(
            "TRADING_ORCHESTRATOR_OUTPUT_ROOT",
            str(ROOT / config.get("output_root", "outputs")),
        )
    )
    mode = str(config.get("execution_mode", "paper")).lower()
    broker_config = resolve_broker_config(config)
    if mode == "paper":
        result = {
            "provider": broker_config.get("provider", "manual_gateway"),
            "mode": "paper",
            "dry_run": True,
            "live_trading_enabled": bool(
                config.get("live_trading_enabled", False)
            ),
            "ready": True,
            "block_reason": "paper mode active; live broker is not used",
            "allowed_symbols": broker_config.get("allowed_symbols", []),
            "checked_at": datetime.now(timezone.utc)
            .replace(microsecond=0)
            .isoformat(),
        }
    else:
        result = _live_adapter_for_provider(
            root,
            bool(config.get("live_trading_enabled", False)),
            broker_config,
        ).preflight()
    write_json(root / "broker_preflight" / "current.json", [result])
    return result


def build_broker_adapter(output_root: Path) -> BrokerAdapter:
    config = load_pipeline_config()
    mode = str(config.get("execution_mode", "paper")).lower()
    broker_config = resolve_broker_config(config)
    if mode == "live":
        return _live_adapter_for_provider(
            output_root,
            bool(config.get("live_trading_enabled", False)),
            broker_config,
        )
    from services.broker_composition import (
        BrokerBuildContext,
        build_broker_execution_port,
    )

    return build_broker_execution_port(
        BrokerBuildContext(
            output_root=output_root,
            execution_mode=mode,
            live_trading_enabled=bool(
                config.get("live_trading_enabled", False)
            ),
            broker_config=broker_config,
        )
    )
