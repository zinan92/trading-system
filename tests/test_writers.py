import json
from pathlib import Path

from schemas.analysis import Analysis
from schemas.backtest import BacktestEvidence
from schemas.journal import JournalPending
from schemas.signal import Signal
from schemas.trade_ticket import TradeTicket
from services.writers import write_outputs


def test_write_outputs_preserves_same_day_signal_and_ticket_evidence(tmp_path: Path):
    root = tmp_path / "outputs"
    run_date = "2026-06-21"
    signal = Signal(
        signal_id="sig_bollinger_gold_20260621_trade",
        asset="GOLD",
        asset_class="commodity",
        direction="short",
        strength=74,
        confidence=66,
        horizon="bollinger_reversion",
        thesis="price stretched above upper band",
        generated_at="2026-06-21T02:32:52+00:00",
    )
    ticket = TradeTicket(
        ticket_id="ticket_bollinger_gold_20260621_trade",
        signal_id=signal.signal_id,
        asset="GOLD",
        asset_class="commodity",
        action="sell",
        entry_zone="4159",
        stop_loss=4242.25,
        targets=[3992.71],
        rationale="bollinger reversion setup",
    )
    analysis = Analysis("analysis_trade", signal.signal_id, "GOLD", methods=["mean-reversion"])
    backtest = BacktestEvidence("backtest_trade", signal.signal_id, "GOLD", 12, 0.58, 0.7, -0.8, "thin")

    write_outputs(root, run_date, [signal], [ticket], [JournalPending.from_ticket(ticket, signal.generated_at)], [analysis], [backtest])

    watch = Signal(
        signal_id="sig_bollinger_gold_20260621_watch",
        asset="GOLD",
        asset_class="commodity",
        direction="watch",
        strength=0,
        confidence=0,
        horizon="bollinger_reversion",
        thesis="no bollinger_reversion setup",
        generated_at="2026-06-21T05:34:37+00:00",
        status="no_signal",
    )
    watch_analysis = Analysis("analysis_watch", watch.signal_id, "GOLD", methods=["no-trade"])
    watch_backtest = BacktestEvidence("backtest_watch", watch.signal_id, "GOLD", 0, 0, 0, 0, "no_trade")

    write_outputs(root, run_date, [watch], [], [], [watch_analysis], [watch_backtest])

    signals = json.loads((root / "signals" / f"{run_date}.json").read_text())
    tickets = json.loads((root / "trade_tickets" / f"{run_date}.json").read_text())
    pending = json.loads((root / "journal_pending" / f"{run_date}.json").read_text())
    analyses = json.loads((root / "analyses" / f"{run_date}.json").read_text())
    backtests = json.loads((root / "backtests" / f"{run_date}.json").read_text())

    assert [item["signal_id"] for item in signals] == [signal.signal_id, watch.signal_id]
    assert [item["ticket_id"] for item in tickets] == [ticket.ticket_id]
    assert pending == []
    assert {item["signal_id"] for item in analyses} == {signal.signal_id, watch.signal_id}
    assert {item["signal_id"] for item in backtests} == {signal.signal_id, watch.signal_id}


def test_limit_ticket_pending_journal_waits_for_entry_order():
    ticket = TradeTicket(
        ticket_id="ticket_limit_wait",
        signal_id="sig_limit_wait",
        asset="GOLD",
        asset_class="commodity",
        action="buy",
        entry_zone="100.50-100.50",
        stop_loss=100.0,
        targets=[101.5],
        rationale="limit entry",
        order_type="limit",
        entry_order_limit_price=100.5,
        entry_order_ttl_bars=10,
        entry_order_timeframe="1m",
        entry_order_created_bar_timestamp="2026-07-04T00:00:00+00:00",
    )

    pending = JournalPending.from_ticket(ticket, "2026-07-04T00:00:00+00:00").to_dict()

    assert pending["decision_status"] == "pending_entry_order"
    assert pending["required_user_action"] == "None; waiting for limit entry or expiry."
    assert pending["entry_order_limit_price"] == 100.5
    assert pending["entry_order_ttl_bars"] == 10


def test_write_outputs_recovers_from_corrupt_same_day_artifact(tmp_path: Path):
    """A truncated/corrupt artifact (e.g. SIGKILL mid-write — write_text is not
    atomic) must not permanently brick the strategy. The merge reads before it
    writes, so an unguarded read would raise every run forever; the writer must
    self-heal by overwriting, the way the pre-merge writer did."""
    root = tmp_path / "outputs"
    run_date = "2026-06-21"
    corrupt = root / "signals" / f"{run_date}.json"
    corrupt.parent.mkdir(parents=True, exist_ok=True)
    corrupt.write_text('[{"signal_id": "sig_half_writ', encoding="utf-8")  # truncated JSON

    signal = Signal(
        signal_id="sig_recovered_gold_20260621",
        asset="GOLD",
        asset_class="commodity",
        direction="short",
        strength=70,
        confidence=60,
        horizon="bollinger_reversion",
        thesis="recovered after corrupt file",
        generated_at="2026-06-21T06:00:00+00:00",
    )

    # Must not raise, and must produce a valid file containing the new signal.
    write_outputs(root, run_date, [signal], [], [], [], [])

    signals = json.loads(corrupt.read_text())
    assert [item["signal_id"] for item in signals] == [signal.signal_id]
