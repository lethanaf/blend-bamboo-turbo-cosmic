from decimal import Decimal

from pmbot.data.bookbuild import BookReplay, RecvClock, clock_id, iso_to_ns, market_bucket, ns_to_iso
from pmbot.data.fill import caveat_from_report, fee_per_match_vs_per_order, taker_fill
from pmbot.data.lockin import (
    binary_edge,
    neg_risk_group_report,
    return_per_day,
    scan_binary_pair,
    scan_outcome_set,
    set_edge,
)


SCHEDULE = {"rate": "0.04", "exponent": 1, "takerOnly": True, "rebateRate": "0.25"}


def test_market_bucket_splits_in_play_from_other() -> None:
    assert market_bucket("cs2-ts7-shin-2026-10-03") == "esports_in_play"
    assert market_bucket("dota2-lgd-gl-2026-10-03-game1") == "esports_in_play"
    assert market_bucket("lol-gam-we-2026-10-03") == "esports_in_play"
    assert market_bucket("val-kc3-ns1-2026-10-03") == "esports_in_play"
    assert market_bucket("fif-arg-bur-2026-10-03-spread-away-5pt5") == "sports_in_play"
    assert market_bucket("unl-hrv-eng-2026-10-03-eng") == "sports_in_play"
    assert market_bucket("wta-andree-boisson-2026-10-03") == "sports_in_play"
    assert market_bucket("es2-alb-eib-2026-10-03-eib") == "sports_in_play"
    assert market_bucket("will-the-us-invade-iran-before-2027") == "other"
    assert market_bucket("bitcoin-above-74k-on-october-3-2026") == "other"


def test_same_server_timestamp_is_a_tie_in_either_tape_order() -> None:
    replay = BookReplay()
    replay.set_gamma("t", "resolved", "match", "cs2-a-b")
    replay.set_gamma("u", "open", "election", "will-example")
    replay.replace(
        1,
        "t",
        1000,
        [{"price": "0.40", "size": "5"}],
        [{"price": "0.60", "size": "5"}],
        kind="book",
        recv_wall="2026-10-03T14:00:00+00:00",
    )
    replay.note_ws("t", 1000)
    # Two quotes share server ts 1100. The second is later on the tape.
    # Local ask is 0.60, so a quoted ask of 0.50 mismatches.
    replay.best_bid_ask(2, "t", 1100, "0.40", "0.50", recv_wall="2026-10-03T14:00:01+00:00")
    replay.best_bid_ask(3, "t", 1100, "0.40", "0.50", recv_wall="2026-10-03T14:00:01.100000+00:00")
    # Unique timestamp. Still a mismatch. Not a tie.
    replay.best_bid_ask(4, "t", 2000, "0.40", "0.50", recv_wall="2026-10-03T14:00:02+00:00")
    replay.replace(
        5,
        "u",
        1000,
        [{"price": "0.40", "size": "5"}],
        [{"price": "0.60", "size": "5"}],
        kind="book",
        recv_wall="2026-10-03T14:00:00+00:00",
    )
    replay.note_ws("u", 1000)
    replay.best_bid_ask(6, "u", 3000, "0.40", "0.50", recv_wall="2026-10-03T14:00:03+00:00")
    report = replay.finish()
    resolved = report["quote_tie"]["resolved"]
    assert resolved["mismatches"] == 3
    assert resolved["tie"] == 2
    assert resolved["not_tie"] == 1
    assert report["quote_tie"]["open"] == {"mismatches": 1, "tie": 0, "not_tie": 1, "no_server_ts": 0, "tie_fraction": 0.0}
    per = report["quote_tie"]["per_1000_ws"]
    # book note + three best_bid_ask on the esports token.
    assert per["esports_in_play"]["ws_events"] == 4
    assert per["esports_in_play"]["mismatches"] == 3
    assert per["in_play"]["mismatches"] == 3
    assert per["other"]["mismatches"] == 1
    assert per["other"]["ws_events"] == 2


def test_unlabeled_ties_count_and_an_empty_replay_is_not_a_finding() -> None:
    empty = BookReplay().finish()
    assert empty["quote_tie"]["ties_dominate"] is None
    assert empty["quote_tie"]["all"]["mismatches"] == 0
    assert empty["quote_tie"]["resolved"]["mismatches"] == 0

    replay = BookReplay()
    replay.replace(
        1,
        "t",
        1000,
        [{"price": "0.40", "size": "5"}],
        [{"price": "0.60", "size": "5"}],
        kind="book",
        recv_wall="2026-10-03T14:00:00+00:00",
    )
    replay.best_bid_ask(2, "t", 1100, "0.40", "0.50", recv_wall="2026-10-03T14:00:01+00:00")
    replay.best_bid_ask(3, "t", 1100, "0.40", "0.50", recv_wall="2026-10-03T14:00:01.100000+00:00")
    report = replay.finish()
    tie = report["quote_tie"]
    assert tie["resolved"]["mismatches"] == 0
    assert tie["open"]["mismatches"] == 0
    assert tie["all"] == {
        "mismatches": 2,
        "tie": 2,
        "not_tie": 0,
        "no_server_ts": 0,
        "tie_fraction": 1.0,
    }
    assert tie["ties_dominate"] is True
    assert tie["by_event"]["best_bid_ask"]["mismatches"] == 2
    assert tie["by_event"]["price_change"]["mismatches"] == 0


def test_quote_checks_split_by_price_change_and_best_bid_ask() -> None:
    replay = BookReplay()
    replay.replace(
        1,
        "t",
        1000,
        [{"price": "0.40", "size": "5"}],
        [{"price": "0.60", "size": "5"}],
        kind="book",
        recv_wall="2026-10-03T14:00:00+00:00",
    )
    # Quoted ask 0.50 is not the local 0.60. The 0.70 level does not move the touch.
    replay.price_change(
        2, "t", 1100, "SELL", "0.70", "3", "0.40", "0.50", recv_wall="2026-10-03T14:00:01+00:00"
    )
    replay.price_change(
        3, "t", 1100, "SELL", "0.70", "4", "0.40", "0.50", recv_wall="2026-10-03T14:00:01.100000+00:00"
    )
    replay.best_bid_ask(4, "t", 2000, "0.40", "0.55", recv_wall="2026-10-03T14:00:02+00:00")
    replay.best_bid_ask(5, "t", 3000, "0.40", "0.60", recv_wall="2026-10-03T14:00:03+00:00")
    # No quoted touch. Not a quote check.
    replay.price_change(
        6, "t", 4000, "SELL", "0.70", "0", None, None, recv_wall="2026-10-03T14:00:04+00:00"
    )
    report = replay.finish()
    by = report["quote_by_event"]
    quote = report["quote_tape_order"]
    assert by["price_change"]["checked"] + by["best_bid_ask"]["checked"] == quote["checked"]
    assert by["price_change"]["mismatch"] + by["best_bid_ask"]["mismatch"] == quote["mismatch"]
    assert by["price_change"] == {"checked": 2, "mismatch": 2, "rate": 1.0}
    assert by["best_bid_ask"]["checked"] == 2
    assert by["best_bid_ask"]["mismatch"] == 1
    tie = report["quote_tie"]["by_event"]
    assert tie["price_change"]["mismatches"] == 2
    assert tie["price_change"]["tie"] == 2
    assert tie["best_bid_ask"]["mismatches"] == 1
    assert tie["best_bid_ask"]["tie"] == 0
    assert report["quote_tie"]["ties_dominate"] is True


def test_cursor_is_forward_only_and_same_ms_defer_drops_the_group() -> None:
    replay = BookReplay()
    replay.replace(
        1,
        "t",
        1000,
        [{"price": "0.40", "size": "5"}],
        [{"price": "0.50", "size": "10"}],
        kind="book",
        recv_wall="2026-10-03T14:00:00+00:00",
    )
    # Same server timestamp. The resting 0.50 ask has to be deleted, or the
    # later 0.70 level is not the touch. Defer keeps the book from before the group.
    replay.price_change(
        2, "t", 2000, "SELL", "0.50", "0", None, None, recv_wall="2026-10-03T14:00:01+00:00"
    )
    replay.price_change(
        3, "t", 2000, "SELL", "0.40", "10", None, None, recv_wall="2026-10-03T14:00:01.100000+00:00"
    )
    replay.price_change(
        4, "t", 2000, "SELL", "0.40", "0", None, None, recv_wall="2026-10-03T14:00:01.200000+00:00"
    )
    replay.price_change(
        5, "t", 2000, "SELL", "0.70", "5", None, None, recv_wall="2026-10-03T14:00:01.300000+00:00"
    )
    both = replay.book_asof("t", "2026-10-03T14:00:01.300000+00:00", same_ms="both")
    assert both["tape"]["asks"].best == Decimal("0.70")
    assert both["defer_same_ms"]["asks"].best == Decimal("0.50")
    assert both["tape"]["same_ms_tie"] is True
    # Strictly after the group, defer collapses to tape order.
    later = replay.book_asof("t", "2026-10-03T14:00:01.301000+00:00", same_ms="both")
    assert later["tape"]["asks"].best == Decimal("0.70")
    assert later["defer_same_ms"]["asks"].best == Decimal("0.70")
    assert later["tape"]["same_ms_tie"] is False
    wall = "2026-10-03T14:00:01.200000+00:00"
    assert iso_to_ns(ns_to_iso(iso_to_ns(wall))) == iso_to_ns(wall)
    try:
        replay.book_asof("t", "2026-10-03T14:00:01+00:00")
    except ValueError as exc:
        assert "backward" in str(exc)
    else:
        raise AssertionError("expected backward query to fail")


def test_backward_recv_wall_is_clamped_and_applied_in_tape_order() -> None:
    replay = BookReplay()
    replay.replace(
        1,
        "t",
        1000,
        [{"price": "0.40", "size": "5"}],
        [{"price": "0.50", "size": "10"}],
        kind="book",
        recv_wall="2026-10-03T14:00:02+00:00",
    )
    replay.price_change(
        2, "t", 2000, "SELL", "0.50", "0", None, None, recv_wall="2026-10-03T14:00:01+00:00"
    )
    replay.price_change(
        3, "t", 2000, "SELL", "0.70", "5", None, None, recv_wall="2026-10-03T14:00:01+00:00"
    )
    early = replay.book_asof("t", "2026-10-03T14:00:01+00:00")
    assert early["anchored"] is False
    late = replay.book_asof("t", "2026-10-03T14:00:02+00:00")
    assert late["asks"].best == Decimal("0.70")
    assert replay._cursors["t"].clamped == 2


def test_per_order_fee_can_differ_from_per_match() -> None:
    # Each leg raw is 0.000005. Per match, half-up charges 0.00001 twice.
    # Per order, the raw sum 0.00001 rounds once.
    schedule = {"rate": "0.00003125", "exponent": 1}
    legs = [(Decimal("1"), Decimal("0.2")), (Decimal("1"), Decimal("0.8"))]
    row = fee_per_match_vs_per_order(legs, schedule, fees_enabled=True, rounding="half-up")
    assert row["per_match"] == Decimal("0.00002")
    assert row["per_order"] == Decimal("0.00001")
    assert row["differ"] is True


def test_caveat_is_loaded_from_the_report_and_not_a_final_window_sentence() -> None:
    text = caveat_from_report({"quote_tape_order": {"mismatch": 31268, "checked": 1766236}})
    assert text == (
        "unmodeled: 31268 quote mismatches out of 1766236 checks are not adjusted for in this number"
    )
    assert "final window" not in text
    assert "404" not in text
    fill = taker_fill(
        *_touch(),
        side="BUY",
        size="1",
        fee_schedule=SCHEDULE,
        fees_enabled=True,
    )
    assert "unmodeled" in fill.lines()[1]
    assert "final window" not in fill.lines()[1]


def test_binary_edge_subtracts_both_taker_fees_and_respects_min_size() -> None:
    # 10 * (1 - 0.40 - 0.40) = 2. Fee per leg = 10 * 0.04 * 0.40 * 0.60 = 0.096.
    edge = binary_edge(
        Decimal("0.40"),
        Decimal("10"),
        Decimal("0.40"),
        Decimal("12"),
        SCHEDULE,
        fees_enabled=True,
        min_size=Decimal("5"),
    )
    assert edge is not None and edge["executable"] is True
    assert edge["size"] == Decimal("10")
    assert edge["gross"] == Decimal("2.0")
    assert edge["fees"] == Decimal("0.19200")
    assert edge["net"] == Decimal("2.0") - Decimal("0.19200")
    # Asks sum to 0.98, but the fee is larger than the 0.20 gross on 10 shares at rate 0.05.
    killed = binary_edge(
        Decimal("0.49"),
        Decimal("10"),
        Decimal("0.49"),
        Decimal("10"),
        {"rate": "0.05", "exponent": 1},
        fees_enabled=True,
        min_size=Decimal("5"),
    )
    assert killed is not None and killed["gross"] > 0 and killed["net"] < 0
    small = binary_edge(
        Decimal("0.40"),
        Decimal("4"),
        Decimal("0.40"),
        Decimal("4"),
        SCHEDULE,
        fees_enabled=True,
        min_size=Decimal("5"),
    )
    assert small is not None and small["executable"] is False
    assert return_per_day(Decimal("1"), Decimal("10"), 0, 86_400_000_000_000) == Decimal("0.1")
    assert return_per_day(Decimal("1"), Decimal("10"), 10, 10) is None


def test_neg_risk_group_without_an_outcome_count_is_not_a_lockin() -> None:
    report = neg_risk_group_report(
        [
            {"slug": "will-lula", "question": "Lula"},
            {"slug": "will-flavio", "question": "Flavio"},
        ]
    )
    assert report["complete"] is False
    assert report["counted_as_lockin"] is False
    assert report["conditions"] == 2
    complete = neg_risk_group_report(
        [
            {"condition_id": "a", "slug": "a", "question": "A"},
            {"condition_id": "b", "slug": "b", "question": "B"},
        ],
        outcome_count=2,
        sibling_condition_ids=["a", "b"],
    )
    assert complete["complete"] is True
    assert complete["counted_as_lockin"] is True


def test_session_id_splits_writers_that_share_connection_0() -> None:
    clock = RecvClock()
    left = clock_id({"session_id": "writer-a", "connection_id": "0"})
    right = clock_id({"session_id": "writer-b", "connection_id": "0"})
    assert left != right
    clock.session_start(left)
    clock.observe(left, "2026-10-03T15:22:31+00:00", 20)
    clock.session_start(right)
    clock.observe(right, "2026-10-03T15:22:30+00:00", 1)
    assert clock.wall_backward == 0
    assert clock_id({"connection_id": "0"}) == "conn:0"


def test_mint_and_sell_uses_the_same_fee_and_dies_at_latency() -> None:
    # Bids 0.60 and 0.60. Gross = 10 * 0.20. Fee per leg = 10 * 0.04 * 0.60 * 0.40.
    edge = binary_edge(
        Decimal("0.60"),
        Decimal("10"),
        Decimal("0.60"),
        Decimal("12"),
        SCHEDULE,
        fees_enabled=True,
        min_size=Decimal("5"),
        side="sell",
    )
    assert edge is not None and edge["executable"] is True
    assert edge["gross"] == Decimal("2.0")
    assert edge["fees"] == Decimal("0.19200")
    assert edge["net"] == Decimal("2.0") - Decimal("0.19200")
    assert edge["cost"] == Decimal("10") + Decimal("0.19200")
    replay = BookReplay()
    t0 = "2026-10-03T14:00:00+00:00"
    t1 = "2026-10-03T14:00:00.100000+00:00"
    replay.replace(
        1, "a", 1, [{"price": "0.60", "size": "10"}], [{"price": "0.70", "size": "10"}], kind="book", recv_wall=t0
    )
    replay.replace(
        2, "b", 1, [{"price": "0.60", "size": "12"}], [{"price": "0.70", "size": "10"}], kind="book", recv_wall=t0
    )
    replay.price_change(3, "b", 2, "BUY", "0.60", "0", None, None, recv_wall=t1)
    replay.price_change(4, "b", 2, "BUY", "0.20", "12", None, None, recv_wall=t1)
    scanned = scan_binary_pair(
        replay,
        "a",
        "b",
        fee_schedule=SCHEDULE,
        fees_enabled=True,
        min_size=Decimal("5"),
        end_ns=iso_to_ns("2026-10-04T14:00:00+00:00"),
        side="sell",
    )
    assert len(scanned["windows"]) == 1
    assert scanned["windows"][0]["net"] == Decimal("2.0") - Decimal("0.19200")
    assert scanned["windows"][0]["duration_ns"] == 100_000_000
    assert scanned["windows"][0]["latency"]["100"]["survived"] is False
    assert scanned["max_bid_sum"] == Decimal("1.20")


def test_three_outcome_yes_sum_pays_one_after_fees() -> None:
    # Asks 0.30 three times. Gross = 10 * 0.10. Fee per leg = 10 * 0.04 * 0.30 * 0.70.
    edge = set_edge(
        [(Decimal("0.30"), Decimal("10"))] * 3,
        SCHEDULE,
        fees_enabled=True,
        min_size=Decimal("5"),
        side="buy",
    )
    assert edge is not None and edge["net"] == Decimal("1") - Decimal("0.25200")
    sold = set_edge(
        [(Decimal("0.40"), Decimal("10"))] * 3,
        SCHEDULE,
        fees_enabled=True,
        min_size=Decimal("5"),
        side="sell",
    )
    assert sold is not None and sold["gross"] == Decimal("2")
    assert sold["fees"] == Decimal("0.28800")
    replay = BookReplay()
    wall = "2026-10-03T14:00:00+00:00"
    for index, token in enumerate(("a", "b", "c")):
        replay.replace(
            index,
            token,
            1,
            [{"price": "0.20", "size": "10"}],
            [{"price": "0.30", "size": "10"}],
            kind="book",
            recv_wall=wall,
        )
    scanned = scan_outcome_set(
        replay,
        ["a", "b", "c"],
        fee_schedule=SCHEDULE,
        fees_enabled=True,
        min_size=Decimal("5"),
        end_ns=None,
        side="buy",
    )
    assert len(scanned["windows"]) == 1
    assert scanned["windows"][0]["net"] == Decimal("1") - Decimal("0.25200")


def test_each_leg_uses_its_own_fee_schedule() -> None:
    cheap = {"rate": "0", "exponent": 1}
    edge = set_edge(
        [(Decimal("0.30"), Decimal("10")), (Decimal("0.30"), Decimal("10"))],
        SCHEDULE,
        fees_enabled=True,
        min_size=Decimal("1"),
        leg_fees=[(SCHEDULE, True), (cheap, True)],
    )
    assert edge is not None
    assert edge["fees"] == Decimal("0.08400")
    assert edge["gross"] == Decimal("4")
    assert edge["net"] == Decimal("4") - Decimal("0.08400")


def test_lockin_window_is_one_shot_and_dies_when_latency_sees_the_next_book() -> None:
    replay = BookReplay()
    t0 = "2026-10-03T14:00:00+00:00"
    t1 = "2026-10-03T14:00:00.100000+00:00"
    replay.replace(
        1, "a", 1, [{"price": "0.30", "size": "10"}], [{"price": "0.40", "size": "10"}], kind="book", recv_wall=t0
    )
    replay.replace(
        2, "b", 1, [{"price": "0.30", "size": "10"}], [{"price": "0.40", "size": "12"}], kind="book", recv_wall=t0
    )
    # Same server timestamp, so the arrival book at t1 still has a defer reading.
    replay.price_change(3, "b", 2, "SELL", "0.40", "0", None, None, recv_wall=t1)
    replay.price_change(4, "b", 2, "SELL", "0.70", "12", None, None, recv_wall=t1)
    end = iso_to_ns("2026-10-04T14:00:00+00:00")
    scanned = scan_binary_pair(
        replay,
        "a",
        "b",
        fee_schedule=SCHEDULE,
        fees_enabled=True,
        min_size=Decimal("5"),
        end_ns=end,
    )
    assert len(scanned["windows"]) == 1
    window = scanned["windows"][0]
    assert window["size"] == Decimal("10")
    assert window["net"] == Decimal("2.0") - Decimal("0.19200")
    assert window["duration_ns"] == 100_000_000
    assert window["right_censored"] is False
    assert window["return_per_day"] is not None
    assert window["latency"]["100"]["survived"] is False
    assert window["latency"]["250"]["survived"] is False
    assert window["latency"]["500"]["survived"] is False
    # Defer at the killing event is the pre-group book, which is still a lock-in.
    assert window["latency"]["100"]["defer_survived"] is True
    assert scanned["fee_killed_intervals"] == 0


def test_fees_kill_a_gross_lockin_before_a_window_opens() -> None:
    replay = BookReplay()
    replay.replace(
        1,
        "a",
        1,
        [{"price": "0.40", "size": "10"}],
        [{"price": "0.49", "size": "10"}],
        kind="book",
        recv_wall="2026-10-03T14:00:00+00:00",
    )
    replay.replace(
        2,
        "b",
        1,
        [{"price": "0.40", "size": "10"}],
        [{"price": "0.49", "size": "10"}],
        kind="book",
        recv_wall="2026-10-03T14:00:00+00:00",
    )
    scanned = scan_binary_pair(
        replay,
        "a",
        "b",
        fee_schedule={"rate": "0.05", "exponent": 1},
        fees_enabled=True,
        min_size=Decimal("5"),
        end_ns=None,
    )
    assert scanned["windows"] == []
    assert scanned["fee_killed_intervals"] == 1
    assert scanned["fee_killed_open_gross"] > 0


def _touch():
    from pmbot.data.bookbuild import Side

    bids = Side(high=True)
    asks = Side(high=False)
    bids.apply("0.40", "5")
    asks.apply("0.50", "5")
    return bids, asks
