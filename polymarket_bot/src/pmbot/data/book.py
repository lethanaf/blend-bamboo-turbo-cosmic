"""Top of book.

The 2026-10-03 probe showed bids sorted ascending and asks descending, so
index 0 is not the best price. Docs do not specify the order. Use max bid
and min ask, and keep the original price strings.
"""

from __future__ import annotations


def _levels(levels: object) -> list[tuple[float, str, str]]:
    if not isinstance(levels, list):
        return []
    parsed: list[tuple[float, str, str]] = []
    for level in levels:
        if not isinstance(level, dict) or "price" not in level:
            continue
        price = level.get("price")
        try:
            numeric = float(price)
        except (TypeError, ValueError):
            continue
        size = level.get("size", "")
        parsed.append((numeric, str(price), "" if size is None else str(size)))
    return parsed


def top_of_book(book: dict) -> dict[str, str | int | None]:
    bids = _levels(book.get("bids"))
    asks = _levels(book.get("asks"))
    best_bid = max(bids, key=lambda item: item[0]) if bids else None
    best_ask = min(asks, key=lambda item: item[0]) if asks else None
    bid_count = len(book.get("bids") or []) if isinstance(book.get("bids"), list) else 0
    ask_count = len(book.get("asks") or []) if isinstance(book.get("asks"), list) else 0
    return {
        "best_bid": None if best_bid is None else best_bid[1],
        "best_ask": None if best_ask is None else best_ask[1],
        "best_bid_size": None if best_bid is None else best_bid[2],
        "best_ask_size": None if best_ask is None else best_ask[2],
        "bid_levels": bid_count,
        "ask_levels": ask_count,
    }
