from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from services.connector_config_apply import ConnectorConfigApply
from services.journal_store import load_json


def _latest_json(path: Path) -> dict:
    rows = load_json(path)
    if not rows:
        raise SystemExit(f"no JSON receipt found at {path}")
    return rows[-1]


def main() -> None:
    parser = argparse.ArgumentParser(description="Apply or roll back connector config changes with explicit acknowledgement.")
    sub = parser.add_subparsers(dest="command", required=True)

    apply_parser = sub.add_parser("apply", help="Apply an activation plan to config files.")
    apply_parser.add_argument("--plan", default="outputs/connector_activation_plan/current.json")
    apply_parser.add_argument("--acknowledgement", default="")
    apply_parser.add_argument("--package-id", default="")
    apply_parser.add_argument("--accept-warnings", action="store_true")
    apply_parser.add_argument("--write", action="store_true", help="Actually write config files. Omit for dry-run.")
    apply_parser.add_argument("--pipeline-config", default="")
    apply_parser.add_argument("--dualtrack-config", default="")
    apply_parser.add_argument("--output-root", default="")
    apply_parser.add_argument("--json", action="store_true")

    check_parser = sub.add_parser("check", help="Validate the current activation package and dry-run without writing config.")
    check_parser.add_argument("--plan", default="outputs/connector_activation_plan/current.json")
    check_parser.add_argument("--pipeline-config", default="")
    check_parser.add_argument("--dualtrack-config", default="")
    check_parser.add_argument("--output-root", default="")
    check_parser.add_argument("--json", action="store_true")

    handoff_parser = sub.add_parser("handoff", help="Generate a read-only operator handoff for the current config package.")
    handoff_parser.add_argument("--plan", default="outputs/connector_activation_plan/current.json")
    handoff_parser.add_argument("--pipeline-config", default="")
    handoff_parser.add_argument("--dualtrack-config", default="")
    handoff_parser.add_argument("--output-root", default="")
    handoff_parser.add_argument("--json", action="store_true")

    rehearsal_parser = sub.add_parser("rehearse", help="Run the config apply/rollback sequence against sandbox config copies.")
    rehearsal_parser.add_argument("--plan", default="outputs/connector_activation_plan/current.json")
    rehearsal_parser.add_argument("--pipeline-config", default="")
    rehearsal_parser.add_argument("--dualtrack-config", default="")
    rehearsal_parser.add_argument("--output-root", default="")
    rehearsal_parser.add_argument("--json", action="store_true")

    authorization_parser = sub.add_parser("authorization", help="Generate a read-only operator authorization package for the current config package.")
    authorization_parser.add_argument("--plan", default="outputs/connector_activation_plan/current.json")
    authorization_parser.add_argument("--pipeline-config", default="")
    authorization_parser.add_argument("--dualtrack-config", default="")
    authorization_parser.add_argument("--output-root", default="")
    authorization_parser.add_argument("--json", action="store_true")

    audit_parser = sub.add_parser("readiness-audit", help="Generate a read-only Go/No-Go audit for the current Tiger/MGC switch package.")
    audit_parser.add_argument("--plan", default="outputs/connector_activation_plan/current.json")
    audit_parser.add_argument("--pipeline-config", default="")
    audit_parser.add_argument("--dualtrack-config", default="")
    audit_parser.add_argument("--output-root", default="")
    audit_parser.add_argument("--json", action="store_true")

    post_switch_parser = sub.add_parser("post-switch-validate", help="Validate a completed Tiger/MGC config switch without opening network clients or orders.")
    post_switch_parser.add_argument("--package-id", default="")
    post_switch_parser.add_argument("--pipeline-config", default="")
    post_switch_parser.add_argument("--dualtrack-config", default="")
    post_switch_parser.add_argument("--output-root", default="")
    post_switch_parser.add_argument("--json", action="store_true")

    status_parser = sub.add_parser("status", help="Show the current read-only Tiger/MGC connector switch stage.")
    status_parser.add_argument("--pipeline-config", default="")
    status_parser.add_argument("--dualtrack-config", default="")
    status_parser.add_argument("--output-root", default="")
    status_parser.add_argument("--json", action="store_true")

    refresh_runbook_parser = sub.add_parser("price-feed-refresh-runbook", help="Write a read-only Tiger/MGC price-feed refresh runbook.")
    refresh_runbook_parser.add_argument("--pipeline-config", default="")
    refresh_runbook_parser.add_argument("--dualtrack-config", default="")
    refresh_runbook_parser.add_argument("--output-root", default="")
    refresh_runbook_parser.add_argument("--as-of", default="")
    refresh_runbook_parser.add_argument("--json", action="store_true")

    rollback_parser = sub.add_parser("rollback", help="Restore config files from an apply receipt backup.")
    rollback_parser.add_argument("--receipt", required=True)
    rollback_parser.add_argument("--acknowledgement", required=True)
    rollback_parser.add_argument("--pipeline-config", default="")
    rollback_parser.add_argument("--dualtrack-config", default="")
    rollback_parser.add_argument("--output-root", default="")
    rollback_parser.add_argument("--json", action="store_true")

    args = parser.parse_args()
    service = ConnectorConfigApply(
        output_root=Path(args.output_root) if getattr(args, "output_root", "") else None,
        pipeline_config_path=Path(args.pipeline_config) if getattr(args, "pipeline_config", "") else None,
        dualtrack_config_path=Path(args.dualtrack_config) if getattr(args, "dualtrack_config", "") else None,
    )
    if args.command == "apply":
        result = service.apply(
            {
                "activation_plan": _latest_json(Path(args.plan)),
                "acknowledgement": args.acknowledgement,
                "package_id": args.package_id,
                "accept_warnings": args.accept_warnings,
                "write": args.write,
            }
        )
    elif args.command == "check":
        result = service.check_plan({"activation_plan": _latest_json(Path(args.plan))})
    elif args.command == "handoff":
        result = service.handoff({"activation_plan": _latest_json(Path(args.plan))})
    elif args.command == "rehearse":
        result = service.rehearse({"activation_plan": _latest_json(Path(args.plan))})
    elif args.command == "authorization":
        result = service.authorization({"activation_plan": _latest_json(Path(args.plan))})
    elif args.command == "readiness-audit":
        result = service.readiness_audit({"activation_plan": _latest_json(Path(args.plan))})
    elif args.command == "post-switch-validate":
        result = service.post_switch_validate({"package_id": args.package_id})
    elif args.command == "status":
        result = service.status()
    elif args.command == "price-feed-refresh-runbook":
        result = service.price_feed_refresh_runbook(as_of=args.as_of or None)
    else:
        result = service.rollback(
            {
                "apply_receipt": _latest_json(Path(args.receipt)),
                "acknowledgement": args.acknowledgement,
            }
        )

    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print(f"connector_config_{args.command}: {result.get('status')}")
        for blocker in result.get("blockers", []):
            print(f"- blocker {blocker.get('name')}: {blocker.get('summary')}")
    if result.get("status") == "blocked":
        sys.exit(1)


if __name__ == "__main__":
    main()
