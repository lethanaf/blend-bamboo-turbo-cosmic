"""Complete-set edge. No orders.

A binary lock-in buys both tokens of one condition at the ask. The pair pays
1 USDC per share when that condition resolves to one winner. Payoff and
resolution come from Gamma status, not from a market_resolved frame.

Neg-risk multi-outcome (one ask per outcome, summed) pays 1 only when the set
contains every outcome of the negRiskMarketID. A subset is not a lock-in.
The soak catalog's embedded event has no sibling market list and no outcome
count, so those groups are incomplete.

Fees on each leg are one match per price level (the touch, here), from that
market's feeSchedule. `per_order_net` is the sensitivity that rounds the
summed raw fees once.
"""

from __future__ import annotations

import heapq
from decimal import Decimal

from pmbot.data.bookbuild import BookReplay, iso_to_ns, ns_to_iso
from pmbot.data.fill import fee_per_match_vs_per_order

LATENCIES_MS = (100, 250, 500)


def _unit_gross(price_sum: Decimal, side: str) -> Decimal:
    if side == "buy":
        return Decimal(1) - price_sum
    if side == "sell":
        return price_sum - Decimal(1)
    raise ValueError("side must be buy or sell")


def set_edge(
    levels: list[tuple[Decimal, Decimal]],
    fee_schedule: dict | None,
    *,
    fees_enabled: bool | None,
    min_size: Decimal,
    side: str = "buy",
    rounding: str = "half-up",
    leg_fees: list[tuple[dict | None, bool | None]] | None = None,
) -> dict | None:
    """Edge of one complete set at the touch.

    `buy` lifts every ask. Gross per share is `1 - sum(asks)`.
    `sell` mints one set for 1 USDC and hits every bid. Gross per share is
    `sum(bids) - 1`. Fees are taker fees, one match per leg. `per_order_net`
    rounds the summed raw fees once and is the sensitivity, not the net.
    `leg_fees` charges each leg from its own market when a neg-risk set spans
    more than one feeSchedule. Omit it and every leg uses `fee_schedule`.
    """
    if side not in ("buy", "sell"):
        raise ValueError("side must be buy or sell")
    if len(levels) < 2 or any(size <= 0 for _price, size in levels):
        return None
    if leg_fees is not None and len(leg_fees) != len(levels):
        raise ValueError("leg_fees must match levels")
    size = min(size for _price, size in levels)
    prices = [price for price, _size in levels]
    price_sum = sum(prices, Decimal(0))
    gross = size * _unit_gross(price_sum, side)
    if size < min_size:
        return {
            "executable": False,
            "reason": "below_min_size",
            "size": size,
            "min_size": min_size,
            "gross": gross,
            "price_sum": price_sum,
            "side": side,
        }
    sens = fee_per_match_vs_per_order(
        [(size, price) for price in prices],
        fee_schedule,
        fees_enabled=fees_enabled,
        rounding=rounding,
        leg_fees=leg_fees,
    )
    fees = sens["per_match"]
    net = gross - fees
    if side == "buy":
        cost = size * price_sum + fees
    else:
        cost = size + fees
    return {
        "executable": True,
        "reason": None,
        "side": side,
        "size": size,
        "prices": prices,
        "price_sum": price_sum,
        "gross": gross,
        "fees": fees,
        "net": net,
        "cost": cost,
        "per_match_fee": sens["per_match"],
        "per_order_fee": sens["per_order"],
        "per_order_net": gross - sens["per_order"],
        "fee_rounding_differs": sens["differ"],
    }


def binary_edge(
    ask_a: Decimal,
    size_a: Decimal,
    ask_b: Decimal,
    size_b: Decimal,
    fee_schedule: dict | None,
    *,
    fees_enabled: bool | None,
    min_size: Decimal,
    rounding: str = "half-up",
    side: str = "buy",
) -> dict | None:
    """Two-leg complete set. `side="sell"` mints and sells both bids.

    The price arguments are asks on a buy and bids on a sell.
    """
    edge = set_edge(
        [(ask_a, size_a), (ask_b, size_b)],
        fee_schedule,
        fees_enabled=fees_enabled,
        min_size=min_size,
        side=side,
        rounding=rounding,
    )
    if edge is None:
        return None
    if edge["executable"]:
        edge["ask_a"] = ask_a
        edge["ask_b"] = ask_b
    return edge


def return_per_day(net: Decimal, cost: Decimal, decision_ns: int, end_ns: int | None) -> Decimal | None:
    """net/cost divided by days until endDate. None when endDate is missing or already past."""
    if end_ns is None or cost <= 0:
        return None
    days = Decimal(end_ns - decision_ns) / Decimal(86_400_000_000_000)
    if days <= 0:
        return None
    return (net / cost) / days


def neg_risk_group_report(
    members: list[dict],
    *,
    outcome_count: int | None = None,
    sibling_condition_ids: list[str] | None = None,
) -> dict:
    """A negRiskMarketID group.

    Without a sibling list and an outcome count the catalog cannot show the
    set is complete, and it is not a lock-in. With both, it is complete only
    when the stored condition ids are exactly that sibling list.
    """
    if outcome_count is None or sibling_condition_ids is None:
        complete = False
        reason = "catalog event payload has no sibling market list and no outcome count"
    else:
        have = {str(member.get("condition_id") or "") for member in members}
        want = {str(item) for item in sibling_condition_ids}
        complete = bool(want) and have == want and len(want) == outcome_count and outcome_count >= 2
        reason = None if complete else "sibling list does not match the markets on hand"
    return {
        "conditions": len(members),
        "complete": complete,
        "counted_as_lockin": complete,
        "outcome_count": outcome_count,
        "reason": reason,
        "slugs": [str(member.get("slug") or "") for member in members],
        "questions": [str(member.get("question") or "") for member in members],
    }


def _blocked(view: dict) -> str | None:
    if view.get("ended"):
        return "ended"
    if view.get("gap_frozen"):
        return "gap_frozen"
    if not view.get("anchored"):
        return "unanchored"
    bids = view["bids"]
    asks = view["asks"]
    if bids.best is not None and asks.best is not None and bids.best >= asks.best:
        return "crossed"
    return None


def touch_edge(
    views: list[dict],
    fee_schedule: dict | None,
    *,
    fees_enabled: bool | None,
    min_size: Decimal,
    side: str = "buy",
    leg_fees: list[tuple[dict | None, bool | None]] | None = None,
) -> dict:
    """Complete-set touch. Buy lifts asks. Sell mints and hits bids.

    The first failing leg wins, in list order. Inside a leg the order is
    ended, gap_frozen, unanchored, crossed, then a missing touch.
    """
    book_name = "asks" if side == "buy" else "bids"
    missing = "no_ask" if side == "buy" else "no_bid"
    labels = ["a", "b"] if len(views) == 2 else [str(index) for index in range(len(views))]
    for label, view in zip(labels, views):
        reason = _blocked(view)
        if reason:
            return {"status": "blocked", "reason": f"{label}_{reason}"}
    levels: list[tuple[Decimal, Decimal]] = []
    for label, view in zip(labels, views):
        book = view[book_name]
        if book.best is None or book.best_size is None or book.best_size <= 0:
            return {"status": "blocked", "reason": f"{label}_{missing}"}
        levels.append((book.best, book.best_size))
    edge = set_edge(
        levels,
        fee_schedule,
        fees_enabled=fees_enabled,
        min_size=min_size,
        side=side,
        leg_fees=leg_fees,
    )
    if edge is None:
        return {"status": "blocked", "reason": missing}
    edge["price_sum"] = edge["price_sum"]
    if side == "buy":
        edge["ask_sum"] = edge["price_sum"]
    if len(levels) == 2 and edge["executable"]:
        edge["ask_a"] = levels[0][0]
        edge["ask_b"] = levels[1][0]
    if not edge["executable"]:
        return {
            "status": "below_min_size",
            "gross": edge["gross"],
            "size": edge["size"],
            "price_sum": edge["price_sum"],
            "ask_sum": edge.get("ask_sum"),
        }
    if edge["gross"] <= 0:
        return {
            "status": "gross_nonpositive",
            "gross": edge["gross"],
            "net": edge["net"],
            "size": edge["size"],
            "price_sum": edge["price_sum"],
            "ask_sum": edge.get("ask_sum"),
        }
    if edge["net"] <= 0:
        return {"status": "fee_killed", **edge}
    return {"status": "net_positive", **edge}


def pair_edge(
    view_a: dict,
    view_b: dict,
    fee_schedule: dict | None,
    *,
    fees_enabled: bool | None,
    min_size: Decimal,
    side: str = "buy",
) -> dict:
    """Two-token touch. `side="sell"` is mint-and-sell both bids."""
    return touch_edge(
        [view_a, view_b],
        fee_schedule,
        fees_enabled=fees_enabled,
        min_size=min_size,
        side=side,
    )


def _times(replay: BookReplay, *tokens: str) -> list[tuple[int, str]]:
    best: dict[int, str] = {}
    for token in tokens:
        for mut in replay.muts.get(token, ()):
            wall = mut[-1]
            if not isinstance(wall, str):
                continue
            stamp = iso_to_ns(wall)
            # Same nanosecond: either wall string round-trips to this stamp.
            best.setdefault(stamp, wall)
    return sorted(best.items())


def scan_binary_pair(
    replay: BookReplay,
    token_a: str,
    token_b: str,
    *,
    fee_schedule: dict | None,
    fees_enabled: bool | None,
    min_size: Decimal,
    end_ns: int | None,
    latencies_ms: tuple[int, ...] = LATENCIES_MS,
    side: str = "buy",
    tokens: list[str] | None = None,
    leg_fees: list[tuple[dict | None, bool | None]] | None = None,
) -> dict:
    """Maximal net>0 intervals on the arrival book, then a shot at each latency.

    `buy` lifts both asks. `sell` mints a pair and hits both bids. The book at
    time T is `book_asof` (tape order). A latency check is the arrival book at
    open+L. Defer is the other same-server-ms ordering. Queries only move forward.

    One shot per window: touch size at the open, or the arrival touch at
    open+L. This does not sum every tick.
    """
    if side not in ("buy", "sell"):
        raise ValueError("side must be buy or sell")
    legs = [token_a, token_b] if tokens is None else list(tokens)
    if len(legs) < 2:
        raise ValueError("a complete set needs at least two tokens")
    if leg_fees is not None and len(leg_fees) != len(legs):
        raise ValueError("leg_fees must match tokens")
    times = _times(replay, *legs)
    windows: list[dict] = []
    fee_killed_intervals = 0
    fee_killed_open_gross = Decimal("0")
    blocked: dict[str, int] = {}
    evaluations = 0
    min_ask_sum: Decimal | None = None
    max_bid_sum: Decimal | None = None
    below_min_positive = 0
    below_min_positive_gross = Decimal("0")
    kind: str | None = None
    open_ns: int | None = None
    checks: list[tuple[int, int, int, int]] = []
    seq = 0

    def edge_at(wall: str) -> tuple[dict, dict]:
        tape_views = []
        defer_views = []
        for token in legs:
            both = replay.book_asof(token, wall, same_ms="both")
            tape_views.append(both["tape"])
            defer_views.append(both["defer_same_ms"])
        tape = touch_edge(
            tape_views,
            fee_schedule,
            fees_enabled=fees_enabled,
            min_size=min_size,
            side=side,
            leg_fees=leg_fees,
        )
        defer = touch_edge(
            defer_views,
            fee_schedule,
            fees_enabled=fees_enabled,
            min_size=min_size,
            side=side,
            leg_fees=leg_fees,
        )
        return tape, defer

    def close(at_ns: int, *, censored: bool) -> None:
        nonlocal kind, open_ns, fee_killed_intervals
        if kind == "net" and open_ns is not None:
            windows[-1]["close_ns"] = at_ns
            windows[-1]["duration_ns"] = at_ns - open_ns
            windows[-1]["right_censored"] = censored
        elif kind == "fee":
            fee_killed_intervals += 1
        kind = None
        open_ns = None

    def note_blocked(status: dict) -> None:
        if status["status"] == "blocked":
            reason = str(status.get("reason") or "blocked")
            blocked[reason] = blocked.get(reason, 0) + 1
        elif status["status"] in ("below_min_size", "gross_nonpositive", "fee_killed"):
            blocked[status["status"]] = blocked.get(status["status"], 0) + 1

    index = 0
    while index < len(times) or checks:
        next_ns = times[index][0] if index < len(times) else None
        if checks and (next_ns is None or checks[0][0] < next_ns):
            due, _seq, widx, latency_ms = heapq.heappop(checks)
            tape, defer = edge_at(ns_to_iso(due))
            evaluations += 1
            row = windows[widx]["latency"][str(latency_ms)]
            row["status"] = tape["status"]
            row["survived"] = tape["status"] == "net_positive"
            row["defer_status"] = defer["status"]
            row["defer_survived"] = defer["status"] == "net_positive"
            if tape["status"] == "net_positive":
                row["net"] = tape["net"]
                row["size"] = tape["size"]
                row["cost"] = tape["cost"]
                row["per_order_net"] = tape["per_order_net"]
                row["return_per_day"] = return_per_day(tape["net"], tape["cost"], due, end_ns)
            if defer["status"] == "net_positive":
                row["defer_net"] = defer["net"]
            continue
        if next_ns is None:
            break
        stamp, wall = times[index]
        index += 1
        tape, defer = edge_at(wall)
        evaluations += 1
        note_blocked(tape)
        price_sum = tape.get("price_sum")
        if isinstance(price_sum, Decimal):
            if side == "buy" and (min_ask_sum is None or price_sum < min_ask_sum):
                min_ask_sum = price_sum
            if side == "sell" and (max_bid_sum is None or price_sum > max_bid_sum):
                max_bid_sum = price_sum
        if tape["status"] == "below_min_size" and tape.get("gross", 0) > 0:
            below_min_positive += 1
            below_min_positive_gross += tape["gross"]
        if tape["status"] == "net_positive":
            if kind != "net":
                close(stamp, censored=False)
                kind = "net"
                open_ns = stamp
                window = {
                    "open_ns": stamp,
                    "close_ns": None,
                    "duration_ns": None,
                    "right_censored": False,
                    "side": side,
                    "size": tape["size"],
                    "ask_a": tape.get("ask_a"),
                    "ask_b": tape.get("ask_b"),
                    "price_sum": tape.get("price_sum"),
                    "gross": tape["gross"],
                    "fees": tape["fees"],
                    "net": tape["net"],
                    "cost": tape["cost"],
                    "per_order_net": tape["per_order_net"],
                    "fee_rounding_differs": tape["fee_rounding_differs"],
                    "defer_status": defer["status"],
                    "defer_net": defer["net"] if defer["status"] == "net_positive" else None,
                    "return_per_day": return_per_day(tape["net"], tape["cost"], stamp, end_ns),
                    "latency": {
                        str(latency): {
                            "survived": False,
                            "defer_survived": False,
                            "status": None,
                            "defer_status": None,
                            "net": None,
                            "defer_net": None,
                            "size": None,
                            "cost": None,
                            "per_order_net": None,
                            "return_per_day": None,
                        }
                        for latency in latencies_ms
                    },
                }
                windows.append(window)
                widx = len(windows) - 1
                for latency in latencies_ms:
                    seq += 1
                    heapq.heappush(checks, (stamp + latency * 1_000_000, seq, widx, latency))
            continue
        if tape["status"] == "fee_killed":
            if kind != "fee":
                close(stamp, censored=False)
                kind = "fee"
                open_ns = stamp
                fee_killed_open_gross += tape["gross"]
            continue
        if kind is not None:
            close(stamp, censored=False)

    if times and kind is not None:
        close(times[-1][0], censored=True)

    return {
        "windows": windows,
        "fee_killed_intervals": fee_killed_intervals,
        "fee_killed_open_gross": fee_killed_open_gross,
        "evaluations": evaluations,
        "blocked": blocked,
        "min_ask_sum": min_ask_sum,
        "max_bid_sum": max_bid_sum,
        "below_min_positive": below_min_positive,
        "below_min_positive_gross": below_min_positive_gross,
        "side": side,
    }


def scan_outcome_set(
    replay: BookReplay,
    tokens: list[str],
    *,
    fee_schedule: dict | None,
    fees_enabled: bool | None,
    min_size: Decimal,
    end_ns: int | None,
    latencies_ms: tuple[int, ...] = LATENCIES_MS,
    side: str = "buy",
    leg_fees: list[tuple[dict | None, bool | None]] | None = None,
) -> dict:
    """Same windows as `scan_binary_pair`, for every YES token of a complete set."""
    return scan_binary_pair(
        replay,
        tokens[0],
        tokens[1],
        fee_schedule=fee_schedule,
        fees_enabled=fees_enabled,
        min_size=min_size,
        end_ns=end_ns,
        latencies_ms=latencies_ms,
        side=side,
        tokens=tokens,
        leg_fees=leg_fees,
    )
