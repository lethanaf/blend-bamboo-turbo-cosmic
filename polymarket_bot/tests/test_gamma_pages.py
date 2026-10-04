import asyncio
import json
from dataclasses import replace
from datetime import datetime, timezone
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


def test_refresh_skips_known_markets_and_keeps_reading() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        page = []
        for index in range(100):
            item = dict(FIXTURE)
            item["conditionId"] = f"0x{calls['n']:02d}{index:02d}"
            item["id"] = f"{calls['n']}-{index}"
            page.append(item)
        return httpx.Response(200, json=page)

    async def run() -> None:
        config = load_config(Path(__file__).resolve().parents[1] / "config.yaml")
        config = replace(config, max_markets=30, gamma_page_size=100, gamma_max_pages=10)
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport, base_url=config.gamma_base) as http:
            markets = await discover_markets(http, config, skip={"0x0100"}, limit=1)
        assert len(markets) == 1
        assert markets[0].condition_id != "0x0100"
        assert calls["n"] == 1

    asyncio.run(run())


def test_top_n_ending_within_six_hours_do_not_fill_the_limit() -> None:
    """The first page is entirely inside the horizon. It must not consume `limit`."""
    now = datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc)
    pages = {
        0: [
            _row("0xshort0", "2026-10-04T16:00:00Z"),
            _row("0xshort1", "2026-10-04T06:00:00Z"),
        ],
        2: [
            _row("0xlong0", "2028-11-08T00:00:00Z"),
            _row("0xlong1", "2028-12-01T00:00:00Z"),
        ],
    }
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        offset = int(request.url.params["offset"])
        return httpx.Response(200, json=pages.get(offset, []))

    async def run() -> None:
        config = load_config(Path(__file__).resolve().parents[1] / "config.yaml")
        config = replace(config, max_markets=30, gamma_page_size=2, gamma_max_pages=5)
        skip: set[str] = set()
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport, base_url=config.gamma_base) as http:
            markets = await discover_markets(
                http,
                config,
                skip=skip,
                limit=2,
                now=now,
                exclude_ending_within_s=21600,
            )
            assert [market.condition_id for market in markets] == ["0xlong0", "0xlong1"]
            assert skip == {"0xshort0", "0xshort1"}
            assert calls["n"] == 2
            # The ids are on the skip set, so a later refresh does not select them
            # even if it forgets to pass the horizon again.
            again = await discover_markets(http, config, skip=skip, limit=2)
        assert [market.condition_id for market in again] == ["0xlong0", "0xlong1"]
        assert "0xshort0" in skip and "0xshort1" in skip

    asyncio.run(run())


def _row(condition_id: str, end_date: str) -> dict:
    item = dict(FIXTURE)
    item["conditionId"] = condition_id
    item["id"] = condition_id
    item["endDate"] = end_date
    item["clobTokenIds"] = json.dumps([f"{condition_id}-yes", f"{condition_id}-no"])
    return item
