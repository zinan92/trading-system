from pathlib import Path

from pipelines import bot


class _FakeBrokerFeedBridge:
    def import_pending(self, run_date: str) -> dict:
        return {"new_files": 0, "imported_rows": 0}


class _FakeBrokerReceiptImporter:
    def import_pending(self, run_date: str) -> dict:
        return {"new_receipts": 0, "total_receipts": 0}


class _FakeReportBuilder:
    output_root = Path("/tmp/trading-orchestrator-test/outputs")

    def build_daily_report(self, run_date: str) -> Path:
        return self.output_root / "reports" / f"{run_date}.md"


class _FakeDataSourcePreflight:
    def run(self, run_date: str) -> dict:
        return {"status": "pass", "message": "paper ready", "ready_for_paper": True}


class _FakeDataSourceLineage:
    def run(self, run_date: str) -> dict:
        return {"status": "pass", "truth_level": "official_broker", "ready_for_live": True}


class _FakeLiveSubmissionSafetySmoke:
    def run(self, run_date: str) -> dict:
        return {"status": "pass", "blocked_by_activation_gate": True, "network_call_attempted": False}


class _FakeCompletionAudit:
    def run(self, run_date: str) -> dict:
        return {"status": "pass"}


class _FakeMockTradingRuntime:
    def run(self, run_date: str) -> dict:
        return {"status": "pass", "mock_ready": True, "mock_running": True}


class _FakeLiveReadiness:
    def run(self, run_date: str) -> dict:
        return {"status": "fail", "live_ready": False}


class _FakeStrategyGuardrailsAllow:
    def allows_new_paper_order(self, run_date: str) -> tuple[bool, dict]:
        return True, {"summary": {"block_reasons": []}}


def _patch_common(monkeypatch, tmp_path: Path, pending: list[dict]) -> None:
    output_root = tmp_path / "outputs"
    market_db = tmp_path / "market.db"
    live_env = tmp_path / "live.env"
    monkeypatch.setenv("TRADING_ORCHESTRATOR_OUTPUT_ROOT", str(output_root))
    monkeypatch.setenv("TRADING_ORCHESTRATOR_MARKET_DB", str(market_db))
    monkeypatch.setenv("TRADING_ORCHESTRATOR_LIVE_ENV", str(live_env))
    _FakeReportBuilder.output_root = output_root

    monkeypatch.setattr(bot, "run_binance_usdm_feed_import", lambda run_date: {"status": "skipped", "imported_rows": 0})
    monkeypatch.setattr(bot, "run_oanda_feed_import", lambda run_date: {"status": "skipped", "imported_rows": 0})
    monkeypatch.setattr(bot, "BrokerFeedBridge", _FakeBrokerFeedBridge)
    monkeypatch.setattr(bot, "BrokerReceiptImporter", _FakeBrokerReceiptImporter)
    monkeypatch.setattr(bot, "collect_once", lambda run_date: [])
    monkeypatch.setattr(bot, "run_daily_pipeline", lambda run_date: {"journal_pending": Path("journal_pending.json")})
    monkeypatch.setattr(bot, "load_json", lambda path: pending if Path(path).name == "journal_pending.json" else [])
    monkeypatch.setattr(bot, "ReportBuilder", _FakeReportBuilder)
    monkeypatch.setattr(bot, "broker_preflight", lambda: {"status": "pass"})
    monkeypatch.setattr(bot, "DataSourcePreflight", _FakeDataSourcePreflight)
    monkeypatch.setattr(bot, "DataSourceLineage", _FakeDataSourceLineage)
    monkeypatch.setattr(bot, "LiveSubmissionSafetySmoke", _FakeLiveSubmissionSafetySmoke)
    monkeypatch.setattr(bot, "CompletionAudit", _FakeCompletionAudit)
    monkeypatch.setattr(bot, "MockTradingRuntime", _FakeMockTradingRuntime)
    monkeypatch.setattr(bot, "LiveReadiness", _FakeLiveReadiness)
    monkeypatch.setattr(bot, "StrategyGuardrails", _FakeStrategyGuardrailsAllow)
    monkeypatch.setattr(bot, "run_data_health", lambda run_date: {"status": "pass", "summary": {"skipped": True}})


def test_bot_cycle_blocks_paper_auto_approve_when_risk_monitor_blocks(monkeypatch, tmp_path: Path):
    pending = [{"ticket_id": "ticket_gold_blocked"}]
    _patch_common(monkeypatch, tmp_path, pending)

    class FakeRiskMonitor:
        def run(self, run_date: str) -> dict:
            return {
                "status": "block",
                "kill_switch_active": True,
                "allow_paper_auto_approve": False,
                "summary": {"block_reasons": ["daily loss stop breached"]},
            }

    class RecordingJournalStore:
        def __init__(self) -> None:
            self.calls: list[dict] = []
            self.output_root = tmp_path / "outputs"

        def record_decision(self, run_date, ticket_id, decision, notes="", **kwargs):
            self.calls.append({"ticket_id": ticket_id, "decision": decision, "notes": notes})
            return {"ticket_id": ticket_id, "decision_status": decision}

    store = RecordingJournalStore()
    monkeypatch.setattr(bot, "RiskMonitor", FakeRiskMonitor)
    monkeypatch.setattr(bot, "JournalStore", lambda *a, **k: store)
    class FakeAutoGate:
        def evaluate(self, run_date: str, auto_requested: bool) -> dict:
            risk = FakeRiskMonitor().run(run_date)
            return {
                "status": "block",
                "allow_auto_approve": False,
                "reasons": ["daily loss stop breached"],
                "risk_monitor": risk,
            }

    monkeypatch.setattr(bot, "PaperAutoApprovalGate", FakeAutoGate)

    result = bot.run_bot_cycle("2026-05-26", paper_auto_approve=True)

    # Autonomous: a gate-blocked ticket is auto-REJECTED (not left in the manual middle state).
    assert result["decision"] is None
    assert "daily loss stop breached" in result["execution_error"]
    assert store.calls and store.calls[0]["decision"] == "rejected"
    assert "auto-rejected (safety)" in store.calls[0]["notes"]
    assert result["auto_resolution"]["rejected"] == ["ticket_gold_blocked"]


def test_bot_cycle_auto_approves_only_after_risk_monitor_allows(monkeypatch, tmp_path: Path):
    pending = [{"ticket_id": "ticket_gold_allowed"}]
    _patch_common(monkeypatch, tmp_path, pending)
    decisions = []

    class FakeRiskMonitor:
        def run(self, run_date: str) -> dict:
            return {
                "status": "pass",
                "kill_switch_active": False,
                "allow_paper_auto_approve": True,
                "summary": {"block_reasons": []},
            }

    class FakeJournalStore:
        output_root = tmp_path / "outputs"

        def record_decision(self, run_date: str, ticket_id: str, decision: str, notes: str) -> dict:
            record = {"run_date": run_date, "ticket_id": ticket_id, "decision_status": decision, "notes": notes}
            decisions.append(record)
            return record

    monkeypatch.setattr(bot, "RiskMonitor", FakeRiskMonitor)
    monkeypatch.setattr(bot, "JournalStore", FakeJournalStore)
    class FakeAutoGate:
        def evaluate(self, run_date: str, auto_requested: bool) -> dict:
            risk = FakeRiskMonitor().run(run_date)
            return {
                "status": "allow",
                "allow_auto_approve": True,
                "reasons": [],
                "risk_monitor": risk,
            }

    monkeypatch.setattr(bot, "PaperAutoApprovalGate", FakeAutoGate)

    result = bot.run_bot_cycle("2026-05-26", paper_auto_approve=True)

    assert result["execution_error"] == ""
    assert result["decision"]["ticket_id"] == "ticket_gold_allowed"
    assert decisions == [result["decision"]]
    assert result["pre_execution_risk_monitor"]["allow_paper_auto_approve"] is True
