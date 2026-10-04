"""Gamma market discovery. GET /markets is paginated with limit and offset."""

from __future__ import annotations

import logging

import httpx

from pmbot.config import Config
from pmbot.data.models import ParsedMarket, parse_market
from pmbot.httputil import get_text

log = logging.getLogger(__name__)


async def discover_markets(
    http: httpx.AsyncClient,
    config: Config,
    *,
    skip: set[str] | None = None,
    limit: int | None = None,
) -> list[ParsedMarket]:
    """Walk volume-sorted pages until `limit` new recordable markets are found.

    `skip` holds condition ids already tracked. Those rows do not count toward
    the limit, so a refresh can see past the current set.
    """
    target = config.max_markets if limit is None else limit
    if target <= 0:
        return []
    ignored = skip or set()
    chosen: list[ParsedMarket] = []
    seen: set[str] = set()
    offset = 0
    for page in range(config.gamma_max_pages):
        response = await get_text(
            http,
            "/markets",
            params={
                "closed": "false",
                "active": "true",
                "limit": str(config.gamma_page_size),
                "offset": str(offset),
                "order": config.market_order,
                "ascending": "true" if config.market_order_ascending else "false",
            },
            attempts=config.http_attempts,
        )
        batch = response.json()
        if not isinstance(batch, list):
            raise RuntimeError(f"Gamma /markets returned {type(batch).__name__}, expected a list")
        log.info("gamma page %s offset=%s rows=%s", page, offset, len(batch))
        for item in batch:
            if not isinstance(item, dict):
                continue
            parsed = parse_market(item)
            if parsed is None or parsed.condition_id in seen or parsed.condition_id in ignored:
                continue
            seen.add(parsed.condition_id)
            chosen.append(parsed)
            if len(chosen) >= target:
                log.info("selected %s/%s markets", len(chosen), target)
                return chosen
        if len(batch) < config.gamma_page_size:
            break
        offset += config.gamma_page_size
    log.info("selected %s/%s markets", len(chosen), target)
    return chosen
