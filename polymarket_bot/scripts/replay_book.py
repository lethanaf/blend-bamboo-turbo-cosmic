#!/usr/bin/env python3
"""Score rebuilt books against a Phase 1 tape. Does not simulate fills."""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from pmbot.data.bookbuild import BookReplay  # noqa: E402
from pmbot.data.jsonl_log import iter_jsonl_gz  # noqa: E402


def _ts(value: object) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def _events(raw: str) -> list[dict]:
    if raw in ("PONG", "PING"):
        return []
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        return [payload]
    return []


def _load_gamma(path: Path, replay: BookReplay) -> None:
    meta = json.loads(path.read_text(encoding="utf-8"))
    for token, info in meta.items():
        if not isinstance(info, dict):
            continue
        replay.set_gamma(str(token), str(info.get("gamma_status") or "unknown"), str(info.get("question") or ""))


def replay_dir(books: Path, replay: BookReplay) -> dict:
    index = 0
    recent: dict[int, int] = {}
    dropped_dupes = 0
    ignored_bootstrap = 0
    multi_entry_tokens = 0
    bootstrapped: set[str] = set()
    files = sorted(books.rglob("*.jsonl.gz"))
    for path in files:
        for record in iter_jsonl_gz(path):
            index += 1
            kind = record.get("kind")
            if kind == "ws":
                raw = record.get("raw")
                if not isinstance(raw, str):
                    continue
                mono = int(record.get("recv_monotonic_ns") or 0)
                digest = hash(raw)
                previous = recent.get(digest)
                if previous is not None and 0 <= mono - previous <= 5_000_000_000:
                    dropped_dupes += 1
                    continue
                recent[digest] = mono
                if len(recent) > 200_000:
                    cutoff = mono - 5_000_000_000
                    recent = {key: stamp for key, stamp in recent.items() if stamp >= cutoff}
                wall = record.get("recv_wall")
                for event in _events(raw):
                    event_type = event.get("event_type")
                    ts = _ts(event.get("timestamp"))
                    if event_type == "book":
                        token = str(event.get("asset_id") or "")
                        if not token:
                            continue
                        replay.note_ws(token, ts)
                        replay.replace(index, token, ts, event.get("bids"), event.get("asks"), kind="book")
                    elif event_type == "price_change":
                        changes = event.get("price_changes")
                        if not isinstance(changes, list):
                            continue
                        per_token = Counter(str(change.get("asset_id") or "") for change in changes if isinstance(change, dict))
                        multi_entry_tokens += sum(1 for count in per_token.values() if count > 1)
                        frame_ts = ts
                        for change in changes:
                            if not isinstance(change, dict):
                                continue
                            token = str(change.get("asset_id") or "")
                            if not token:
                                continue
                            change_ts = _ts(change.get("timestamp")) or frame_ts
                            replay.price_change(
                                index,
                                token,
                                change_ts,
                                str(change.get("side") or ""),
                                str(change.get("price") or ""),
                                str(change.get("size") or "0"),
                                None if change.get("best_bid") is None else str(change.get("best_bid")),
                                None if change.get("best_ask") is None else str(change.get("best_ask")),
                                recv_wall=wall if isinstance(wall, str) else None,
                            )
                    elif event_type == "best_bid_ask":
                        token = str(event.get("asset_id") or "")
                        if not token:
                            continue
                        replay.best_bid_ask(
                            index,
                            token,
                            ts,
                            None if event.get("best_bid") is None else str(event.get("best_bid")),
                            None if event.get("best_ask") is None else str(event.get("best_ask")),
                            recv_wall=wall if isinstance(wall, str) else None,
                        )
                    elif event_type in ("last_trade_price", "tick_size_change"):
                        token = str(event.get("asset_id") or "")
                        replay.note_ws(token, ts)
            elif kind == "gap":
                tokens = record.get("token_ids")
                if isinstance(tokens, list):
                    replay.gap(index, [str(token) for token in tokens])
            elif kind == "rest_book":
                token = str(record.get("token_id") or "")
                if not token:
                    continue
                reason = str(record.get("reason") or "")
                if reason == "bootstrap":
                    if token in bootstrapped:
                        ignored_bootstrap += 1
                        continue
                    bootstrapped.add(token)
                error = record.get("error")
                payload = record.get("payload") if isinstance(record.get("payload"), dict) else {}
                replay.rest(
                    index,
                    token,
                    _ts(payload.get("timestamp")),
                    reason,
                    payload.get("bids"),
                    payload.get("asks"),
                    None if error is None else str(error),
                )
    report = replay.finish()
    report["files"] = [str(path) for path in files]
    report["dropped_duplicate_ws"] = dropped_dupes
    report["ignored_extra_bootstrap"] = ignored_bootstrap
    report["price_change_multi_entry_tokens"] = multi_entry_tokens
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Replay a tape and score the rebuilt book")
    parser.add_argument("--books", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--gamma", type=Path, default=None, help="token -> {gamma_status, question}")
    parser.add_argument("--end-after", type=int, default=3)
    args = parser.parse_args()
    replay = BookReplay(end_after=args.end_after)
    if args.gamma is not None:
        _load_gamma(args.gamma, replay)
    report = replay_dir(args.books, replay)
    mismatches = report.pop("first_quote_mismatches")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    mismatch_path = args.out.with_name(args.out.stem + "_mismatches.json")
    mismatch_path.write_text(json.dumps(mismatches, indent=2), encoding="utf-8")
    def brief(name: str) -> str:
        block = report[name]
        rate = block["rate"]
        shown = "n/a" if rate is None else f"{rate:.6f}"
        return f"{name} checked={block['checked']} mismatch={block['mismatch']} rate={shown}"

    print(brief("level_aligned"))
    print(brief("level_unaligned"))
    print(brief("level_aligned_quiet"))
    print(brief("level_unaligned_quiet"))
    print(brief("quote_tape_order"))
    print(brief("quote_tape_order_quiet"))
    print(
        f"crossed_after_apply={report['crossed_after_apply']} tokens={len(report['crossed_tokens'])} "
        f"multi_entry={report['price_change_multi_entry_tokens']} dupes={report['dropped_duplicate_ws']}"
    )
    print(f"wrote {args.out} and {mismatch_path}")


if __name__ == "__main__":
    main()
