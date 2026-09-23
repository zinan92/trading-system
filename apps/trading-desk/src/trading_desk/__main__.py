from __future__ import annotations

import argparse
import json

import uvicorn

from .advice import Advisor
from .app import create_app, current_state
from .config import Config
from .notify import Notifier, park_outbox
from .sources import Sources
from .store import Store
from .watch import Watch


def main() -> None:
    parser = argparse.ArgumentParser(prog="trading-desk")
    parser.add_argument("command", choices=["serve", "watch", "advise", "tick"])
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--force", action="store_true", help="watch: re-rank now; advise: re-ask the model for every asset")
    args = parser.parse_args()
    config = Config()
    if args.command in {"watch", "tick"}:
        watch = Watch(config.watch_folder, config.intel_url)
        result = watch.refresh(force=args.force)
        report = {key: result.get(key) for key in ("generated_at", "provider", "replaced")}
        if args.command == "tick":  # the 20-minute job: refresh the three things, then send what is due
            sources, store = Sources(config), Store(config.db_path)
            futures = [a["kline_key"] for a in store.assets() if a["kind"] == "watch" and a.get("kline_key")]
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
    uvicorn.run(create_app(config), host="127.0.0.1", port=args.port or config.port, log_level="warning")


if __name__ == "__main__":
    main()
