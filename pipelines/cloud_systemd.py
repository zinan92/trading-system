from __future__ import annotations

import argparse
import json
from pathlib import Path

from services.cloud_systemd import (
    CloudSystemdInstaller,
    CloudSystemdPaths,
    CloudSystemdRenderer,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Render or install Cloud Paper systemd units.")
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--datafeed-root", type=Path, required=True)
    parser.add_argument("--app-python", type=Path, required=True)
    parser.add_argument("--datafeed-python", type=Path, required=True)
    parser.add_argument("--render-dir", type=Path, required=True)
    parser.add_argument(
        "--action",
        choices=(
            "render",
            "install-passive",
            "activate-dashboard",
            "activate-remote-access",
            "activate-provider-readiness",
            "activate-next-cycle-plan",
            "uninstall",
        ),
        default="render",
    )
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    paths = CloudSystemdPaths(
        repo_root=args.repo_root,
        datafeed_root=args.datafeed_root,
        app_python=args.app_python,
        datafeed_python=args.datafeed_python,
    )
    render = CloudSystemdRenderer(paths).render(args.render_dir)
    if args.action == "render":
        result = render
    else:
        result = CloudSystemdInstaller().apply(
            args.render_dir,
            args.action,
            dry_run=not args.apply,
        )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
