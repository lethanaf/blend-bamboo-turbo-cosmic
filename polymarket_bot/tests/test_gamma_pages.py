import asyncio
import json
from dataclasses import replace
from pathlib import Path

import httpx

from pmbot.config import load_config
from pmbot.data.gamma import discover_markets

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "market.json").read_text(encoding="utf-8"))


def test_discovery_stops_once_the_universe_is_full() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        assert request.url.params["order"] == "volume24hr"
        assert request.url.params["ascending"] == "false"
        page = []
        for index in range(100):
            item = dict(FIXTURE)
            item["conditionId"] = f"0x{calls['n']:02d}{index:02d}"
            item["id"] = f"{calls['n']}-{index}"
            page.append(item)
        return httpx.Response(200, json=page)

    async def run() -> None:
        config = load_config(Path(__file__).resolve().parents[1] / "config.yaml")
        config = replace(config, max_markets=2, gamma_page_size=100, gamma_max_pages=10)
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport, base_url=config.gamma_base) as http:
            markets = await discover_markets(http, config)
        assert len(markets) == 2
        assert calls["n"] == 1

    asyncio.run(run())
