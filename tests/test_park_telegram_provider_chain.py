from __future__ import annotations

from services.park_ai_provider_gateway import ParkAiProviderGateway


def test_deepseek_is_called_before_codex_and_short_circuits_on_success() -> None:
    calls: list[str] = []

    class DeepSeek:
        def parse(self, text: str, *, context=None) -> dict:
            calls.append("deepseek")
            return {
                "status": "ok",
                "candidate": {"direction": "long", "strategy_type": "dca"},
                "metadata": {"provider": "deepseek", "status": "returned"},
            }

    class Codex:
        def parse(self, text: str, **kwargs) -> dict:
            calls.append("codex")
            return {
                "status": "ok",
                "candidate": {"direction": "short", "strategy_type": "grid"},
                "metadata": {"provider": "codex_cli", "status": "returned"},
            }

    result = ParkAiProviderGateway(deepseek=DeepSeek(), codex=Codex()).parse(
        "做多 DCA",
        context={"market": {"price": 4300}},
    )

    assert result["status"] == "ok"
    assert result["candidate"]["direction"] == "long"
    assert calls == ["deepseek"]


def test_codex_is_called_only_after_deepseek_failure_and_records_fallback() -> None:
    calls: list[str] = []

    class DeepSeek:
        def parse(self, text: str, *, context=None) -> dict:
            calls.append("deepseek")
            return {
                "status": "unavailable",
                "metadata": {
                    "provider": "deepseek",
                    "status": "timeout",
                    "timed_out": True,
                    "elapsed_ms": 12000,
                },
            }

    class Codex:
        def parse(self, text: str, **kwargs) -> dict:
            calls.append("codex")
            return {
                "status": "ok",
                "candidate": {"direction": "long", "strategy_type": "dca"},
                "metadata": {"provider": "codex_cli", "status": "returned"},
            }

    result = ParkAiProviderGateway(deepseek=DeepSeek(), codex=Codex()).parse(
        "做多 DCA",
        context={"market": {"price": 4300}},
    )

    assert result["status"] == "ok"
    assert calls == ["deepseek", "codex"]
    assert result["metadata"]["fallback_from"] == "deepseek"
    assert result["metadata"]["fallback_status"] == "timeout"
    assert result["metadata"]["fallback_elapsed_ms"] == 12000


def test_gateway_honors_cloud_codex_override(monkeypatch) -> None:
    monkeypatch.setenv("TRADING_ORCHESTRATOR_CODEX_CLI", "/usr/local/bin/codex")
    gateway = ParkAiProviderGateway(
        deepseek=object(),
        codex=None,
    )

    assert gateway.codex.executable == "/usr/local/bin/codex"
    assert gateway.codex.model == "gpt-5.6-sol"

