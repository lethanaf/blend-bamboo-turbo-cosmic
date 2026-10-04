#!/usr/bin/env python3
"""Write the market catalog and fee-rate payloads. Does not open a WebSocket."""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from pmbot.config import LiveTradingDisabled, load_config  # noqa: E402
from pmbot.data.recorder import Recorder  # noqa: E402
from pmbot.logging_setup import setup_logging  # noqa: E402


async def _main(config_path: Path) -> int:
    config = load_config(config_path)
    setup_logging(config.log_level, config.log_path)
    try:
        async with Recorder(config) as recorder:
            await recorder.check_exchange()
            markets = await recorder.catalog()
    except LiveTradingDisabled as exc:
        print(exc, file=sys.stderr)
        return 2
    for market in markets:
        rate = None if not market.fee_schedule else market.fee_schedule.get("rate")
        print(
            f"{market.volume_24hr or 0:12.2f}  tokens={len(market.tokens)}  "
            f"fee_rate={rate}  fees_enabled={market.fees_enabled}  {market.question}"
        )
    print(f"catalog={config.sqlite_path} markets={len(markets)}")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Discover Polymarket markets into SQLite")
    parser.add_argument("--config", type=Path, default=ROOT / "config.yaml")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(_main(args.config)))


if __name__ == "__main__":
    main()
