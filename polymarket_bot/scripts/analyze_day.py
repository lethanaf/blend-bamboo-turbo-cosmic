#!/usr/bin/env python3
"""Replay one recording directory and print a markdown report. No orders.

Usage:
    python scripts/analyze_day.py data/day
    python scripts/analyze_day.py data/day --fetch-gamma

`data_dir` is the recorder directory: `catalog.sqlite` plus `books/**/*.jsonl.gz`.
The report is aligned level scores, quote ties, binary buy and sell lock-ins,
and complete neg-risk sets from the catalog sibling lists (sum of YES asks,
sum of YES bids, after each leg's feeSchedule, at 0/100/250/500 ms).

`--fetch-gamma` writes `gamma_status.json` from Gamma for the catalog's
condition ids before the replay. The recorder does not write that file.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import sys
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from pmbot.data.bookbuild import BookReplay, iso_to_ns  # noqa: E402
from pmbot.data.fill import caveat_from_report  # noqa: E402
from pmbot.data.gamma_status import fetch_and_write  # noqa: E402
from pmbot.data.lockin import scan_binary_pair, scan_outcome_set  # noqa: E402
from replay_book import _load_gamma, replay_dir  # noqa: E402
from scan_lockin import UNMODELED  # noqa: E402

WINDOW_CAP = 30


def _emit(text: str) -> None:
    if not text.endswith("\n"):
        text += "\n"
    try:
        sys.stdout.write(text)
        sys.stdout.flush()
    except UnicodeEncodeError:
        sys.stdout.buffer.write(text.encode("utf-8", errors="replace"))
        sys.stdout.buffer.flush()


def _connect(path: Path) -> sqlite3.Connection:
    try:
        con = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    except sqlite3.OperationalError:
        con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    return con


def _dec(value: object) -> Decimal:
    if value is None or value == "":
        return Decimal(0)
    return Decimal(str(value))


def _end_ns(value: str | None) -> int | None:
    if not value:
        return None
    try:
        return iso_to_ns(value)
    except ValueError:
        return None


def _schedule(raw: str | None) -> dict | None:
    if not raw:
        return None
    try:
        loaded = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return loaded if isinstance(loaded, dict) else None


def _rate_line(name: str, block: object) -> str:
    if not isinstance(block, dict):
        return f"- {name}: missing"
    rate = block.get("rate")
    shown = "n/a" if not isinstance(rate, (int, float)) else f"{rate:.6f}"
    return f"- {name}: checked {block.get('checked')} mismatch {block.get('mismatch')} rate {shown}"


def _tie_line(label: str, block: object) -> str:
    if not isinstance(block, dict):
        return f"- {label}: none"
    fraction = block.get("tie_fraction")
    shown = "n/a" if not isinstance(fraction, (int, float)) else f"{fraction:.3f}"
    return (
        f"- {label} quote mismatches {block.get('mismatches')}: "
        f"tie {block.get('tie')} not_tie {block.get('not_tie')} fraction {shown}"
    )


def _dominate_line(tie: dict) -> str:
    """n/a when nothing was classified. False is only a real comparison."""
    dominate = tie.get("ties_dominate")
    everyone = tie.get("all") if isinstance(tie.get("all"), dict) else {}
    mismatches = everyone.get("mismatches")
    if dominate is None or not mismatches:
        return "- ties_dominate: n/a"
    return f"- ties_dominate: {bool(dominate)}"


def _group_blocked(blocked: dict[str, int]) -> list[tuple[str, int]]:
    totals: dict[str, int] = {}
    for key, count in blocked.items():
        head, sep, tail = key.partition("_")
        if sep and (head in ("a", "b") or head.isdigit()):
            name = tail
        else:
            name = key
        totals[name] = totals.get(name, 0) + int(count)
    return sorted(totals.items(), key=lambda item: (-item[1], item[0]))


def _window_table(windows: list[dict]) -> list[str]:
    if not windows:
        return ["No net-positive windows."]
    lines = [
        "| slug | duration_s | size | net_usdc | 100ms | 250ms | 500ms |",
        "|---|---:|---:|---:|---|---|---|",
    ]
    ordered = sorted(windows, key=lambda window: window["net"], reverse=True)
    for window in ordered[:WINDOW_CAP]:
        duration = window.get("duration_ns")
        if duration is None:
            dur = ""
        else:
            dur = f"{duration / 1_000_000_000:.3f}"
            if window.get("right_censored"):
                dur += " censored"
        latency = window.get("latency") or {}

        def flag(ms: str) -> str:
            shot = latency.get(ms) or {}
            return "yes" if shot.get("survived") else "no"

        lines.append(
            f"| {window.get('slug') or ''} | {dur} | {window.get('size')} | {window.get('net')} | "
            f"{flag('100')} | {flag('250')} | {flag('500')} |"
        )
    extra = len(ordered) - WINDOW_CAP
    if extra > 0:
        lines.append(f"{extra} more windows not shown. The count above is the full set.")
    return lines


def _survived(windows: list[dict]) -> dict[str, int]:
    counts = {"100": 0, "250": 0, "500": 0}
    for window in windows:
        latency = window.get("latency") or {}
        for key in counts:
            shot = latency.get(key) or {}
            if shot.get("survived"):
                counts[key] += 1
    return counts


def _verdict(windows: list[dict], fee_killed: int, empty: str) -> str:
    if not windows and fee_killed == 0:
        return empty
    if not windows:
        return "Gross edge showed up and fees took all of it. Nothing survives fees."
    survived = _survived(windows)
    if all(value == 0 for value in survived.values()):
        return (
            f"{len(windows)} latency-0 window(s) have positive net after fees. "
            "None survive 100, 250, or 500 ms."
        )
    return (
        f"{len(windows)} latency-0 window(s) have positive net after fees. "
        f"Still positive at 100/250/500 ms: {survived['100']}/{survived['250']}/{survived['500']}."
    )


def _section(title: str, rollup: dict, *, extreme_name: str, empty: str) -> list[str]:
    lines = [
        f"## {title}",
        (
            f"evaluations {rollup['evaluations']}; fee-killed intervals {rollup['fee_killed']}; "
            f"windows {len(rollup['windows'])}; {extreme_name} {rollup['extreme']}"
        ),
        "",
        "Blocked reasons:",
    ]
    grouped = _group_blocked(rollup["blocked"])
    if not grouped:
        lines.append("- none")
    else:
        lines.extend(f"- {name}: {count}" for name, count in grouped)
    lines.append("")
    lines.extend(_window_table(rollup["windows"]))
    lines.append("")
    lines.append(_verdict(rollup["windows"], rollup["fee_killed"], empty))
    lines.append("")
    return lines


def _empty_rollup() -> dict:
    return {"evaluations": 0, "fee_killed": 0, "windows": [], "blocked": {}, "extreme": None}


def _absorb(rollup: dict, scanned: dict, slug: str, *, side: str) -> None:
    rollup["evaluations"] += int(scanned["evaluations"])
    rollup["fee_killed"] += int(scanned["fee_killed_intervals"])
    for reason, count in scanned["blocked"].items():
        rollup["blocked"][reason] = rollup["blocked"].get(reason, 0) + int(count)
    key = "min_ask_sum" if side == "buy" else "max_bid_sum"
    value = scanned.get(key)
    if isinstance(value, Decimal):
        current = rollup["extreme"]
        if current is None or (side == "buy" and value < current) or (side == "sell" and value > current):
            rollup["extreme"] = value
    for window in scanned["windows"]:
        window["slug"] = slug
        rollup["windows"].append(window)


def _load_catalog(path: Path) -> tuple[list[dict], list[dict]]:
    con = _connect(path)
    try:
        tables = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        markets = []
        if "markets" not in tables:
            return [], []
        for row in con.execute(
            """
            SELECT condition_id, slug, question, end_date, fees_enabled,
                   fee_schedule_json, order_min_size
            FROM markets
            """
        ):
            tokens = list(
                con.execute(
                    """
                    SELECT token_id, outcome, outcome_index
                    FROM tokens
                    WHERE condition_id = ?
                    ORDER BY outcome_index
                    """,
                    (row["condition_id"],),
                )
            )
            market = {
                "condition_id": row["condition_id"],
                "slug": row["slug"],
                "question": row["question"],
                "end_date": row["end_date"],
                "fees_enabled": row["fees_enabled"],
                "fee_schedule": _schedule(row["fee_schedule_json"]),
                "min_size": _dec(row["order_min_size"]),
                "tokens": [
                    {
                        "token_id": token["token_id"],
                        "outcome": token["outcome"],
                        "outcome_index": token["outcome_index"],
                    }
                    for token in tokens
                ],
            }
            markets.append(market)
        events = []
        if "neg_risk_events" in tables:
            for row in con.execute(
                """
                SELECT neg_risk_market_id, event_slug, title, end_date, outcome_count,
                       augmented, complete, sibling_json
                FROM neg_risk_events
                """
            ):
                try:
                    siblings = json.loads(row["sibling_json"])
                except json.JSONDecodeError:
                    siblings = []
                if not isinstance(siblings, list):
                    siblings = []
                events.append(
                    {
                        "neg_risk_market_id": row["neg_risk_market_id"],
                        "slug": row["event_slug"],
                        "title": row["title"],
                        "end_date": row["end_date"],
                        "outcome_count": int(row["outcome_count"]),
                        "augmented": bool(row["augmented"]),
                        "complete": bool(row["complete"]),
                        "siblings": [item for item in siblings if isinstance(item, dict)],
                    }
                )
        return markets, events
    finally:
        con.close()


def _sets(events: list[dict], by_condition: dict[str, dict]) -> tuple[list[dict], list[str]]:
    ready = []
    notes = []
    for event in events:
        slug = event["slug"] or event["neg_risk_market_id"]
        if not event["complete"]:
            notes.append(f"- {slug}: incomplete, not summed ({len(event['siblings'])} stored siblings)")
            continue
        missing = []
        leg_fees = []
        yes_tokens = []
        mins = []
        placeholders = 0
        for sibling in event["siblings"]:
            condition_id = str(sibling.get("condition_id") or "")
            market = by_condition.get(condition_id)
            yes = sibling.get("yes_token_id")
            if market is None or not isinstance(yes, str) or not yes:
                missing.append(condition_id or "?")
                continue
            yes_tokens.append(yes)
            leg_fees.append((market["fee_schedule"], market["fees_enabled"]))
            mins.append(market["min_size"])
            if sibling.get("placeholder") or sibling.get("augmented"):
                placeholders += 1
        if missing or len(yes_tokens) < 2 or len(yes_tokens) != event["outcome_count"]:
            notes.append(
                f"- {slug}: catalog says complete but the sibling rows are not all here "
                f"(yes tokens {len(yes_tokens)}, outcome_count {event['outcome_count']}, missing {missing})"
            )
            continue
        ready.append(
            {
                "slug": slug,
                "tokens": yes_tokens,
                "leg_fees": leg_fees,
                "min_size": max(mins) if mins else Decimal(0),
                "end_ns": _end_ns(event["end_date"]),
                "placeholders": placeholders,
                "augmented": event["augmented"],
                "outcome_count": event["outcome_count"],
            }
        )
    return ready, notes


def build_report(data_dir: Path) -> str:
    data_dir = data_dir.resolve()
    catalog = data_dir / "catalog.sqlite"
    books = data_dir / "books"
    markets, events = _load_catalog(catalog)
    by_condition = {market["condition_id"]: market for market in markets}
    replay = BookReplay(score_levels=True)
    gamma_path = data_dir / "gamma_status.json"
    if gamma_path.is_file():
        _load_gamma(gamma_path, replay)
    report = replay_dir(books, replay)
    lines = [
        f"# Day report `{data_dir.name}`",
        "",
        f"data_dir: `{data_dir}`",
        "",
        "## Book replay",
        f"- tape files: {len(report.get('files') or [])}",
        _rate_line("level_aligned", report.get("level_aligned")),
        _rate_line("level_unaligned", report.get("level_unaligned")),
        _rate_line("quote_tape_order", report.get("quote_tape_order")),
        _rate_line("quote_tape_order_quiet", report.get("quote_tape_order_quiet")),
    ]
    by_event = report.get("quote_by_event") if isinstance(report.get("quote_by_event"), dict) else {}
    lines.append(_rate_line("quote_price_change", by_event.get("price_change")))
    lines.append(_rate_line("quote_best_bid_ask", by_event.get("best_bid_ask")))
    after = report.get("quote_best_bid_ask_after_same_ms")
    lines.append(_rate_line("quote_best_bid_ask_after_same_ms", after))
    tie = report.get("quote_tie") if isinstance(report.get("quote_tie"), dict) else {}
    lines.append(_tie_line("all", tie.get("all")))
    lines.append(_tie_line("closed_or_resolved", tie.get("resolved")))
    lines.append(_tie_line("open", tie.get("open")))
    tie_by_event = tie.get("by_event") if isinstance(tie.get("by_event"), dict) else {}
    lines.append(_tie_line("price_change", tie_by_event.get("price_change")))
    lines.append(_tie_line("best_bid_ask", tie_by_event.get("best_bid_ask")))
    after_ties = after.get("ties") if isinstance(after, dict) else None
    lines.append(_tie_line("best_bid_ask after same-ms", after_ties))
    lines.append(_dominate_line(tie))
    lines.append(
        "- best_bid_ask is scored twice: tape order when the event is seen, and after every "
        "book mutation with that server timestamp has been applied. A different server timestamp "
        "is not part of that group. price_change quotes stay tape order."
    )
    lines.append(
        "- closed_or_resolved is the Gamma label for closed, or umaResolutionStatus resolved. "
        "The file still stores gamma_status \"resolved\"."
    )
    per = tie.get("per_1000_ws") if isinstance(tie.get("per_1000_ws"), dict) else {}
    for name in ("esports_in_play", "sports_in_play", "in_play", "other"):
        block = per.get(name) or {}
        lines.append(
            f"- per 1000 ws {name}: mismatches {block.get('mismatches', 0)} "
            f"events {block.get('ws_events', 0)} per_1000 {block.get('per_1000_ws')}"
        )
    lines.append(
        f"- dupes {report.get('dropped_duplicate_ws')} crossed_after_apply {report.get('crossed_after_apply')} "
        f"recv_wall_backward {((report.get('recv_clock') or {}).get('recv_wall_backward'))}"
    )
    if gamma_path.is_file():
        lines.append(f"- gamma file: `{gamma_path.name}`")
    else:
        lines.append(
            "- gamma file: none (pass --fetch-gamma to build gamma_status.json from Gamma "
            "for the catalog tokens; scans still run without labels)"
        )
    lines.append("")

    binaries = [market for market in markets if len(market["tokens"]) == 2]
    buy = _empty_rollup()
    sell = _empty_rollup()
    replay.reset_cursors()
    for market in binaries:
        tokens = market["tokens"]
        scanned = scan_binary_pair(
            replay,
            tokens[0]["token_id"],
            tokens[1]["token_id"],
            fee_schedule=market["fee_schedule"],
            fees_enabled=market["fees_enabled"],
            min_size=market["min_size"],
            end_ns=_end_ns(market["end_date"]),
            side="buy",
        )
        _absorb(buy, scanned, market["slug"] or market["condition_id"], side="buy")
    replay.reset_cursors()
    for market in binaries:
        tokens = market["tokens"]
        scanned = scan_binary_pair(
            replay,
            tokens[0]["token_id"],
            tokens[1]["token_id"],
            fee_schedule=market["fee_schedule"],
            fees_enabled=market["fees_enabled"],
            min_size=market["min_size"],
            end_ns=_end_ns(market["end_date"]),
            side="sell",
        )
        _absorb(sell, scanned, market["slug"] or market["condition_id"], side="sell")
    lines.append(f"Binary markets {len(binaries)}.")
    lines.append("")
    lines.extend(
        _section(
            "Binary buy",
            buy,
            extreme_name="tightest ask sum",
            empty="No executable touch had ask_yes + ask_no < 1. Nothing survives.",
        )
    )
    lines.extend(
        _section(
            "Binary sell",
            sell,
            extreme_name="highest bid sum",
            empty="No executable touch had bid_yes + bid_no > 1. Nothing survives.",
        )
    )

    ready, notes = _sets(events, by_condition)
    lines.append("## Neg-risk complete sets")
    lines.append(
        "Sum of every YES ask, and separately every YES bid, only when the catalog "
        "row says the sibling list is complete. Fees are each leg's own feeSchedule."
    )
    lines.append(f"complete sets scanned: {len(ready)}")
    if notes:
        lines.extend(notes)
    else:
        lines.append("No incomplete neg-risk rows.")
    for event in ready:
        flag = ""
        if event["placeholders"] or event["augmented"]:
            flag = f" (augmented/placeholder legs {event['placeholders']})"
        lines.append(f"- scanning {event['slug']} outcomes {event['outcome_count']}{flag}")
    lines.append("")
    set_buy = _empty_rollup()
    set_sell = _empty_rollup()
    if ready:
        for event in ready:
            replay.reset_cursors()
            scanned = scan_outcome_set(
                replay,
                event["tokens"],
                fee_schedule=event["leg_fees"][0][0],
                fees_enabled=event["leg_fees"][0][1],
                min_size=event["min_size"],
                end_ns=event["end_ns"],
                side="buy",
                leg_fees=event["leg_fees"],
            )
            _absorb(set_buy, scanned, event["slug"], side="buy")
        for event in ready:
            replay.reset_cursors()
            scanned = scan_outcome_set(
                replay,
                event["tokens"],
                fee_schedule=event["leg_fees"][0][0],
                fees_enabled=event["leg_fees"][0][1],
                min_size=event["min_size"],
                end_ns=event["end_ns"],
                side="sell",
                leg_fees=event["leg_fees"],
            )
            _absorb(set_sell, scanned, event["slug"], side="sell")
    lines.extend(
        _section(
            "Complete-set buy",
            set_buy,
            extreme_name="tightest YES ask sum",
            empty=(
                "No complete neg-risk set had a YES-ask sum under 1 after fees. Nothing survives there."
                if ready
                else "No complete neg-risk set was in this catalog. Those sums were not taken. Nothing survives there."
            ),
        )
    )
    lines.extend(
        _section(
            "Complete-set sell",
            set_sell,
            extreme_name="highest YES bid sum",
            empty=(
                "No complete neg-risk set had a YES-bid sum over 1 after fees. Nothing survives there."
                if ready
                else "No complete neg-risk set was in this catalog. Those sums were not taken. Nothing survives there."
            ),
        )
    )
    lines.append("## Not modeled")
    lines.extend(f"- {item}" for item in UNMODELED)
    lines.append(f"- {caveat_from_report(report)}")
    lines.append("")
    lines.append("No orders. live_trading is false.")
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Replay a recording and print one markdown report. No orders.")
    parser.add_argument("data_dir", type=Path, help="Recorder directory (catalog.sqlite and books/)")
    parser.add_argument(
        "--fetch-gamma",
        action="store_true",
        help="Write data_dir/gamma_status.json from Gamma for the catalog condition ids, then report.",
    )
    args = parser.parse_args(argv)
    data_dir = args.data_dir
    if not data_dir.is_dir():
        print(f"not a directory: {data_dir}", file=sys.stderr)
        return 2
    catalog = data_dir / "catalog.sqlite"
    if not catalog.is_file():
        print(f"no catalog at {catalog}", file=sys.stderr)
        return 2
    if args.fetch_gamma:
        rows = asyncio.run(fetch_and_write(data_dir))
        print(f"wrote {data_dir / 'gamma_status.json'} ({len(rows)} tokens)", file=sys.stderr)
    _emit(build_report(data_dir))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
