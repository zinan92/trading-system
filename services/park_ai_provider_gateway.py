"""Shared DeepSeek-first provider chain for Park proposal surfaces.

Provider output is untrusted intent data.  This module only selects a bounded
natural-language provider; callers must still run the deterministic Park
normalizer, risk planner, ownership checks, and exact confirmation gate.
"""

from __future__ import annotations

import os
import shutil
from typing import Any, Mapping

from services.dashboard_ai_provider import (
    DEFAULT_DEEPSEEK_MODEL,
    DEFAULT_DEEPSEEK_TIMEOUT_SECONDS,
    DEFAULT_DEEPSEEK_URL,
    DeepSeekIntentProvider,
)
from services.park_codex_intent_parser import CodexCliIntentParser


class ParkAiProviderGateway:
    """Call DeepSeek first, then the bounded Codex CLI fallback."""

    def __init__(
        self,
        *,
        deepseek: Any | None = None,
        codex: Any | None = None,
    ) -> None:
        self.deepseek = deepseek or DeepSeekIntentProvider(
            base_url=os.environ.get("DEEPSEEK_API_URL") or DEFAULT_DEEPSEEK_URL,
            model=os.environ.get("DEEPSEEK_MODEL") or DEFAULT_DEEPSEEK_MODEL,
            timeout_seconds=float(
                os.environ.get(
                    "DEEPSEEK_TIMEOUT_SECONDS",
                    str(DEFAULT_DEEPSEEK_TIMEOUT_SECONDS),
                )
            ),
        )
        configured_codex = (
            os.environ.get("PARK_CODEX_CLI")
            or os.environ.get("CODEX_CLI")
            or os.environ.get("TRADING_ORCHESTRATOR_CODEX_CLI")
            or shutil.which("codex")
            or "/opt/homebrew/bin/codex"
        )
        codex_model = (
            os.environ.get("PARK_CODEX_MODEL")
            or os.environ.get("TRADING_ORCHESTRATOR_CODEX_MODEL")
            or "gpt-5.6-sol"
        )
        codex_timeout = os.environ.get(
            "PARK_CODEX_FALLBACK_TIMEOUT_SECONDS",
            os.environ.get("TRADING_ORCHESTRATOR_CODEX_TIMEOUT_SECONDS", "15"),
        )
        codex_cwd = (
            os.environ.get("PARK_CODEX_CWD")
            or os.environ.get("TRADING_ORCHESTRATOR_CODEX_CWD")
            or None
        )
        self.codex = codex or CodexCliIntentParser(
            executable=configured_codex,
            model=codex_model,
            timeout_seconds=float(codex_timeout),
            cwd=codex_cwd,
            codex_home=os.environ.get("CODEX_HOME") or None,
        )

    def parse(
        self,
        text: str,
        *,
        context: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        first = self._call(self.deepseek, text, context=context)
        if first.get("status") == "ok":
            return first
        first_metadata = dict(first.get("metadata") or {})
        second = self._call(self.codex, text, context=context)
        metadata = dict(second.get("metadata") or {})
        metadata.setdefault("fallback_from", str(first_metadata.get("provider") or "deepseek"))
        metadata["fallback_status"] = first_metadata.get("status")
        metadata["fallback_elapsed_ms"] = first_metadata.get("elapsed_ms")
        second["metadata"] = metadata
        return second

    @staticmethod
    def _call(
        provider: Any,
        text: str,
        *,
        context: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        try:
            result = provider.parse(text, context=context)
        except TypeError:
            # Codex's existing parser has a text-only public seam.
            try:
                result = provider.parse(text)
            except Exception as exc:  # noqa: BLE001 - fallback remains fail-closed.
                return {
                    "status": "unavailable",
                    "metadata": {
                        "provider": provider.__class__.__name__,
                        "status": "adapter_error",
                        "error_type": type(exc).__name__,
                    },
                }
        except Exception as exc:  # noqa: BLE001 - provider failure is safe fallback.
            return {
                "status": "unavailable",
                "metadata": {
                    "provider": provider.__class__.__name__,
                    "status": "adapter_error",
                    "error_type": type(exc).__name__,
                },
            }
        return dict(result or {})
