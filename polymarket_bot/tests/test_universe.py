"""Diversified universe and neg-risk sibling flags. No network."""

from datetime import datetime, timezone

from pmbot.data.models import parse_market
from pmbot.data.universe import (
    choose_complete_events,
    parse_neg_risk_event,
    select_across_categories,
)


def _market(condition_id: str, title: str, *, other: bool = False, active: bool = True, book: bool = True) -> dict:
    return {
        "conditionId": condition_id,
        "slug": title.lower().replace(" ", "-"),
        "question": title,
        "groupItemTitle": title,
        "active": active,
        "closed": False,
        "enableOrderBook": book,
        "acceptingOrders": True,
        "negRisk": True,
        "negRiskMarketID": "0xabc",
        "negRiskOther": other,
        "outcomes": '["Yes", "No"]',
        "clobTokenIds": f'["{condition_id}-yes", "{condition_id}-no"]',
        "feesEnabled": True,
        "feeType": "politics_fees",
        "endDate": "2028-01-01T00:00:00Z",
        "volume24hr": 10,
    }


def _event(markets: list[dict], *, augmented: bool = True, volume: float = 100) -> dict:
    return {
        "id": "1",
        "slug": "sample-event",
        "title": "Sample",
        "negRisk": True,
        "negRiskAugmented": augmented,
        "negRiskMarketID": "0xabc",
        "endDate": "2028-01-01T00:00:00Z",
        "volume24hr": volume,
        "markets": markets,
    }


def test_placeholder_other_is_flagged_and_the_set_is_complete() -> None:
    event = parse_neg_risk_event(
        _event(
            [
                _market("a", "Alpha"),
                _market("b", "Beta"),
                _market("c", "Other", other=True),
            ]
        )
    )
    assert event is not None and event.complete is True
    assert event.outcome_count == 3
    assert event.augmented is True
    flags = {sibling.group_item_title: (sibling.placeholder, sibling.augmented) for sibling in event.siblings}
    assert flags["Alpha"] == (False, False)
    assert flags["Other"] == (True, True)
    assert event.siblings[0].yes_token_id == "a-yes"


def test_a_sibling_without_a_book_is_not_a_complete_set() -> None:
    event = parse_neg_risk_event(_event([_market("a", "Alpha"), _market("b", "Beta", book=False)]))
    assert event is not None and event.complete is False
    assert event.reason is not None


def test_complete_events_are_kept_whole_or_skipped() -> None:
    small = parse_neg_risk_event(_event([_market("a", "Alpha"), _market("b", "Beta")], volume=50))
    large = parse_neg_risk_event(
        _event([_market("a", "Alpha"), _market("b", "Beta"), _market("c", "Other", other=True)], volume=500)
    )
    assert small is not None and large is not None
    now = datetime(2026, 10, 3, tzinfo=timezone.utc)
    # Cap 2 fits the 2-outcome event and skips the 3-outcome event even though it has more volume.
    chosen = choose_complete_events([large, small], count=4, max_markets=2, now=now, horizon_s=6 * 3600)
    assert [event.outcome_count for event in chosen] == [2]


def test_categories_round_robin_and_skip_markets_ending_within_six_hours() -> None:
    now = datetime(2026, 10, 3, 12, tzinfo=timezone.utc)
    soon = {
        "conditionId": "soon",
        "slug": "soon",
        "question": "Soon",
        "active": True,
        "closed": False,
        "enableOrderBook": True,
        "acceptingOrders": True,
        "outcomes": '["Yes", "No"]',
        "clobTokenIds": '["sy", "sn"]',
        "feeType": "sports_fees_v3",
        "endDate": "2026-10-03T16:00:00Z",
        "volume24hr": 1000,
    }
    rows = []
    for condition_id, fee, volume in (
        ("s1", "sports_fees_v3", 100),
        ("s2", "sports_fees_v3", 80),
        ("p1", "politics_fees", 90),
        ("c1", "crypto_fees_v2", 70),
    ):
        raw = dict(soon)
        raw.update(
            {
                "conditionId": condition_id,
                "slug": condition_id,
                "question": condition_id,
                "feeType": fee,
                "endDate": "2028-01-01T00:00:00Z",
                "volume24hr": volume,
                "clobTokenIds": f'["{condition_id}y", "{condition_id}n"]',
            }
        )
        rows.append(raw)
    rows.append(soon)
    markets = [parsed for row in rows if (parsed := parse_market(row)) is not None]
    chosen = select_across_categories(markets, max_markets=3, now=now, horizon_s=6 * 3600)
    assert [market.condition_id for market in chosen] == ["s1", "p1", "c1"]
    assert "soon" not in {market.condition_id for market in chosen}
