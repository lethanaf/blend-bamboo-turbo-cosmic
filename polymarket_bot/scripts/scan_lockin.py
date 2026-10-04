#!/usr/bin/env python3
"""Scan the soak tape for complete-set lock-ins. Does not send orders.

Binary: buy both tokens of one condition at the ask. Net is
1 - (ask_a + ask_b), minus taker fees on both legs, from that market's
feeSchedule. Neg-risk multi-outcome groups are reported and not summed
unless the catalog proves the set is complete. It does not.
"""

from __future__ import annotations

import json
import sys
import time
from collections import defaultdict
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from pmbot.data.bookbuild import BookReplay, iso_to_ns  # noqa: E402
from pmbot.data.fill import caveat_from_report  # noqa: E402
from pmbot.data.lockin import LATENCIES_MS, neg_risk_group_report, scan_binary_pair  # noqa: E402
from replay_book import _load_gamma, replay_dir  # noqa: E402

UNMODELED = (
    "gas",
    "queue position",
    "rejects",
    "one leg filling and the other missing",
    "walking past the touch",
    "a true per-match fee versus one fee per visible price level",
    "voids (the gamma file has no void flag)",
    "rebates",
)

DURATION_BINS = (
    (0, 0, "0s"),
    (0, 0.1, "0-100ms"),
    (0.1, 0.5, "100-500ms"),
    (0.5, 1, "0.5-1s"),
    (1, 5, "1-5s"),
    (5, 30, "5-30s"),
    (30, 300, "30s-5min"),
    (300, 10**9, ">5min"),
)


def _jsonable(value):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def _pct(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * fraction))))
    return ordered[index]


def _duration_hist(seconds: list[float]) -> dict:
    counts = {label: 0 for *_, label in DURATION_BINS}
    for value in seconds:
        for lo, hi, label in DURATION_BINS:
            if label == "0s":
                if value == 0:
                    counts[label] += 1
                    break
                continue
            if lo < value <= hi:
                counts[label] += 1
                break
    return {
        "bins": counts,
        "min_s": None if not seconds else min(seconds),
        "p50_s": _pct(seconds, 0.50),
        "p90_s": _pct(seconds, 0.90),
        "max_s": None if not seconds else max(seconds),
    }


def _load_markets(path: Path) -> tuple[list[dict], list[dict]]:
    import sqlite3

    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    markets = []
    groups: dict[str, list[dict]] = defaultdict(list)
    for row in con.execute(
        """
        select condition_id, slug, question, end_date, neg_risk, fees_enabled,
               fee_schedule_json, order_min_size, raw_json
        from markets
        """
    ):
        raw = json.loads(row["raw_json"])
        schedule = json.loads(row["fee_schedule_json"]) if row["fee_schedule_json"] else None
        tokens = list(
            con.execute(
                """
                select token_id, outcome, outcome_index
                from tokens
                where condition_id = ?
                order by outcome_index
                """,
                (row["condition_id"],),
            )
        )
        market = {
            "condition_id": row["condition_id"],
            "slug": row["slug"],
            "question": row["question"],
            "end_date": row["end_date"],
            "neg_risk": bool(row["neg_risk"]),
            "fees_enabled": row["fees_enabled"],
            "fee_schedule": schedule,
            "min_size": Decimal(str(row["order_min_size"] or "0")),
            "tokens": [
                {"token_id": token["token_id"], "outcome": token["outcome"], "outcome_index": token["outcome_index"]}
                for token in tokens
            ],
            "neg_risk_market_id": raw.get("negRiskMarketID") if isinstance(raw.get("negRiskMarketID"), str) else None,
        }
        markets.append(market)
        if market["neg_risk"] and market["neg_risk_market_id"]:
            groups[market["neg_risk_market_id"]].append(market)
    group_reports = []
    for group_id, members in groups.items():
        report = neg_risk_group_report(members)
        report["neg_risk_market_id"] = group_id
        group_reports.append(report)
    return markets, group_reports


def _gamma_pair(meta: dict, token_a: str, token_b: str) -> str:
    left = meta.get(token_a, {})
    right = meta.get(token_b, {})
    status_a = left.get("gamma_status") if isinstance(left, dict) else None
    status_b = right.get("gamma_status") if isinstance(right, dict) else None
    if status_a == "resolved" and status_b == "resolved":
        return "resolved"
    if status_a == "open" and status_b == "open":
        return "open"
    if status_a is None or status_b is None:
        return "unknown"
    return "mixed"


def _end_ns(value: str | None) -> int | None:
    if not value:
        return None
    try:
        return iso_to_ns(value)
    except ValueError:
        return None


def benchmark(replay: BookReplay, queries: int = 10_000) -> dict:
    rows: list[tuple[int, str, str]] = []
    for token, muts in replay.muts.items():
        for mut in muts:
            wall = mut[-1]
            if isinstance(wall, str):
                rows.append((iso_to_ns(wall), token, wall))
    rows.sort()
    if len(rows) > queries:
        step = len(rows) / queries
        picked = [rows[min(len(rows) - 1, int(i * step))] for i in range(queries)]
    else:
        picked = rows
    replay.reset_cursors()
    started = time.perf_counter()
    for _stamp, token, wall in picked:
        replay.book_asof(token, wall)
    elapsed = time.perf_counter() - started
    clamped = sum(cursor.clamped for cursor in replay._cursors.values())
    replay.reset_cursors()
    return {
        "queries": len(picked),
        "seconds": elapsed,
        "per_query_us": None if not picked else elapsed / len(picked) * 1_000_000,
        "clock": "recv_wall",
        "recv_wall_clamped": clamped,
    }


def _add(total, value: Decimal | None) -> Decimal:
    if value is None:
        return total
    return total + value


def main() -> None:
    books = ROOT / "data" / "soak" / "books"
    gamma_path = ROOT / "data" / "replay" / "gamma_status.json"
    catalog = ROOT / "data" / "soak" / "catalog.sqlite"
    out = ROOT / "data" / "replay" / "lockin_scan.json"
    replay = BookReplay(keep_quote_rows=False, score_levels=False)
    _load_gamma(gamma_path, replay)
    started = time.perf_counter()
    report = replay_dir(books, replay)
    replay_seconds = time.perf_counter() - started
    quote = report["quote_tape_order"]
    if quote.get("checked") != 1_766_236 or quote.get("mismatch") != 31_268:
        raise SystemExit(f"quote score moved: {quote}")
    if report.get("dropped_duplicate_ws") != 722 or report.get("crossed_after_apply") != 982:
        raise SystemExit(
            f"tape counters moved: dupes={report.get('dropped_duplicate_ws')} "
            f"crossed={report.get('crossed_after_apply')}"
        )
    tie = report["quote_tie"]
    clock = report["recv_clock"]
    token_backward = report["token_recv_wall_backward"]
    caveat = caveat_from_report(report)
    print(f"replay_seconds={replay_seconds:.3f} score_levels=false")
    print("quote_tie", json.dumps(tie, sort_keys=True))
    print("recv_clock", json.dumps(clock, sort_keys=True))
    print(f"token_recv_wall_backward={token_backward}")
    print(caveat)
    if clock["recv_monotonic_ns_backward"] != 0:
        print(
            "recv_monotonic_ns also steps backward inside a session "
            f"({clock['recv_monotonic_ns_backward']} times). It is not a usable fallback. "
            "The cursor stays on recv_wall and clamps a backward stamp to the previous one."
        )
    bench = benchmark(replay, 10_000)
    print(
        f"benchmark queries={bench['queries']} seconds={bench['seconds']:.3f} "
        f"per_query_us={bench['per_query_us']:.1f} clock={bench['clock']} "
        f"clamped_on_sampled_tokens={bench['recv_wall_clamped']}"
    )
    meta = json.loads(gamma_path.read_text(encoding="utf-8"))
    resolved_tokens = [token for token, info in meta.items() if isinstance(info, dict) and info.get("gamma_status") == "resolved"]
    with_frame = sum(1 for token in resolved_tokens if token in replay.resolved_recv_wall)
    print(
        f"gamma_resolved_tokens={len(resolved_tokens)} "
        f"with_market_resolved_frame={with_frame} "
        f"without_frame={len(resolved_tokens) - with_frame}"
    )
    markets, groups = _load_markets(catalog)
    replay.reset_cursors()
    market_rows = []
    skipped = {"mixed": 0, "unknown": 0, "not_binary": 0}
    all_windows: list[dict] = []
    fee_intervals = 0
    fee_gross = Decimal("0")
    blocked_totals: dict[str, int] = defaultdict(int)
    evaluations = 0
    min_ask_sum: Decimal | None = None
    below_min_positive = 0
    below_min_positive_gross = Decimal("0")
    for market in markets:
        tokens = market["tokens"]
        if len(tokens) != 2:
            skipped["not_binary"] += 1
            continue
        token_a = tokens[0]["token_id"]
        token_b = tokens[1]["token_id"]
        gamma = _gamma_pair(meta, token_a, token_b)
        if gamma not in ("resolved", "open"):
            skipped[gamma] += 1
            continue
        end_ns = _end_ns(market["end_date"])
        scanned = scan_binary_pair(
            replay,
            token_a,
            token_b,
            fee_schedule=market["fee_schedule"],
            fees_enabled=market["fees_enabled"],
            min_size=market["min_size"],
            end_ns=end_ns,
        )
        fee_intervals += scanned["fee_killed_intervals"]
        fee_gross += scanned["fee_killed_open_gross"]
        evaluations += scanned["evaluations"]
        below_min_positive += scanned["below_min_positive"]
        below_min_positive_gross += scanned["below_min_positive_gross"]
        for reason, count in scanned["blocked"].items():
            blocked_totals[reason] += count
        if scanned["min_ask_sum"] is not None and (min_ask_sum is None or scanned["min_ask_sum"] < min_ask_sum):
            min_ask_sum = scanned["min_ask_sum"]
        windows = scanned["windows"]
        net_sum = Decimal("0")
        size_sum = Decimal("0")
        survived = {str(latency): 0 for latency in LATENCIES_MS}
        arrival_net = {str(latency): Decimal("0") for latency in LATENCIES_MS}
        for window in windows:
            window["slug"] = market["slug"]
            window["gamma"] = gamma
            window["end_ns"] = end_ns
            net_sum += window["net"]
            size_sum += window["size"]
            for latency in LATENCIES_MS:
                key = str(latency)
                shot = window["latency"][key]
                if shot["survived"]:
                    survived[key] += 1
                    arrival_net[key] = _add(arrival_net[key], shot["net"])
            all_windows.append(window)
        market_rows.append(
            {
                "slug": market["slug"],
                "question": market["question"],
                "gamma": gamma,
                "neg_risk": market["neg_risk"],
                "end_date": market["end_date"],
                "windows": len(windows),
                "net_sum": net_sum,
                "size_sum": size_sum,
                "survived": survived,
                "arrival_net": arrival_net,
                "fee_killed_intervals": scanned["fee_killed_intervals"],
                "evaluations": scanned["evaluations"],
            }
        )
        print(
            f"market {market['slug']} gamma={gamma} windows={len(windows)} "
            f"net={net_sum} fee_killed_intervals={scanned['fee_killed_intervals']}",
            flush=True,
        )

    durations = [window["duration_ns"] / 1_000_000_000 for window in all_windows]
    censored = sum(1 for window in all_windows if window["right_censored"])
    net_sum = sum((window["net"] for window in all_windows), Decimal("0"))
    size_sum = sum((window["size"] for window in all_windows), Decimal("0"))
    open_defer_differs = 0
    open_range_crosses = 0
    rounding_differs = 0
    per_order_delta = Decimal("0")
    end_missing = 0
    end_past = 0
    returns: list[Decimal] = []
    by_gamma = {"resolved": 0, "open": 0}
    latency_survived = {str(latency): 0 for latency in LATENCIES_MS}
    latency_defer = {str(latency): 0 for latency in LATENCIES_MS}
    latency_net = {str(latency): Decimal("0") for latency in LATENCIES_MS}
    for window in all_windows:
        by_gamma[window["gamma"]] += 1
        if window["fee_rounding_differs"]:
            rounding_differs += 1
            per_order_delta += window["per_order_net"] - window["net"]
        defer_net = window["defer_net"]
        if defer_net is None or defer_net != window["net"]:
            open_defer_differs += 1
        if defer_net is None or defer_net <= 0:
            open_range_crosses += 1
        if window["return_per_day"] is None:
            if window["end_ns"] is None:
                end_missing += 1
            else:
                end_past += 1
        else:
            returns.append(window["return_per_day"])
        for latency in LATENCIES_MS:
            key = str(latency)
            shot = window["latency"][key]
            if shot["survived"]:
                latency_survived[key] += 1
                latency_net[key] = _add(latency_net[key], shot["net"])
            if shot["defer_survived"]:
                latency_defer[key] += 1
    largest = sorted(all_windows, key=lambda window: window["net"], reverse=True)[:30]
    clamped_scan = sum(cursor.clamped for cursor in replay._cursors.values())
    summary = {
        "caveat": caveat,
        "cursor_clock": "recv_wall",
        "ties_dominate": bool(tie["ties_dominate"]),
        "quote_tie": tie,
        "recv_clock": clock,
        "token_recv_wall_backward": token_backward,
        "recv_wall_clamped": clamped_scan,
        "benchmark": bench,
        "replay_seconds": replay_seconds,
        "gamma_resolved_tokens": len(resolved_tokens),
        "gamma_resolved_with_market_resolved_frame": with_frame,
        "gamma_resolved_without_frame": len(resolved_tokens) - with_frame,
        "payoff": (
            "A complete set pays 1 USDC per share. Gamma status is the resolution "
            "source. Both tokens resolved: that payoff is already determined. "
            "Both open: it is paid at resolution. Mixed tokens are not counted."
        ),
        "binary_markets_scanned": len(market_rows),
        "skipped": skipped,
        "neg_risk_groups": groups,
        "neg_risk_note": "No group is summed. None is a complete outcome set.",
        "windows": len(all_windows),
        "windows_by_gamma": by_gamma,
        "right_censored": censored,
        "duration_s": _duration_hist(durations),
        "touch_size_sum": size_sum,
        "touch_size_p50": None if not all_windows else _pct([float(window["size"]) for window in all_windows], 0.50),
        "net_usdc_one_shot_sum": net_sum,
        "net_note": "Sum of one shot per window at the open touch. Windows overlap across markets. Not a portfolio.",
        "same_ms_open_defer_differs": open_defer_differs,
        "same_ms_open_range_includes_nonpositive": open_range_crosses,
        "same_ms_note": (
            "defer is the book from before the same-server-ms group, at a decision "
            "that lands on that group. After the last mutation of the group, defer "
            "collapses to tape. It is not a permutation of the group."
        ),
        "latency_survived_tape": latency_survived,
        "latency_survived_defer": latency_defer,
        "latency_arrival_net_usdc": latency_net,
        "fee_killed_intervals": fee_intervals,
        "fee_killed_open_gross_sum": fee_gross,
        "evaluations": evaluations,
        "blocked": dict(blocked_totals),
        "min_ask_sum": min_ask_sum,
        "below_min_positive_gross_ticks": below_min_positive,
        "below_min_positive_gross_sum": below_min_positive_gross,
        "per_order_windows_that_differ": rounding_differs,
        "per_order_minus_per_match_net": per_order_delta,
        "return_per_day_count": len(returns),
        "return_per_day_missing_end": end_missing,
        "return_per_day_end_already_past": end_past,
        "return_per_day_p50": None if not returns else _pct([float(value) for value in returns], 0.50),
        "return_per_day_min": None if not returns else float(min(returns)),
        "return_per_day_max": None if not returns else float(max(returns)),
        "unmodeled": list(UNMODELED),
        "markets": market_rows,
        "largest_windows": largest,
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(_jsonable(summary), indent=2, sort_keys=True), encoding="utf-8")
    print(f"wrote {out}")
    print(f"recv_wall_clamped={clamped_scan}")
    print(
        f"windows={len(all_windows)} net_one_shot={net_sum} size_sum={size_sum} "
        f"fee_killed_intervals={fee_intervals} fee_killed_gross={fee_gross} "
        f"min_ask_sum={min_ask_sum} below_min_positive={below_min_positive} "
        f"blocked={dict(blocked_totals)} evaluations={evaluations}"
    )
    print(f"latency_survived_tape={latency_survived} latency_survived_defer={latency_defer}")
    print(f"duration={summary['duration_s']}")
    print(f"return_per_day count={len(returns)} missing_end={end_missing} already_past={end_past}")
    print("unmodeled: " + ", ".join(UNMODELED))
    print(caveat)
    if not all_windows and fee_intervals == 0 and below_min_positive == 0:
        print(
            "No executable touch had ask_yes + ask_no < 1. "
            "Fees never had a gross edge to take, and there is nothing for latency to keep."
        )
    elif not all_windows:
        print(
            "No binary lock-in on this tape has a positive net after taker fees at the touch. "
            "Nothing survives fees."
        )
    elif all(latency_survived[str(latency)] == 0 for latency in LATENCIES_MS):
        print(
            f"{len(all_windows)} latency-0 windows have positive net after fees. "
            "None are still positive at 100, 250, or 500 ms on the tape-order book."
        )
    else:
        print(f"Some windows survive latency on the tape-order book: {latency_survived}")


if __name__ == "__main__":
    main()
