"""Rebuild a book from the tape and score it. No strategy, no orders.

`price_change` sizes are absolute. Size 0 deletes the level. Apply order is
tape order. A periodic REST book is scored two ways:

- unaligned: the local book after every earlier tape event
- aligned: the local book after every event whose server timestamp is <= the
  snapshot's own server timestamp, still in tape order

Events later on the tape with an earlier server timestamp count for the
aligned book. Events earlier on the tape with a later server timestamp do not.

Books are keyed by normalized Decimal so "0.40" and "0.400" are one level.
Best bid and best ask move incrementally: a full scan happens only when the
current best price is deleted.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections import defaultdict, deque
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

# book/anchor: (kind, index, ts, bids, asks, recv_wall)
# px:          (kind, index, ts, side, price, size, recv_wall)
# gap:         ("gap", index, recv_wall)
Mut = tuple

# Identical raw WS payload, kept within this many nanoseconds, is a duplicate.
# Compared with recv_monotonic_ns. Inclusive on both ends.
DUPLICATE_WINDOW_NS = 5_000_000_000
DUPLICATE_MAP_CAP = 200_000


def is_duplicate_ws(
    recent: dict[int, int],
    digest: int,
    mono: int,
    *,
    window_ns: int = DUPLICATE_WINDOW_NS,
) -> bool:
    """Whether `hash(raw)` was already kept inside the duplicate window.

    `hash` is Python's per-process string hash, so this matches identical
    payload text inside one replay process. It is not a stable cross-process
    digest. The caller records the new stamp after this returns False, and
    prunes `recent` back to `window_ns` once it grows past DUPLICATE_MAP_CAP.
    """
    previous = recent.get(digest)
    return previous is not None and 0 <= mono - previous <= window_ns


def _dec(value: object) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def iso_to_ns(value: str) -> int:
    """UTC nanoseconds from an ISO-8601 timestamp. Exact to microseconds."""
    moment = datetime.fromisoformat(value)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    delta = moment.astimezone(timezone.utc) - datetime(1970, 1, 1, tzinfo=timezone.utc)
    return delta.days * 86_400 * 1_000_000_000 + delta.seconds * 1_000_000_000 + delta.microseconds * 1000


class Side:
    """One side of a book, keyed by normalized Decimal, with an incremental best."""

    __slots__ = ("levels", "best", "best_size", "best_price_str", "best_size_str", "high")

    def __init__(self, *, high: bool) -> None:
        self.levels: dict[Decimal, tuple[Decimal, str, str]] = {}
        self.best: Decimal | None = None
        self.best_size: Decimal | None = None
        self.best_price_str: str | None = None
        self.best_size_str: str | None = None
        self.high = high

    def clear(self) -> None:
        self.levels.clear()
        self.best = None
        self.best_size = None
        self.best_price_str = None
        self.best_size_str = None

    def apply(self, price: str, size: str) -> None:
        """Absolute size. Zero deletes the level. Best is recomputed only if it was removed."""
        numeric = _dec(price)
        amount = _dec(size)
        if numeric is None or amount is None:
            return
        if amount == 0:
            if numeric not in self.levels:
                return
            del self.levels[numeric]
            if self.best == numeric:
                self._recompute_best()
            return
        self.levels[numeric] = (amount, str(price), str(size))
        if self.best is None or (self.high and numeric > self.best) or (not self.high and numeric < self.best):
            self.best = numeric
            self.best_size = amount
            self.best_price_str = str(price)
            self.best_size_str = str(size)
        elif self.best == numeric:
            self.best_size = amount
            self.best_price_str = str(price)
            self.best_size_str = str(size)

    def _recompute_best(self) -> None:
        if not self.levels:
            self.best = None
            self.best_size = None
            self.best_price_str = None
            self.best_size_str = None
            return
        numeric = max(self.levels) if self.high else min(self.levels)
        amount, price_str, size_str = self.levels[numeric]
        self.best = numeric
        self.best_size = amount
        self.best_price_str = price_str
        self.best_size_str = size_str

    def load(self, levels: tuple[tuple[str, str], ...]) -> None:
        self.clear()
        for price, size in levels:
            self.apply(price, size)

    def copy(self) -> Side:
        other = Side(high=self.high)
        other.levels = dict(self.levels)
        other.best = self.best
        other.best_size = self.best_size
        other.best_price_str = self.best_price_str
        other.best_size_str = self.best_size_str
        return other

    def __len__(self) -> int:
        return len(self.levels)


def apply_level(levels: Side, price: str, size: str) -> None:
    levels.apply(price, size)


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


def _load(levels: tuple[tuple[str, str], ...], *, high: bool) -> Side:
    side = Side(high=high)
    side.load(levels)
    return side


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


def quote_verdict(bids: Side, asks: Side, best_bid: str | None, best_ask: str | None) -> dict:
    bid_class = _side_class(bids.best, best_bid, sentinel="0")
    ask_class = _side_class(asks.best, best_ask, sentinel="1")
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
        "local_bid": bids.best_price_str,
        "local_ask": asks.best_price_str,
        "local_bid_size": bids.best_size_str,
        "local_ask_size": asks.best_size_str,
        "crossed": bids.best is not None and asks.best is not None and bids.best >= asks.best,
    }


def levels_diff(local: Side, remote: tuple[tuple[str, str], ...]) -> dict[str, int]:
    got = {price: size for price, (size, _price_str, _size_str) in local.levels.items() if size != 0}
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


_TO_END_BINS: tuple[tuple[float, float, str], ...] = (
    (0, 30, "0-30s"),
    (30, 60, "30-60s"),
    (60, 120, "1-2min"),
    (120, 300, "2-5min"),
    (300, 600, "5-10min"),
    (600, 1800, "10-30min"),
    (1800, 3600, "30-60min"),
    (3600, 7200, "60-120min"),
    (7200, 1e18, ">120min"),
)

_SINCE_START_BINS: tuple[tuple[float, float, str], ...] = (
    (0, 3, "0-3s"),
    (3, 30, "3-30s"),
    (30, 60, "30-60s"),
    (60, 300, "1-5min"),
    (300, 1800, "5-30min"),
    (1800, 3600, "30-60min"),
    (3600, 7200, "60-120min"),
    (7200, 1e18, ">120min"),
)


def _percentile(sorted_values: list[float], fraction: float) -> float | None:
    if not sorted_values:
        return None
    index = min(len(sorted_values) - 1, max(0, int(round((len(sorted_values) - 1) * fraction))))
    return sorted_values[index]


def _histogram(values: list[float], edges: tuple[tuple[float, float, str], ...]) -> dict:
    counts = {label: 0 for _lo, _hi, label in edges}
    negative = 0
    other = 0
    for value in values:
        if value < 0:
            negative += 1
            continue
        placed = False
        for lo, hi, label in edges:
            if lo <= value < hi:
                counts[label] += 1
                placed = True
                break
        if not placed:
            other += 1
    ordered = sorted(values)
    within = {
        "60s": sum(1 for value in values if 0 <= value <= 60),
        "120s": sum(1 for value in values if 0 <= value <= 120),
        "300s": sum(1 for value in values if 0 <= value <= 300),
        "600s": sum(1 for value in values if 0 <= value <= 600),
    }
    return {
        "n": len(values),
        "negative_or_after": negative,
        "bins": counts,
        "other": other,
        "within": within,
        "p50_s": _percentile(ordered, 0.50),
        "p90_s": _percentile(ordered, 0.90),
        "p99_s": _percentile(ordered, 0.99),
        "min_s": None if not ordered else ordered[0],
        "max_s": None if not ordered else ordered[-1],
    }


class BookReplay:
    def __init__(self, *, end_after: int = 3, quiet_ms: int = 1000, mismatch_limit: int = 20) -> None:
        if end_after < 1:
            raise ValueError("end_after must be >= 1")
        self.end_after = end_after
        self.quiet_ms = quiet_ms
        self.mismatch_limit = mismatch_limit
        self.muts: dict[str, list[Mut]] = defaultdict(list)
        self.live_bids: dict[str, Side] = {}
        self.live_asks: dict[str, Side] = {}
        self.anchored: set[str] = set()
        self.gap_frozen: set[str] = set()
        self.fail_streak: dict[str, int] = defaultdict(int)
        self.ended_at: dict[str, int] = {}
        self.ended_recv_wall: dict[str, str] = {}
        self.resolved_recv_wall: dict[str, str] = {}
        self.resolved_server_ts: dict[str, int] = {}
        self.session_start_wall: str | None = None
        self.ws_ts: dict[str, list[int]] = defaultdict(list)
        self.precede: dict[str, deque] = defaultdict(lambda: deque(maxlen=8))
        self.snapshots: list[dict] = []
        self.quote_rows: list[tuple] = []
        self.resolved_quote_walls: list[tuple[str, str]] = []
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
        self.aligned_misses: list[dict] = []
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

    def note_session_start(self, recv_wall: str, is_reconnect: bool) -> None:
        if self.session_start_wall is None and recv_wall:
            self.session_start_wall = recv_wall

    def note_market_resolved(self, token: str, server_ts: int | None, recv_wall: str | None) -> None:
        if recv_wall and token not in self.resolved_recv_wall:
            self.resolved_recv_wall[token] = recv_wall
        if server_ts is not None and token not in self.resolved_server_ts:
            self.resolved_server_ts[token] = server_ts

    def _sides(self, token: str) -> tuple[Side, Side]:
        bids = self.live_bids.get(token)
        asks = self.live_asks.get(token)
        if bids is None or asks is None:
            bids = Side(high=True)
            asks = Side(high=False)
            self.live_bids[token] = bids
            self.live_asks[token] = asks
        return bids, asks

    def gap(self, index: int, token_ids: list[str], recv_wall: str | None = None) -> None:
        for token in token_ids:
            self.muts[token].append(("gap", index, recv_wall))
            bids, asks = self._sides(token)
            bids.clear()
            asks.clear()
            self.anchored.discard(token)
            self.gap_frozen.add(token)
            self.precede[token].append({"kind": "gap", "index": index})

    def replace(
        self,
        index: int,
        token: str,
        ts: int | None,
        bids: object,
        asks: object,
        *,
        kind: str,
        recv_wall: str | None = None,
    ) -> None:
        bid_levels = levels_of(bids)
        ask_levels = levels_of(asks)
        self.muts[token].append((kind, index, ts, bid_levels, ask_levels, recv_wall))
        live_bids = _load(bid_levels, high=True)
        live_asks = _load(ask_levels, high=False)
        self.live_bids[token] = live_bids
        self.live_asks[token] = live_asks
        self.anchored.add(token)
        self.gap_frozen.discard(token)
        self.fail_streak[token] = 0
        self.precede[token].append(
            {
                "kind": kind,
                "index": index,
                "ts": ts,
                "bids": len(live_bids),
                "asks": len(live_asks),
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
        self.muts[token].append(("px", index, ts, side, str(price), str(size), recv_wall))
        if token not in self.anchored:
            self.skipped_unanchored_px += 1
        else:
            bids, asks = self._sides(token)
            target = bids if side == "BUY" else asks
            if side in ("BUY", "SELL"):
                target.apply(str(price), str(size))
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
        recv_wall: str | None = None,
    ) -> None:
        if error:
            if "404" in error:
                self.fail_streak[token] += 1
                if self.fail_streak[token] >= self.end_after and token not in self.ended_at:
                    self.ended_at[token] = index
                    if recv_wall:
                        self.ended_recv_wall[token] = recv_wall
            return
        self.fail_streak[token] = 0
        if reason in ("bootstrap", "reconnect"):
            self.replace(index, token, ts, bids, asks, kind="anchor", recv_wall=recv_wall)
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
        miss_diff = {"extra": 0, "missing": 0, "size_diff": 0}
        for snap in self.snapshots:
            token = snap["token"]
            anchored, bids, asks, last_ts, within_5ms = self._replay(
                token, aligned=True, cutoff_index=snap["index"], cutoff_ts=snap["ts"]
            )
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
            if not exact:
                for key in ("extra", "missing", "size_diff"):
                    miss_diff[key] += diff_b[key] + diff_a[key]
                delta = None if last_ts is None else snap["ts"] - last_ts
                tie = delta is not None and 0 <= delta <= 5
                self.aligned_misses.append(
                    {
                        "token": token,
                        "question": self.questions.get(token, ""),
                        "gamma_status": snap["gamma_status"],
                        "snapshot_ts": snap["ts"],
                        "last_applied_ts": last_ts,
                        "delta_ms": delta,
                        "events_within_5ms": within_5ms,
                        "class": "tie" if tie else "divergence",
                        "bids": diff_b,
                        "asks": diff_a,
                    }
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
            "aligned_level_misses": self.aligned_misses,
            "aligned_level_miss_levels": miss_diff,
            "resolved_quote_histogram": self._resolved_histogram(),
        }

    def book_asof(self, token: str, recv_wall: str) -> dict:
        """Tape-order book using events whose recv_wall is <= `recv_wall`.

        A gap clears and freezes the token until the next anchor. Ended is the
        recv time of the record that tripped `end_after` consecutive 404s.
        """
        asof = iso_to_ns(recv_wall)
        bids = Side(high=True)
        asks = Side(high=False)
        anchored = False
        gap_frozen = False
        for mut in self.muts[token]:
            wall = mut[-1]
            if not isinstance(wall, str):
                raise ValueError(f"token {token} event has no recv_wall; cannot apply latency")
            if iso_to_ns(wall) > asof:
                continue
            kind = mut[0]
            if kind == "gap":
                bids.clear()
                asks.clear()
                anchored = False
                gap_frozen = True
                continue
            if kind == "px":
                if not anchored:
                    continue
                side, price, size = mut[3], mut[4], mut[5]
                if side == "BUY":
                    bids.apply(price, size)
                elif side == "SELL":
                    asks.apply(price, size)
                continue
            bids = _load(mut[3], high=True)
            asks = _load(mut[4], high=False)
            anchored = True
            gap_frozen = False
        ended_wall = self.ended_recv_wall.get(token)
        ended = ended_wall is not None and iso_to_ns(ended_wall) <= asof
        return {
            "bids": bids,
            "asks": asks,
            "anchored": anchored,
            "gap_frozen": gap_frozen,
            "ended": ended,
        }

    def _resolved_histogram(self) -> dict:
        to_ended: list[float] = []
        to_resolved: list[float] = []
        since_start: list[float] = []
        no_ended = 0
        no_resolved = 0
        no_session = 0
        missing_wall = 0
        start = self.session_start_wall
        start_ns = iso_to_ns(start) if start else None
        ended_ns = {token: iso_to_ns(wall) for token, wall in self.ended_recv_wall.items()}
        resolved_ns = {token: iso_to_ns(wall) for token, wall in self.resolved_recv_wall.items()}
        for token, wall in self.resolved_quote_walls:
            if not wall:
                missing_wall += 1
                continue
            stamp = iso_to_ns(wall)
            end = ended_ns.get(token)
            if end is None:
                no_ended += 1
            else:
                to_ended.append((end - stamp) / 1_000_000_000)
            resolved = resolved_ns.get(token)
            if resolved is None:
                no_resolved += 1
            else:
                to_resolved.append((resolved - stamp) / 1_000_000_000)
            if start_ns is None:
                no_session += 1
            else:
                since_start.append((stamp - start_ns) / 1_000_000_000)
        startup = sum(1 for value in since_start if 0 <= value < 3)
        return {
            "mismatches": len(self.resolved_quote_walls),
            "missing_recv_wall": missing_wall,
            "no_ended_index": no_ended,
            "no_market_resolved_on_tape": no_resolved,
            "no_session_start": no_session,
            "startup_first_3s": startup,
            "seconds_to_ended_index": _histogram(to_ended, _TO_END_BINS),
            "seconds_to_market_resolved": _histogram(to_resolved, _TO_END_BINS),
            "seconds_since_session_start": _histogram(since_start, _SINCE_START_BINS),
            "note": (
                "Population is quote mismatches whose Gamma status is resolved. "
                "seconds_to_ended_index uses the recv time of the tape record that "
                "tripped end_after consecutive GET /book 404s. "
                "seconds_to_market_resolved uses the first market_resolved frame on the tape "
                "that names the token. Tokens with neither endpoint are counted aside, not binned. "
                "The first 20 quote mismatches in the dump are a startup transient and are not this histogram."
            ),
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
        bids, asks = self._sides(token)
        view = quote_verdict(bids, asks, best_bid, best_ask)
        if view["verdict"] == "skip":
            return
        book_status = self._book_status(token, index)
        gamma_status = self.gamma.get(token, "unknown")
        mismatch = view["verdict"] == "mismatch"
        self.quote.add(token, book_status, gamma_status, mismatch)
        self.quote_rows.append((token, ts, book_status, gamma_status, mismatch))
        if mismatch and gamma_status == "resolved":
            self.resolved_quote_walls.append((token, recv_wall or ""))
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
        bids, asks = self._sides(token)
        if bids.best is not None and asks.best is not None and bids.best >= asks.best:
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
        self,
        token: str,
        *,
        aligned: bool,
        cutoff_index: int,
        cutoff_ts: int | None,
    ) -> tuple[bool, Side, Side, int | None, int]:
        bids = Side(high=True)
        asks = Side(high=False)
        anchored = False
        last_ts: int | None = None
        within_5ms = 0
        for mut in self.muts[token]:
            kind = mut[0]
            index = mut[1]
            if kind == "gap":
                if index < cutoff_index:
                    bids = Side(high=True)
                    asks = Side(high=False)
                    anchored = False
                continue
            ts = mut[2]
            if aligned:
                if ts is None or (cutoff_ts is not None and ts > cutoff_ts):
                    continue
            elif index >= cutoff_index:
                break
            if (
                aligned
                and ts is not None
                and cutoff_ts is not None
                and 0 <= cutoff_ts - ts <= 5
            ):
                within_5ms += 1
            last_ts = ts if isinstance(ts, int) else last_ts
            if kind == "px":
                if not anchored:
                    continue
                side, price, size = mut[3], mut[4], mut[5]
                if side == "BUY":
                    bids.apply(price, size)
                elif side == "SELL":
                    asks.apply(price, size)
            else:
                bids = _load(mut[3], high=True)
                asks = _load(mut[4], high=False)
                anchored = True
        return anchored, bids, asks, last_ts, within_5ms


def _rest_top(bids: tuple[tuple[str, str], ...], asks: tuple[tuple[str, str], ...]) -> tuple[str | None, str | None]:
    bid_book = _load(bids, high=True)
    ask_book = _load(asks, high=False)
    return bid_book.best_price_str, ask_book.best_price_str
