from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

from services.cloud_aliyun import CloudAliyunProvisioner
from services.config_loader import ROOT


def _load(path: Path) -> dict:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain an object")
    return payload


def _git_sha(root: Path) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(root),
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _datafeed_sha() -> str:
    for line in (ROOT / "deploy" / "cloud" / "source-lock.env").read_text().splitlines():
        if line.startswith("GRIDMIND_DATAFEED_SHA="):
            return line.split("=", 1)[1].strip()
    raise ValueError("datafeed_sha_missing")


def main() -> int:
    parser = argparse.ArgumentParser(description="Render or apply Alibaba SWAS Paper host.")
    parser.add_argument("action", choices=("render", "apply"))
    parser.add_argument("--spec", type=Path, default=ROOT / "deploy/cloud/aliyun-plan.json")
    parser.add_argument("--datafeed-root", type=Path)
    parser.add_argument("--render-dir", type=Path)
    parser.add_argument("--host")
    parser.add_argument("--ssh-key", type=Path)
    parser.add_argument("--plan", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    spec = _load(args.spec)
    provisioner = CloudAliyunProvisioner(repo_root=ROOT)

    if args.action == "render":
        if not args.datafeed_root or not args.render_dir or not args.host or not args.ssh_key:
            raise SystemExit("render requires --datafeed-root, --render-dir, --host and --ssh-key")
        render_dir = args.render_dir.resolve()
        render_dir.mkdir(parents=True, exist_ok=True)
        trading_sha = _git_sha(ROOT)
        datafeed_sha = _datafeed_sha()
        trading_archive = render_dir / "trading-system.tar.gz"
        datafeed_archive = render_dir / "datafeed.tar.gz"
        trading_bundle = render_dir / "trading-system.bundle"
        datafeed_bundle = render_dir / "datafeed.bundle"
        trading_archive_sha = provisioner.source_archive(
            repo=ROOT, revision=trading_sha, destination=trading_archive
        )
        datafeed_archive_sha = provisioner.source_archive(
            repo=args.datafeed_root,
            revision=datafeed_sha,
            destination=datafeed_archive,
        )
        trading_bundle_sha = provisioner.source_bundle(
            repo=ROOT, ref="HEAD", destination=trading_bundle
        )
        datafeed_bundle_sha = provisioner.source_bundle(
            repo=args.datafeed_root,
            ref="refs/remotes/origin/main",
            destination=datafeed_bundle,
        )
        bootstrap = provisioner.render_bootstrap(
            trading_sha=trading_sha,
            datafeed_sha=datafeed_sha,
            trading_archive_sha256=trading_archive_sha,
            datafeed_archive_sha256=datafeed_archive_sha,
            trading_bundle_sha256=trading_bundle_sha,
            datafeed_bundle_sha256=datafeed_bundle_sha,
            runtime=spec.get("runtime") or {},
        )
        bootstrap_path = render_dir / "bootstrap.sh"
        bootstrap_path.write_text(bootstrap, encoding="utf-8")
        bootstrap_path.chmod(0o700)
        result = provisioner.plan(
            spec=spec,
            host=args.host,
            ssh_key=args.ssh_key,
            render_dir=render_dir,
        )
        plan_path = render_dir / "deploy-plan.json"
        plan_path.write_text(
            json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        result = {**result, "plan_path": str(plan_path)}
    else:
        if not args.plan:
            raise SystemExit("apply requires --plan")
        result = provisioner.apply(_load(args.plan), dry_run=not args.apply)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result.get("status") not in {"blocked", "failed"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
