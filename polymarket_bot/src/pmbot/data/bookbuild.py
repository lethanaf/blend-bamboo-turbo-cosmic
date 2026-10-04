"""Rebuild a book from the tape and score it. No fill simulator.

`price_change` sizes are absolute. Size 0 deletes the level. Apply order is
tape order. A periodic REST book is scored two ways:

- unaligned: the local book after every earlier tape event
- aligned: the local book after every event whose server timestamp is <= the
  snapshot's own server timestamp, still in tape order

Events later on the tape with an earlier server timestamp count for the
aligned book. Events earlier on the tape with a later server timestamp do not.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections import defaultdict, deque
from decimal import Decimal, InvalidOperation

BidAsk = dict[str, str]
# book/anchor: (kind, index, ts, bids, asks) with bids/asks tuple[(price, size)]
# px:          (kind, index, ts, side, price, size)
# gap:         ("gap", index)
Mut = tuple


def _dec(value: object) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def apply_level(levels: BidAsk, price: str, size: str) -> None:
    """Set the absolute size at `price`. A zero size removes the level."""
    amount = _dec(size)
    if amount is None:
        return
    if amount == 0:
        levels.pop(str(price), None)
        # Also drop a key that is the same number with different spelling.
        target = _dec(price)
        if target is None:
            return
        for key in list(levels):
            if _dec(key) == target:
                levels.pop(key, None)
        return
    target = _dec(price)
    if target is None:
        return
    for key in list(levels):
        if _dec(key) == target:
            levels.pop(key, None)
    levels[str(price)] = str(size)


def levels_of(raw: object) -> tuple[tuple[str, str], ...]:
    if not isinstance(raw, list):
        return ()
    out: list[tuple[str, str]] = []
    for level in raw:
        if not isinstance(level, dict) or "price" not in level:
            continue
        size = level.get("size", "0")
        out.append((str(level["price"]), "0" if size is None else str(size)))
    return tuple(out)


def _load(levels: tuple[tuple[str, str], ...]) -> BidAsk:
    book: BidAsk = {}
    for price, size in levels:
        apply_level(book, price, size)
    return book


def _best(levels: BidAsk, *, high: bool) -> tuple[Decimal | None, str | None, str | None]:
    chosen_d: Decimal | None = None
    chosen_p: str | None = None
    chosen_s: str | None = None
    for price, size in levels.items():
        amount = _dec(size)
        if amount is None or amount == 0:
            continue
        numeric = _dec(price)
        if numeric is None:
            continue
        if chosen_d is None or (high and numeric > chosen_d) or (not high and numeric < chosen_d):
            chosen_d, chosen_p, chosen_s = numeric, price, size
    return chosen_d, chosen_p, chosen_s


def _side_class(local: Decimal | None, quoted: str | None, *, sentinel: str) -> str:
    if quoted is None or quoted == "":
        return "skip"
    numeric = _dec(quoted)
    if numeric is None:
        return "mismatch"
    if local is None:
        if numeric == Decimal(sentinel):
            return "sentinel"
        return "mismatch"
    if local == numeric:
        if numeric == Decimal(sentinel):
            return "real_extreme"
        return "match"
    return "mismatch"


def quote_verdict(bids: BidAsk, asks: BidAsk, best_bid: str | None, best_ask: str | None) -> dict:
    bid_d, bid_p, bid_s = _best(bids, high=True)
    ask_d, ask_p, ask_s = _best(asks, high=False)
    bid_class = _side_class(bid_d, best_bid, sentinel="0")
    ask_class = _side_class(ask_d, best_ask, sentinel="1")
    parts = [item for item in (bid_class, ask_class) if item != "skip"]
    if not parts:
        verdict = "skip"
    elif any(item == "mismatch" for item in parts):
        verdict = "mismatch"
    else:
        verdict = "match"
    return {
        "verdict": verdict,
        "bid_class": bid_class,
        "ask_class": ask_class,
        "local_bid": bid_p,
        "local_ask": ask_p,
        "local_bid_size": bid_s,
        "local_ask_size": ask_s,
        "crossed": bid_d is not None and ask_d is not None and bid_d >= ask_d,
    }


def levels_diff(local: BidAsk, remote: tuple[tuple[str, str], ...]) -> dict[str, int]:
    got = {_dec(price): _dec(size) for price, size in local.items()}
    got = {price: size for price, size in got.items() if price is not None and size not in (None, 0)}
    want: dict[Decimal, Decimal] = {}
    for price, size in remote:
        numeric = _dec(price)
        amount = _dec(size)
        if numeric is None or amount is None or amount == 0:
            continue
        want[numeric] = amount
    extra = sum(1 for price in got if price not in want)
    missing = sum(1 for price in want if price not in got)
    size_diff = sum(1 for price, size in want.items() if price in got and got[price] != size)
    return {
        "extra": extra,
        "missing": missing,
        "size_diff": size_diff,
        "exact": int(extra == 0 and missing == 0 and size_diff == 0),
    }


class _Rate:
    def __init__(self) -> None:
        self.checked = 0
        self.mismatch = 0
        self.by_token: dict[str, list[int]] = defaultdict(lambda: [0, 0])
        self.by_book_status: dict[str, list[int]] = defaultdict(lambda: [0, 0])
        self.by_gamma_status: dict[str, list[int]] = defaultdict(lambda: [0, 0])

    def add(self, token: str, book_status: str, gamma_status: str, mismatch: bool) -> None:
        self.checked += 1
        self.mismatch += int(mismatch)
        for bucket, key in (
            (self.by_token, token),
            (self.by_book_status, book_status),
            (self.by_gamma_status, gamma_status),
        ):
            bucket[key][0] += 1
            bucket[key][1] += int(mismatch)

    def as_dict(self) -> dict:
        def pack(rows: dict[str, list[int]]) -> dict:
            return {
                key: {
                    "checked": checked,
                    "mismatch": mismatch,
                    "rate": None if checked == 0 else mismatch / checked,
                }
                for key, (checked, mismatch) in sorted(rows.items())
            }

        return {
            "checked": self.checked,
            "mismatch": self.mismatch,
            "rate": None if self.checked == 0 else self.mismatch / self.checked,
            "by_token": pack(self.by_token),
            "by_book_status": pack(self.by_book_status),
            "by_gamma_status": pack(self.by_gamma_status),
        }


class BookReplay:
    def __init__(self, *, end_after: int = 3, quiet_ms: int = 1000, mismatch_limit: int = 20) -> None:
        if end_after < 1:
            raise ValueError("end_after must be >= 1")
        self.end_after = end_after
        self.quiet_ms = quiet_ms
        self.mismatch_limit = mismatch_limit
        self.muts: dict[str, list[Mut]] = defaultdict(list)
        self.live_bids: dict[str, BidAsk] = defaultdict(dict)
        self.live_asks: dict[str, BidAsk] = defaultdict(dict)
        self.anchored: set[str] = set()
        self.fail_streak: dict[str, int] = defaultdict(int)
        self.ended_at: dict[str, int] = {}
        self.ws_ts: dict[str, list[int]] = defaultdict(list)
        self.precede: dict[str, deque] = defaultdict(lambda: deque(maxlen=8))
        self.snapshots: list[dict] = []
        self.quote_rows: list[tuple] = []
        self.gamma: dict[str, str] = {}
        self.questions: dict[str, str] = {}
        self.crossed = 0
        self.crossed_tokens: set[str] = set()
        self.crossed_at_snapshot = {"aligned": 0, "unaligned": 0}
        self.quote = _Rate()
        self.quote_quiet = _Rate()
        self.level_aligned = _Rate()
        self.level_unaligned = _Rate()
        self.level_aligned_quiet = _Rate()
        self.level_unaligned_quiet = _Rate()
        self.top_aligned = _Rate()
        self.top_unaligned = _Rate()
        self.top_aligned_quiet = _Rate()
        self.top_unaligned_quiet = _Rate()
        self.mismatches: list[dict] = []
        self.skipped_unanchored_px = 0
        self.quote_checked_unanchored = 0

    def set_gamma(self, token: str, status: str, question: str = "") -> None:
        self.gamma[token] = status
        if question:
            self.questions[token] = question

    def note_ws(self, token: str, ts: int | None) -> None:
        if ts is None or not token:
            return
        self.ws_ts[token].append(ts)

    def gap(self, index: int, token_ids: list[str]) -> None:
        for token in token_ids:
            self.muts[token].append(("gap", index))
            self.live_bids[token].clear()
            self.live_asks[token].clear()
            self.anchored.discard(token)
            self.precede[token].append({"kind": "gap", "index": index})

    def replace(self, index: int, token: str, ts: int | None, bids: object, asks: object, *, kind: str) -> None:
        bid_levels = levels_of(bids)
        ask_levels = levels_of(asks)
        self.muts[token].append((kind, index, ts, bid_levels, ask_levels))
        self.live_bids[token] = _load(bid_levels)
        self.live_asks[token] = _load(ask_levels)
        self.anchored.add(token)
        self.fail_streak[token] = 0
        self.precede[token].append(
            {
                "kind": kind,
                "index": index,
                "ts": ts,
                "bids": len(self.live_bids[token]),
                "asks": len(self.live_asks[token]),
            }
        )
        self._count_crossed(token)

    def price_change(
        self,
        index: int,
        token: str,
        ts: int | None,
        side: str,
        price: str,
        size: str,
        best_bid: str | None,
        best_ask: str | None,
        *,
        recv_wall: str | None = None,
    ) -> None:
        self.note_ws(token, ts)
        self.muts[token].append(("px", index, ts, side, str(price), str(size)))
        if token not in self.anchored:
            self.skipped_unanchored_px += 1
        else:
            target = self.live_bids[token] if side == "BUY" else self.live_asks[token]
            if side in ("BUY", "SELL"):
                apply_level(target, str(price), str(size))
            self._count_crossed(token)
        self.precede[token].append(
            {"kind": "px", "index": index, "ts": ts, "side": side, "price": str(price), "size": str(size)}
        )
        if best_bid is None and best_ask is None:
            return
        self._score_quote(index, token, ts, best_bid, best_ask, recv_wall=recv_wall)

    def best_bid_ask(
        self,
        index: int,
        token: str,
        ts: int | None,
        best_bid: str | None,
        best_ask: str | None,
        *,
        recv_wall: str | None = None,
    ) -> None:
        self.note_ws(token, ts)
        self.precede[token].append(
            {"kind": "best_bid_ask", "index": index, "ts": ts, "best_bid": best_bid, "best_ask": best_ask}
        )
        self._score_quote(index, token, ts, best_bid, best_ask, recv_wall=recv_wall)

    def rest(
        self,
        index: int,
        token: str,
        ts: int | None,
        reason: str,
        bids: object,
        asks: object,
        error: str | None,
    ) -> None:
        if error:
            if "404" in error:
                self.fail_streak[token] += 1
                if self.fail_streak[token] >= self.end_after and token not in self.ended_at:
                    self.ended_at[token] = index
            return
        self.fail_streak[token] = 0
        if reason in ("bootstrap", "reconnect"):
            self.replace(index, token, ts, bids, asks, kind="anchor")
            return
        if reason != "periodic":
            return
        book_status = self._book_status(token, index)
        gamma_status = self.gamma.get(token, "unknown")
        anchored_now = token in self.anchored
        if anchored_now:
            remote_bids = levels_of(bids)
            remote_asks = levels_of(asks)
            diff = {
                "bids": levels_diff(self.live_bids[token], remote_bids),
                "asks": levels_diff(self.live_asks[token], remote_asks),
            }
            exact = diff["bids"]["exact"] == 1 and diff["asks"]["exact"] == 1
            top = quote_verdict(self.live_bids[token], self.live_asks[token], *_rest_top(remote_bids, remote_asks))
            self.level_unaligned.add(token, book_status, gamma_status, not exact)
            self.top_unaligned.add(token, book_status, gamma_status, top["verdict"] == "mismatch")
            if top["crossed"]:
                self.crossed_at_snapshot["unaligned"] += 1
            unaligned_exact: bool | None = exact
            unaligned_top_mismatch: bool | None = top["verdict"] == "mismatch"
        else:
            unaligned_exact = None
            unaligned_top_mismatch = None
        self.snapshots.append(
            {
                "index": index,
                "token": token,
                "ts": ts,
                "bids": levels_of(bids),
                "asks": levels_of(asks),
                "book_status": book_status,
                "gamma_status": gamma_status,
                "unaligned_exact": unaligned_exact,
                "unaligned_top_mismatch": unaligned_top_mismatch,
            }
        )

    def finish(self) -> dict:
        for token, stamps in self.ws_ts.items():
            stamps.sort()
        for snap in self.snapshots:
            token = snap["token"]
            anchored, bids, asks = self._replay(token, aligned=True, cutoff_index=snap["index"], cutoff_ts=snap["ts"])
            if not anchored or snap["ts"] is None:
                continue
            diff_b = levels_diff(bids, snap["bids"])
            diff_a = levels_diff(asks, snap["asks"])
            exact = diff_b["exact"] == 1 and diff_a["exact"] == 1
            quiet = self._quiet(token, snap["ts"])
            self.level_aligned.add(token, snap["book_status"], snap["gamma_status"], not exact)
            top = quote_verdict(bids, asks, *_rest_top(snap["bids"], snap["asks"]))
            self.top_aligned.add(token, snap["book_status"], snap["gamma_status"], top["verdict"] == "mismatch")
            if top["crossed"]:
                self.crossed_at_snapshot["aligned"] += 1
            if quiet:
                self.level_aligned_quiet.add(token, snap["book_status"], snap["gamma_status"], not exact)
                self.top_aligned_quiet.add(
                    token, snap["book_status"], snap["gamma_status"], top["verdict"] == "mismatch"
                )
            if quiet and snap["unaligned_exact"] is not None:
                self.level_unaligned_quiet.add(
                    token, snap["book_status"], snap["gamma_status"], not snap["unaligned_exact"]
                )
                self.top_unaligned_quiet.add(
                    token, snap["book_status"], snap["gamma_status"], bool(snap["unaligned_top_mismatch"])
                )
        for token, ts, book_status, gamma_status, mismatch in self.quote_rows:
            if self._quiet(token, ts, ignore_one=ts):
                self.quote_quiet.add(token, book_status, gamma_status, mismatch)
        return {
            "level_aligned": self.level_aligned.as_dict(),
            "level_unaligned": self.level_unaligned.as_dict(),
            "level_aligned_quiet": self.level_aligned_quiet.as_dict(),
            "level_unaligned_quiet": self.level_unaligned_quiet.as_dict(),
            "top_aligned": self.top_aligned.as_dict(),
            "top_unaligned": self.top_unaligned.as_dict(),
            "top_aligned_quiet": self.top_aligned_quiet.as_dict(),
            "top_unaligned_quiet": self.top_unaligned_quiet.as_dict(),
            "quote_tape_order": self.quote.as_dict(),
            "quote_tape_order_quiet": self.quote_quiet.as_dict(),
            "crossed_after_apply": self.crossed,
            "crossed_tokens": sorted(self.crossed_tokens),
            "crossed_at_snapshot": dict(self.crossed_at_snapshot),
            "ended_at_index": dict(self.ended_at),
            "skipped_unanchored_px": self.skipped_unanchored_px,
            "quote_checked_unanchored": self.quote_checked_unanchored,
            "first_quote_mismatches": self.mismatches,
            "periodic_snapshots_seen": len(self.snapshots),
        }

    def _score_quote(
        self,
        index: int,
        token: str,
        ts: int | None,
        best_bid: str | None,
        best_ask: str | None,
        *,
        recv_wall: str | None,
    ) -> None:
        if token not in self.anchored:
            self.quote_checked_unanchored += 1
            return
        view = quote_verdict(self.live_bids[token], self.live_asks[token], best_bid, best_ask)
        if view["verdict"] == "skip":
            return
        book_status = self._book_status(token, index)
        gamma_status = self.gamma.get(token, "unknown")
        mismatch = view["verdict"] == "mismatch"
        self.quote.add(token, book_status, gamma_status, mismatch)
        self.quote_rows.append((token, ts, book_status, gamma_status, mismatch))
        if mismatch and len(self.mismatches) < self.mismatch_limit:
            self.mismatches.append(
                {
                    "token": token,
                    "question": self.questions.get(token, ""),
                    "index": index,
                    "server_ts": ts,
                    "recv_wall": recv_wall,
                    "quoted_bid": best_bid,
                    "quoted_ask": best_ask,
                    "local_bid": view["local_bid"],
                    "local_ask": view["local_ask"],
                    "local_bid_size": view["local_bid_size"],
                    "local_ask_size": view["local_ask_size"],
                    "bid_class": view["bid_class"],
                    "ask_class": view["ask_class"],
                    "book_status": book_status,
                    "gamma_status": gamma_status,
                    "preceding": list(self.precede[token]),
                }
            )

    def _count_crossed(self, token: str) -> None:
        view = quote_verdict(self.live_bids[token], self.live_asks[token], None, None)
        if view["crossed"]:
            self.crossed += 1
            self.crossed_tokens.add(token)

    def _book_status(self, token: str, index: int) -> str:
        ended = self.ended_at.get(token)
        if ended is not None and ended <= index:
            return "ended"
        return "live"

    def _quiet(self, token: str, ts: int | None, ignore_one: int | None = None) -> bool:
        if ts is None:
            return False
        stamps = self.ws_ts.get(token)
        if not stamps:
            return True
        lo = bisect_left(stamps, ts - self.quiet_ms)
        hi = bisect_right(stamps, ts + self.quiet_ms)
        count = hi - lo
        if ignore_one is not None and ts - self.quiet_ms <= ignore_one <= ts + self.quiet_ms and count:
            count -= 1
        return count == 0

    def _replay(
        self, token: str, *, aligned: bool, cutoff_index: int, cutoff_ts: int | None
    ) -> tuple[bool, BidAsk, BidAsk]:
        bids: BidAsk = {}
        asks: BidAsk = {}
        anchored = False
        for mut in self.muts[token]:
            kind = mut[0]
            index = mut[1]
            if kind == "gap":
                if index < cutoff_index:
                    bids, asks, anchored = {}, {}, False
                continue
            ts = mut[2]
            if aligned:
                if ts is None or (cutoff_ts is not None and ts > cutoff_ts):
                    continue
            elif index >= cutoff_index:
                break
            if kind == "px":
                if not anchored:
                    continue
                side, price, size = mut[3], mut[4], mut[5]
                if side == "BUY":
                    apply_level(bids, price, size)
                elif side == "SELL":
                    apply_level(asks, price, size)
            else:
                bids = _load(mut[3])
                asks = _load(mut[4])
                anchored = True
        return anchored, bids, asks


def _rest_top(bids: tuple[tuple[str, str], ...], asks: tuple[tuple[str, str], ...]) -> tuple[str | None, str | None]:
    bid_book = _load(bids)
    ask_book = _load(asks)
    _bid_d, bid_p, _bid_s = _best(bid_book, high=True)
    _ask_d, ask_p, _ask_s = _best(ask_book, high=False)
    return bid_p, ask_p
