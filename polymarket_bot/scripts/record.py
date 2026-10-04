#!/usr/bin/env python3
"""Record REST book snapshots and the public market WebSocket. Places no orders."""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from pmbot.config import LiveTradingDisabled, load_config  # noqa: E402
from pmbot.data.recorder import run_recorder  # noqa: E402
from pmbot.logging_setup import setup_logging  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="Record Polymarket order books")
    parser.add_argument("--config", type=Path, default=ROOT / "config.yaml")
    parser.add_argument("--seconds", type=float, default=None, help="Stop after N seconds (default: until SIGINT)")
    args = parser.parse_args()
    config = load_config(args.config)
    setup_logging(config.log_level, config.log_path)
    try:
        asyncio.run(run_recorder(config, seconds=args.seconds))
    except LiveTradingDisabled as exc:
        print(exc, file=sys.stderr)
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
