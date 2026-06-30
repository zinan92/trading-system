from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from services.alert_notifier import resolve_alert_sender
from services.journal_store import write_json


class AlertDeliveryDrill:
    """Send a deliberate test alert and persist proof of delivery.

    Health checks can tell whether credentials are present, but M5 requires a
    stronger proof: a notification channel has actually delivered a message.
    """

    def __init__(self, output_root: Path, sender=None) -> None:
        self.output_root = Path(output_root)
        self.sender = sender if sender is not None else resolve_alert_sender()

    def run(self, run_date: str, message: str = "") -> dict:
        generated_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        text = message or f"[{run_date}] trading-orchestrator alert delivery drill"
        configured = bool(getattr(self.sender, "configured", False))
        channel = getattr(self.sender, "channel", "unknown")
        delivery = {"ok": False, "channel": channel, "reason": "not_configured"}
        if configured:
            delivery = self.sender.send(text)
        delivered = bool(delivery.get("ok"))
        status = "pass" if delivered else ("fail" if configured else "blocked")
        payload = {
            "run_date": run_date,
            "generated_at": generated_at,
            "status": status,
            "configured": configured,
            "delivered": delivered,
            "channel": delivery.get("channel", channel),
            "message": text,
            "delivery": delivery,
            "required_env": list(getattr(self.sender, "required_env", [])),
        }
        write_json(self.output_root / "alert_delivery_drill" / "current.json", [payload])
        write_json(self.output_root / "alert_delivery_drill" / f"{run_date}.json", [payload])
        return payload
