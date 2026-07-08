from __future__ import annotations

import argparse
import json

from services.schedule_manager import ScheduleManager


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate local launchd schedule files for runner, plan/review, daily ops review, and dashboard.")
    parser.add_argument("--plan-hour", type=int, default=8)
    parser.add_argument("--plan-minute", type=int, default=30)
    parser.add_argument("--evening-review-hour", type=int, default=23)
    parser.add_argument("--evening-review-minute", type=int, default=30)
    parser.add_argument("--review-hour", type=int, default=23)
    parser.add_argument("--review-minute", type=int, default=55)
    parser.add_argument("--dashboard-port", type=int, default=8765)
    parser.add_argument("--profile", choices=["dualtrack_focus", "full"], default="", help="Override configs/pipeline.yaml schedule.profile.")
    parser.add_argument("--json", action="store_true", help="Print the full JSON payload.")
    args = parser.parse_args()

    result = ScheduleManager(profile=args.profile or None).build(
        review_hour=args.review_hour,
        review_minute=args.review_minute,
        dashboard_port=args.dashboard_port,
        plan_hour=args.plan_hour,
        plan_minute=args.plan_minute,
        evening_review_hour=args.evening_review_hour,
        evening_review_minute=args.evening_review_minute,
    )
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return
    print(f"schedule: {result['status']}")
    print(f"launch_agents: {result['launch_agents_dir']}")
    for job in result["jobs"]:
        cadence = job.get("start_interval") or job.get("start_calendar_interval") or ("keepalive" if job.get("keep_alive") else "manual")
        print(f"- {job['label']}: {cadence}")
    print("Generated only; review README before installing.")


if __name__ == "__main__":
    main()
