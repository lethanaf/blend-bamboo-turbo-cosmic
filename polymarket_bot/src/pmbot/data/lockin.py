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
from pmbot.data.fill import fee_per_match_vs_per_order, taker_fee

LATENCIES_MS = (100, 250, 500)


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
) -> dict | None:
    """Edge of buying the touch on both legs. None if the touch is not executable.

    Fees are per leg, each rounded as its own match. `per_order_net` rounds
    the summed raw fees once. That figure is the sensitivity, not the net.
    """
    if size_a <= 0 or size_b <= 0:
        return None
    size = size_a if size_a < size_b else size_b
    if size < min_size:
        return {
            "executable": False,
            "reason": "below_min_size",
            "size": size,
            "min_size": min_size,
            "gross": size * (Decimal(1) - ask_a - ask_b),
        }
    gross = size * (Decimal(1) - ask_a - ask_b)
    fee_a = taker_fee(size, ask_a, fee_schedule, fees_enabled=fees_enabled, rounding=rounding)
    fee_b = taker_fee(size, ask_b, fee_schedule, fees_enabled=fees_enabled, rounding=rounding)
    fees = fee_a + fee_b
    net = gross - fees
    sens = fee_per_match_vs_per_order(
        [(size, ask_a), (size, ask_b)],
        fee_schedule,
        fees_enabled=fees_enabled,
        rounding=rounding,
    )
    cost = size * (ask_a + ask_b) + fees
    return {
        "executable": True,
        "reason": None,
        "size": size,
        "ask_a": ask_a,
        "ask_b": ask_b,
        "gross": gross,
        "fees": fees,
        "net": net,
        "cost": cost,
        "per_match_fee": sens["per_match"],
        "per_order_fee": sens["per_order"],
        "per_order_net": gross - sens["per_order"],
        "fee_rounding_differs": sens["differ"],
    }


def return_per_day(net: Decimal, cost: Decimal, decision_ns: int, end_ns: int | None) -> Decimal | None:
    """net/cost divided by days until endDate. None when endDate is missing or already past."""
    if end_ns is None or cost <= 0:
        return None
    days = Decimal(end_ns - decision_ns) / Decimal(86_400_000_000_000)
    if days <= 0:
        return None
    return (net / cost) / days


def neg_risk_group_report(members: list[dict]) -> dict:
    """A negRiskMarketID group from the catalog.

    Completeness is not knowable here: the stored event payload has no
    sibling market list and no outcome count. The group is not a lock-in.
    Binary Yes+No of one condition is a separate trade.
    """
    return {
        "conditions": len(members),
        "complete": False,
        "counted_as_lockin": False,
        "reason": "catalog event payload has no sibling market list and no outcome count",
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


def pair_edge(
    view_a: dict,
    view_b: dict,
    fee_schedule: dict | None,
    *,
    fees_enabled: bool | None,
    min_size: Decimal,
) -> dict:
    """Touch lock-in on the two views. Status is the only decision."""
    for label, view in (("a", view_a), ("b", view_b)):
        reason = _blocked(view)
        if reason:
            return {"status": "blocked", "reason": f"{label}_{reason}"}
    ask_a = view_a["asks"]
    ask_b = view_b["asks"]
    if ask_a.best is None or ask_a.best_size is None or ask_a.best_size <= 0:
        return {"status": "blocked", "reason": "a_no_ask"}
    if ask_b.best is None or ask_b.best_size is None or ask_b.best_size <= 0:
        return {"status": "blocked", "reason": "b_no_ask"}
    ask_sum = ask_a.best + ask_b.best
    edge = binary_edge(
        ask_a.best,
        ask_a.best_size,
        ask_b.best,
        ask_b.best_size,
        fee_schedule,
        fees_enabled=fees_enabled,
        min_size=min_size,
    )
    if edge is None:
        return {"status": "blocked", "reason": "no_ask"}
    edge["ask_sum"] = ask_sum
    if not edge["executable"]:
        return {"status": "below_min_size", "gross": edge["gross"], "size": edge["size"], "ask_sum": ask_sum}
    if edge["gross"] <= 0:
        return {
            "status": "gross_nonpositive",
            "gross": edge["gross"],
            "net": edge["net"],
            "size": edge["size"],
            "ask_sum": ask_sum,
        }
    if edge["net"] <= 0:
        return {"status": "fee_killed", **edge}
    return {"status": "net_positive", **edge}


def _times(replay: BookReplay, token_a: str, token_b: str) -> list[tuple[int, str]]:
    best: dict[int, str] = {}
    for token in (token_a, token_b):
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
) -> dict:
    """Maximal net>0 intervals on the arrival book, then a shot at each latency.

    The book at time T is `book_asof` (tape order). A latency check is the
    arrival book at open+L, including events whose recv time is <= that instant.
    Defer is the other same-server-ms ordering (the book from before that
    group). Queries only move the cursors forward.

    One shot per window: touch size at the open, or the arrival touch at
    open+L. This does not sum every tick.
    """
    times = _times(replay, token_a, token_b)
    windows: list[dict] = []
    fee_killed_intervals = 0
    fee_killed_open_gross = Decimal("0")
    blocked: dict[str, int] = {}
    evaluations = 0
    min_ask_sum: Decimal | None = None
    below_min_positive = 0
    below_min_positive_gross = Decimal("0")
    kind: str | None = None
    open_ns: int | None = None
    checks: list[tuple[int, int, int, int]] = []
    seq = 0

    def edge_at(wall: str) -> tuple[dict, dict]:
        both_a = replay.book_asof(token_a, wall, same_ms="both")
        both_b = replay.book_asof(token_b, wall, same_ms="both")
        tape = pair_edge(
            both_a["tape"], both_b["tape"], fee_schedule, fees_enabled=fees_enabled, min_size=min_size
        )
        defer = pair_edge(
            both_a["defer_same_ms"],
            both_b["defer_same_ms"],
            fee_schedule,
            fees_enabled=fees_enabled,
            min_size=min_size,
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
        ask_sum = tape.get("ask_sum")
        if isinstance(ask_sum, Decimal) and (min_ask_sum is None or ask_sum < min_ask_sum):
            min_ask_sum = ask_sum
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
                    "size": tape["size"],
                    "ask_a": tape["ask_a"],
                    "ask_b": tape["ask_b"],
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
        "below_min_positive": below_min_positive,
        "below_min_positive_gross": below_min_positive_gross,
    }
