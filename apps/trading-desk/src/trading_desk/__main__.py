from __future__ import annotations

import argparse
import json
import os
import sys

import uvicorn

from services.structured_log import configure

from .advice import Advisor
from .app import create_app, current_state
from .config import Config
from .notify import Notifier, park_outbox
from .sources import Sources
from .store import Store
from .watch import Watch


def main() -> None:
    parser = argparse.ArgumentParser(prog="trading-desk")
    parser.add_argument("command", choices=["serve", "watch", "advise", "tick", "token"])
    parser.add_argument("rest", nargs="*", help="token: create <name> | list | revoke <name>  (read-only open API, #1344)")
    parser.add_argument("--port", type=int, default=None)
    # 127.0.0.1 unless told otherwise: off-machine traffic must come through a proxy, which the passcode
    # gate recognises by its headers. Docker binds 0.0.0.0 inside the container and publishes on the
    # host's loopback only (compose.yaml).
    parser.add_argument("--host", default=os.environ.get("TRADING_DESK_HOST", "127.0.0.1"))
    parser.add_argument("--force", action="store_true", help="watch: re-rank now; advise: re-ask the model for every asset")
    args = parser.parse_args()
    config = Config()
    if args.command == "token":
        raise SystemExit(_token(config, args.rest))
    configure("desk" if args.command == "serve" else f"desk-{args.command}", config.paper_output / "logs")  # one writer per file
    if args.command in {"watch", "tick"}:
        watch = Watch(config.watch_folder, config.intel_url)
        result = watch.refresh(force=args.force)
        report = {key: result.get(key) for key in ("generated_at", "provider", "replaced")}
        if args.command == "tick":  # the 20-minute job: refresh the three things, then send what is due
            sources, store = Sources(config), Store(config.db_path)
            futures = [a["kline_key"] for a in store.assets() if sources.venues.of(a).can("futures_overview") and a.get("kline_key")]
            report["futures"] = sources.refresh_futures(futures) if futures else None
            advisor = Advisor(store, sources, watch, asset_state=lambda asset: current_state(sources, asset))
            report["pushed"] = Notifier(config, store, sources, watch, advisor, queue=park_outbox(config)).run()
        print(json.dumps(report, ensure_ascii=False))
        return
    if args.command == "advise":
        sources, store = Sources(config), Store(config.db_path)
        advisor = Advisor(store, sources, Watch(config.watch_folder, config.intel_url), asset_state=lambda asset: current_state(sources, asset))
        print(json.dumps(advisor.generate(force=args.force), ensure_ascii=False))
        return
    uvicorn.run(create_app(config), host=args.host, port=args.port or config.port, log_level="warning")


def _token(config: Config, rest: list[str]) -> int:
    """Manage read-only API tokens. The token is printed once, here; only its hash is stored."""
    from .open_api import Tokens

    tokens = Tokens(config.api_tokens)
    action, name = (rest + ["", ""])[:2]
    try:
        if action == "create" and name:
            print(tokens.create(name))
            print(f"↑ 只显示这一次。只读：{config.api_tokens} 里只存它的 SHA-256。", file=sys.stderr)
            return 0
        if action == "revoke" and name:
            if tokens.revoke(name):
                print(f"revoked {name}")
                return 0
            print(f"没有叫 {name} 的 token", file=sys.stderr)
            return 1
        if action == "list":
            for row in tokens.names():
                print(f"{row['name']}\t{row['created_at']}")
            return 0
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 1
    print("usage: python -m trading_desk token create <name> | list | revoke <name>", file=sys.stderr)
    return 2


if __name__ == "__main__":
    main()
