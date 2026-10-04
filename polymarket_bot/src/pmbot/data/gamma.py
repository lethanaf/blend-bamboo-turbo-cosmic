"""Gamma market discovery. GET /markets is paginated with limit and offset."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import httpx

from pmbot.config import Config
from pmbot.data.models import ParsedMarket, parse_market
from pmbot.data.universe import (
    NegRiskEvent,
    choose_complete_events,
    ending_within,
    parse_neg_risk_event,
    select_across_categories,
)
from pmbot.httputil import get_text

log = logging.getLogger(__name__)


async def discover_markets(
    http: httpx.AsyncClient,
    config: Config,
    *,
    skip: set[str] | None = None,
    limit: int | None = None,
    now: datetime | None = None,
    exclude_ending_within_s: int | None = None,
) -> list[ParsedMarket]:
    """Walk volume-sorted pages until `limit` new recordable markets are found.

    `skip` holds condition ids already tracked. Those rows do not count toward
    the limit, so a refresh can see past the current set. When
    `exclude_ending_within_s` is set, markets that end inside that horizon are
    added to `skip` and do not count toward the limit either. Filtering after
    the limit would drop a whole page of short-dated markets and refetch them
    on the next refresh.
    """
    target = config.max_markets if limit is None else limit
    if target <= 0:
        return []
    ignored = skip if skip is not None else set()
    chosen: list[ParsedMarket] = []
    seen: set[str] = set()
    dropped = 0
    offset = 0

    def finish() -> list[ParsedMarket]:
        if dropped:
            log.info(
                "skipped %s markets ending within %ss",
                dropped,
                exclude_ending_within_s,
            )
        log.info("selected %s/%s markets", len(chosen), target)
        return chosen

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
            if (
                exclude_ending_within_s is not None
                and now is not None
                and ending_within(parsed.end_date, now, exclude_ending_within_s)
            ):
                ignored.add(parsed.condition_id)
                dropped += 1
                continue
            seen.add(parsed.condition_id)
            chosen.append(parsed)
            if len(chosen) >= target:
                return finish()
        if len(batch) < config.gamma_page_size:
            break
        offset += config.gamma_page_size
    return finish()


async def _pages(
    http: httpx.AsyncClient,
    config: Config,
    path: str,
) -> list[dict]:
    rows: list[dict] = []
    offset = 0
    for page in range(config.gamma_max_pages):
        response = await get_text(
            http,
            path,
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
            raise RuntimeError(f"Gamma {path} returned {type(batch).__name__}, expected a list")
        log.info("gamma %s page %s offset=%s rows=%s", path, page, offset, len(batch))
        rows.extend(item for item in batch if isinstance(item, dict))
        if len(batch) < config.gamma_page_size:
            break
        offset += config.gamma_page_size
    return rows


async def discover_diversified(
    http: httpx.AsyncClient,
    config: Config,
    *,
    now: datetime | None = None,
) -> tuple[list[ParsedMarket], list[NegRiskEvent]]:
    """24h universe: complete neg-risk events, then top volume across categories.

    Markets whose endDate is inside `exclude_ending_within_s` are left out of
    both pieces. A complete event that does not fit in `max_markets` is skipped
    whole. Sibling markets of a chosen event are all included.
    """
    moment = now or datetime.now(timezone.utc)
    event_rows = await _pages(http, config, "/events")
    parsed_events = [event for row in event_rows if (event := parse_neg_risk_event(row)) is not None]
    chosen_events = choose_complete_events(
        parsed_events,
        count=config.neg_risk_event_count,
        max_markets=config.max_markets,
        now=moment,
        horizon_s=config.exclude_ending_within_s,
    )
    markets: list[ParsedMarket] = []
    seen: set[str] = set()
    for event in chosen_events:
        for sibling in event.siblings:
            if sibling.condition_id in seen:
                continue
            seen.add(sibling.condition_id)
            markets.append(sibling.market)
    market_rows = await _pages(http, config, "/markets")
    pool: list[ParsedMarket] = []
    for row in market_rows:
        parsed = parse_market(row)
        if parsed is None or parsed.condition_id in seen:
            continue
        pool.append(parsed)
    rest = select_across_categories(
        pool,
        max_markets=config.max_markets - len(markets),
        now=moment,
        horizon_s=config.exclude_ending_within_s,
        skip=seen,
    )
    for market in rest:
        if market.condition_id in seen:
            continue
        seen.add(market.condition_id)
        markets.append(market)
    log.info(
        "diversified markets=%s events=%s (complete candidates=%s)",
        len(markets),
        len(chosen_events),
        sum(1 for event in parsed_events if event.complete),
    )
    return markets, chosen_events
