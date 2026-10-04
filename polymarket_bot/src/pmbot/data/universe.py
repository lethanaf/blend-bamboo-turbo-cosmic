"""Recording universe. No orders.

`volume` discovery is unchanged. `diversified` is the 24h set: top volume
across fee categories, markets ending within the horizon left out, plus up
to N complete neg-risk events. A complete event is every sibling Gamma
returned. A larger event than the remaining market cap is skipped, not cut.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone

from pmbot.data.bookbuild import iso_to_ns
from pmbot.data.models import ParsedMarket, parse_market

PLACEHOLDER_TITLES = {"other", "placeholder"}


def category_of(market: ParsedMarket) -> str:
    """One bucket per market. feeType is the category the catalog already stores."""
    if market.fee_type:
        return market.fee_type
    return "other"


def ending_within(end_date: str | None, now: datetime, horizon_s: int) -> bool:
    """True when endDate is missing-or-not: False. True when time-to-end <= horizon.

    Already past counts as ending within the horizon. A missing endDate is kept.
    """
    if not end_date or horizon_s < 0:
        return False
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    try:
        end_ns = iso_to_ns(end_date)
        now_ns = iso_to_ns(now.astimezone(timezone.utc).isoformat())
    except ValueError:
        return False
    return end_ns - now_ns <= horizon_s * 1_000_000_000


def yes_token_id(market: ParsedMarket) -> str | None:
    for token in market.tokens:
        if token.outcome.lower() == "yes":
            return token.token_id
    if market.tokens:
        return market.tokens[0].token_id
    return None


@dataclass(frozen=True)
class Sibling:
    condition_id: str
    slug: str
    question: str
    group_item_title: str
    yes_token_id: str
    placeholder: bool
    augmented: bool
    market: ParsedMarket


@dataclass(frozen=True)
class NegRiskEvent:
    neg_risk_market_id: str
    event_id: str
    slug: str
    title: str
    end_date: str | None
    outcome_count: int
    augmented: bool
    complete: bool
    reason: str | None
    volume_24hr: float | None
    siblings: tuple[Sibling, ...]


def _flags(raw: dict, event_augmented: bool) -> tuple[bool, bool]:
    title = str(raw.get("groupItemTitle") or "")
    placeholder = bool(raw.get("negRiskOther")) or title.strip().lower() in PLACEHOLDER_TITLES
    return placeholder, bool(event_augmented and placeholder)


def parse_neg_risk_event(raw: dict) -> NegRiskEvent | None:
    """One Gamma /events row. None when it is not a neg-risk event with markets."""
    if not isinstance(raw, dict) or not raw.get("negRisk"):
        return None
    markets = raw.get("markets")
    if not isinstance(markets, list) or len(markets) < 2:
        return None
    event_augmented = bool(raw.get("negRiskAugmented"))
    siblings: list[Sibling] = []
    missing: list[str] = []
    nid = raw.get("negRiskMarketID")
    if not isinstance(nid, str) or not nid:
        first = markets[0] if isinstance(markets[0], dict) else {}
        nid = first.get("negRiskMarketID") if isinstance(first, dict) else None
    if not isinstance(nid, str) or not nid:
        return None
    for item in markets:
        if not isinstance(item, dict):
            missing.append("?")
            continue
        if item.get("negRiskMarketID") not in (None, nid):
            missing.append(str(item.get("slug") or item.get("conditionId") or "?"))
            continue
        parsed = parse_market(item, require_active=False)
        placeholder, augmented = _flags(item, event_augmented)
        if parsed is None:
            missing.append(str(item.get("slug") or item.get("conditionId") or "?"))
            continue
        yes = yes_token_id(parsed)
        if yes is None:
            missing.append(parsed.slug)
            continue
        siblings.append(
            Sibling(
                condition_id=parsed.condition_id,
                slug=parsed.slug,
                question=parsed.question,
                group_item_title=str(item.get("groupItemTitle") or ""),
                yes_token_id=yes,
                placeholder=placeholder,
                augmented=augmented,
                market=parsed,
            )
        )
    volume = raw.get("volume24hr")
    try:
        volume_f = None if volume is None else float(volume)
    except (TypeError, ValueError):
        volume_f = None
    end = raw.get("endDate") if isinstance(raw.get("endDate"), str) else None
    complete = not missing and len(siblings) >= 2
    reason = None if complete else "not every sibling is an open book: " + ", ".join(missing[:8])
    return NegRiskEvent(
        neg_risk_market_id=nid,
        event_id=str(raw.get("id") or ""),
        slug=str(raw.get("slug") or ""),
        title=str(raw.get("title") or ""),
        end_date=end,
        outcome_count=len(markets),
        augmented=event_augmented,
        complete=complete,
        reason=reason,
        volume_24hr=volume_f,
        siblings=tuple(siblings),
    )


def choose_complete_events(
    events: list[NegRiskEvent],
    *,
    count: int,
    max_markets: int,
    now: datetime,
    horizon_s: int,
) -> list[NegRiskEvent]:
    """Highest volume first. Skip an event that does not fit whole, or ends too soon."""
    ranked = sorted(
        (event for event in events if event.complete and not ending_within(event.end_date, now, horizon_s)),
        key=lambda event: -(event.volume_24hr or 0),
    )
    chosen: list[NegRiskEvent] = []
    used = 0
    seen: set[str] = set()
    for event in ranked:
        if len(chosen) >= count:
            break
        if event.neg_risk_market_id in seen:
            continue
        if used + event.outcome_count > max_markets:
            continue
        chosen.append(event)
        seen.add(event.neg_risk_market_id)
        used += event.outcome_count
    return chosen


def select_across_categories(
    markets: list[ParsedMarket],
    *,
    max_markets: int,
    now: datetime,
    horizon_s: int,
    skip: set[str] | None = None,
) -> list[ParsedMarket]:
    """Round-robin categories, highest volume inside each category first."""
    ignored = skip or set()
    by_cat: dict[str, list[ParsedMarket]] = defaultdict(list)
    for market in markets:
        if market.condition_id in ignored:
            continue
        if ending_within(market.end_date, now, horizon_s):
            continue
        by_cat[category_of(market)].append(market)
    for rows in by_cat.values():
        rows.sort(key=lambda market: -(market.volume_24hr or 0.0))
    categories = sorted(by_cat, key=lambda name: -(by_cat[name][0].volume_24hr or 0.0))
    chosen: list[ParsedMarket] = []
    index = {name: 0 for name in categories}
    while len(chosen) < max_markets:
        progressed = False
        for name in categories:
            cursor = index[name]
            rows = by_cat[name]
            if cursor >= len(rows):
                continue
            chosen.append(rows[cursor])
            index[name] = cursor + 1
            progressed = True
            if len(chosen) >= max_markets:
                break
        if not progressed:
            break
    return chosen
