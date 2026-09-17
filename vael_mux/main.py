from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

import uvicorn

from .config import AppConfig
from .database import Database
from .logger import LogBus, setup_logging
from .web.app import create_app


def run() -> None:
    parser = argparse.ArgumentParser("vael-mux")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    args = parser.parse_args()

    cfg_path = Path(args.config).resolve()
    if not cfg_path.exists():
        print(f"[vael-mux] config not found: {cfg_path}", file=sys.stderr)
        sys.exit(1)

    cfg = AppConfig.load(cfg_path)
    setup_logging(cfg.logging.level)

    host = args.host or cfg.server.host
    port = args.port or cfg.server.port

    app = create_app(cfg, cfg_path)
    uvicorn.run(app, host=host, port=port, log_config=None)


if __name__ == "__main__":
    run()
