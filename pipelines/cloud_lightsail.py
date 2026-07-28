from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path

from services.cloud_lightsail import CloudLightsailProvisioner
from services.config_loader import ROOT, load_pipeline_config


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
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError("trading_system_sha_unavailable")
    return str(result.stdout or "").strip()


def main() -> int:
    parser = argparse.ArgumentParser(description="Plan or apply the passive Lightsail Paper host.")
    parser.add_argument(
        "action",
        choices=("catalog", "render", "apply", "verify"),
    )
    parser.add_argument(
        "--spec",
        type=Path,
        default=ROOT / "deploy" / "cloud" / "lightsail-plan.json",
    )
    parser.add_argument("--catalog", type=Path)
    parser.add_argument("--operator-cidr")
    parser.add_argument("--key-pair-name")
    parser.add_argument("--plan", type=Path)
    parser.add_argument("--render-dir", type=Path)
    parser.add_argument("--instance-name")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    config = load_pipeline_config()
    output_root = Path(
        os.getenv(
            "TRADING_ORCHESTRATOR_OUTPUT_ROOT",
            str(ROOT / config.get("output_root", "outputs")),
        )
    )
    provisioner = CloudLightsailProvisioner(
        repo_root=ROOT,
        output_root=output_root,
    )
    spec = _load(args.spec)
    region = str(spec.get("region") or "")

    if args.action == "catalog":
        result = provisioner.catalog(region=region)
    elif args.action == "render":
        if not args.catalog or not args.operator_cidr or not args.key_pair_name or not args.render_dir:
            raise SystemExit(
                "render requires --catalog, --operator-cidr, --key-pair-name and --render-dir"
            )
        catalog = _load(args.catalog)
        selection = provisioner.select(spec, catalog)
        datafeed_lock = {}
        for line in (ROOT / "deploy" / "cloud" / "source-lock.env").read_text().splitlines():
            if line.startswith("GRIDMIND_DATAFEED_SHA="):
                datafeed_lock["sha"] = line.split("=", 1)[1].strip()
        render_dir = Path(args.render_dir)
        render_dir.mkdir(parents=True, exist_ok=True)
        user_data_path = render_dir / "cloud-init.sh"
        user_data_path.write_text(
            provisioner.render_user_data(
                trading_sha=_git_sha(ROOT),
                datafeed_sha=str(datafeed_lock.get("sha") or ""),
                runtime=spec.get("runtime") or {},
            ),
            encoding="utf-8",
        )
        user_data_path.chmod(0o700)
        result = provisioner.plan(
            spec=spec,
            selection=selection,
            operator_cidr=args.operator_cidr,
            key_pair_name=args.key_pair_name,
            user_data_path=user_data_path,
        )
        (render_dir / "provision-plan.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    elif args.action == "apply":
        if not args.plan:
            raise SystemExit("apply requires --plan")
        result = provisioner.apply(_load(args.plan), dry_run=not args.apply)
    else:
        result = provisioner.verify_instance(
            region=region,
            instance_name=args.instance_name or str(spec.get("instance_name") or ""),
        )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result.get("status") not in {"blocked", "failed"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
