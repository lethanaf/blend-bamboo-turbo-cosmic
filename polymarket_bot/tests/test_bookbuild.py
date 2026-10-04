from decimal import Decimal

from pmbot.data.bookbuild import BookReplay, Side, is_duplicate_ws


def test_size_zero_deletes_and_absolute_size_replaces() -> None:
    side = Side(high=True)
    side.apply("0.40", "5")
    side.apply("0.40", "9")
    assert side.levels[Decimal("0.40")][0] == Decimal("9")
    side.apply("0.400", "0")
    assert side.levels == {}


def test_best_updates_without_a_scan_until_it_is_removed() -> None:
    bids = Side(high=True)
    bids.apply("0.10", "1")
    bids.apply("0.40", "2")
    bids.apply("0.30", "3")
    assert bids.best_price_str == "0.40"
    bids.apply("0.30", "8")
    assert bids.best_price_str == "0.40"
    assert bids.best_size_str == "2"
    bids.apply("0.40", "0")
    assert bids.best_price_str == "0.30"
    assert bids.best_size_str == "8"
    assert Decimal("0.10") in bids.levels


def test_aligned_uses_exchange_ts_and_tape_order() -> None:
    replay = BookReplay()
    replay.replace(1, "t", 1000, [{"price": "0.40", "size": "5"}], [{"price": "0.60", "size": "5"}], kind="book")
    # Received before the snapshot, but the exchange time is after it. Unaligned applies it.
    replay.price_change(2, "t", 5000, "BUY", "0.41", "1", "0.41", "0.60")
    replay.rest(
        3,
        "t",
        3000,
        "periodic",
        [{"price": "0.40", "size": "5"}],
        [{"price": "0.60", "size": "5"}],
        None,
    )
    # Received after the snapshot record, exchange time still before the snapshot. Aligned applies it.
    replay.price_change(4, "t", 2000, "SELL", "0.60", "7", "0.40", "0.60")
    report = replay.finish()
    # Unaligned saw the bid 0.41 update and not the later ask size, so it mismatches the snapshot.
    assert report["level_unaligned"]["mismatch"] == 1
    # Aligned skips ts=5000 and applies ts=2000, so the ask size is 7, not the snapshot's 5.
    assert report["level_aligned"]["mismatch"] == 1
    # A snapshot that matches the aligned book.
    replay2 = BookReplay()
    replay2.replace(1, "t", 1000, [{"price": "0.40", "size": "5"}], [{"price": "0.60", "size": "5"}], kind="book")
    replay2.price_change(2, "t", 5000, "BUY", "0.41", "1", "0.41", "0.60")
    replay2.rest(
        3,
        "t",
        3000,
        "periodic",
        [{"price": "0.40", "size": "5"}],
        [{"price": "0.60", "size": "7"}],
        None,
    )
    replay2.price_change(4, "t", 2000, "SELL", "0.60", "7", "0.40", "0.60")
    report2 = replay2.finish()
    assert report2["level_aligned"]["mismatch"] == 0
    assert report2["level_aligned"]["checked"] == 1
    assert report2["level_unaligned"]["mismatch"] == 1


def test_quiet_subset_ignores_nearby_ws_only() -> None:
    quiet = BookReplay(quiet_ms=1000)
    quiet.replace(1, "t", 1000, [{"price": "0.40", "size": "1"}], [{"price": "0.60", "size": "1"}], kind="book")
    quiet.note_ws("t", 1000)
    quiet.rest(
        2,
        "t",
        5000,
        "periodic",
        [{"price": "0.40", "size": "1"}],
        [{"price": "0.60", "size": "1"}],
        None,
    )
    assert quiet.finish()["level_aligned_quiet"]["checked"] == 1

    noisy = BookReplay(quiet_ms=1000)
    noisy.replace(1, "t", 1000, [{"price": "0.40", "size": "1"}], [{"price": "0.60", "size": "1"}], kind="book")
    noisy.note_ws("t", 1000)
    noisy.rest(
        2,
        "t",
        5000,
        "periodic",
        [{"price": "0.40", "size": "1"}],
        [{"price": "0.60", "size": "1"}],
        None,
    )
    noisy.note_ws("t", 4500)
    assert noisy.finish()["level_aligned_quiet"]["checked"] == 0


def test_crossed_book_and_first_mismatch_dump() -> None:
    replay = BookReplay()
    replay.set_gamma("t", "open", "example")
    replay.replace(1, "t", 1000, [{"price": "0.40", "size": "1"}], [{"price": "0.60", "size": "1"}], kind="book")
    replay.price_change(2, "t", 1100, "BUY", "0.70", "3", "0.40", "0.60")
    report = replay.finish()
    assert report["crossed_after_apply"] == 1
    assert report["quote_tape_order"]["mismatch"] == 1
    assert report["quote_tape_order"]["by_book_status"]["live"]["mismatch"] == 1
    dumped = report["first_quote_mismatches"]
    assert len(dumped) == 1
    assert dumped[0]["quoted_bid"] == "0.40"
    assert dumped[0]["local_bid"] == "0.70"
    assert dumped[0]["preceding"][-1]["kind"] == "px"
    assert dumped[0]["gamma_status"] == "open"


def test_empty_ask_sentinel_matches_and_real_one_matches() -> None:
    replay = BookReplay()
    replay.replace(1, "t", 1000, [{"price": "0.999", "size": "2"}], [], kind="book")
    replay.best_bid_ask(2, "t", 1100, "0.999", "1")
    replay.replace(3, "u", 1000, [{"price": "0.999", "size": "2"}], [{"price": "1", "size": "4"}], kind="book")
    replay.best_bid_ask(4, "u", 1100, "0.999", "1")
    report = replay.finish()
    assert report["quote_tape_order"]["checked"] == 2
    assert report["quote_tape_order"]["mismatch"] == 0
    classes = {row["token"]: row["ask_class"] for row in report["first_quote_mismatches"]}
    assert classes == {}


def test_ended_status_after_consecutive_404s() -> None:
    replay = BookReplay(end_after=2)
    replay.replace(1, "t", 1000, [{"price": "0.40", "size": "1"}], [{"price": "0.60", "size": "1"}], kind="book")
    replay.rest(2, "t", None, "periodic", None, None, "HTTP 404 no book", recv_wall="2026-10-03T14:00:02+00:00")
    replay.price_change(3, "t", 1200, "BUY", "0.40", "1", "0.40", "0.60")
    replay.rest(4, "t", None, "periodic", None, None, "HTTP 404 no book", recv_wall="2026-10-03T14:00:04+00:00")
    replay.price_change(5, "t", 1300, "BUY", "0.40", "2", "0.20", "0.60")
    report = replay.finish()
    assert report["quote_tape_order"]["by_book_status"]["live"]["checked"] == 1
    assert report["quote_tape_order"]["by_book_status"]["ended"]["checked"] == 1
    assert report["quote_tape_order"]["by_book_status"]["ended"]["mismatch"] == 1


def test_aligned_miss_tie_is_within_5ms_and_divergence_is_not() -> None:
    tie = BookReplay()
    tie.set_gamma("t", "open", "tie market")
    tie.replace(1, "t", 1000, [{"price": "0.40", "size": "5"}], [{"price": "0.60", "size": "5"}], kind="book")
    tie.price_change(2, "t", 3000, "BUY", "0.40", "9", None, None)
    tie.rest(
        3,
        "t",
        3000,
        "periodic",
        [{"price": "0.40", "size": "5"}],
        [{"price": "0.60", "size": "5"}],
        None,
    )
    tie_report = tie.finish()
    assert tie_report["aligned_level_misses"][0]["class"] == "tie"
    assert tie_report["aligned_level_misses"][0]["delta_ms"] == 0
    assert tie_report["aligned_level_misses"][0]["bids"]["size_diff"] == 1
    assert tie_report["aligned_level_miss_levels"]["size_diff"] == 1
    assert tie_report["aligned_level_miss_levels"]["extra"] == 0

    diverged = BookReplay()
    diverged.replace(1, "t", 1000, [{"price": "0.40", "size": "5"}], [{"price": "0.60", "size": "5"}], kind="book")
    diverged.price_change(2, "t", 1100, "BUY", "0.41", "1", None, None)
    diverged.rest(
        3,
        "t",
        5000,
        "periodic",
        [{"price": "0.40", "size": "5"}],
        [{"price": "0.60", "size": "5"}],
        None,
    )
    diverged_report = diverged.finish()
    miss = diverged_report["aligned_level_misses"][0]
    assert miss["class"] == "divergence"
    assert miss["delta_ms"] == 3900
    assert miss["events_within_5ms"] == 0
    assert miss["bids"]["extra"] == 1
    assert miss["bids"]["missing"] == 0


def test_duplicate_ws_window_is_inclusive_5s() -> None:
    recent = {1: 1_000}
    assert is_duplicate_ws(recent, 1, 1_000) is True
    assert is_duplicate_ws(recent, 1, 1_000 + 5_000_000_000) is True
    assert is_duplicate_ws(recent, 1, 1_000 + 5_000_000_001) is False
    assert is_duplicate_ws(recent, 2, 1_000) is False
    assert is_duplicate_ws(recent, 1, 999) is False
