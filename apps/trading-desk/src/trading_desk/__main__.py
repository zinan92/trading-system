from __future__ import annotations

import argparse

import uvicorn

from .app import create_app
from .config import Config


def main() -> None:
    parser = argparse.ArgumentParser(prog="trading-desk")
    parser.add_argument("command", choices=["serve"])
    parser.add_argument("--port", type=int, default=None)
    args = parser.parse_args()
    config = Config()
    uvicorn.run(create_app(config), host="127.0.0.1", port=args.port or config.port, log_level="warning")


if __name__ == "__main__":
    main()
