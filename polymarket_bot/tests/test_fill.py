from decimal import Decimal

from pmbot.data.bookbuild import BookReplay, Side
from pmbot.data.fill import FeeError, rounding_sensitivity, taker_fee, taker_fill


SCHEDULE = {"rate": "0.04", "exponent": 1, "takerOnly": True, "rebateRate": "0.25"}
CRYPTO = {"rate": "0.07", "exponent": 1, "takerOnly": True, "rebateRate": "0.20"}


def _book(bids: list[tuple[str, str]], asks: list[tuple[str, str]]) -> tuple[Side, Side]:
    bid = Side(high=True)
    ask = Side(high=False)
    for price, size in bids:
        bid.apply(price, size)
    for price, size in asks:
        ask.apply(price, size)
    return bid, ask


def test_fee_table_matches_formula_and_ignores_rebate() -> None:
    # 100 shares at 0.50, politics/finance rate 0.04 -> 1.00 USDC. Rebate is not credited.
    assert taker_fee(Decimal("100"), Decimal("0.50"), SCHEDULE, fees_enabled=True) == Decimal("1.00000")
    # Crypto peak from the fees table: 100 * 0.07 * 0.5 * 0.5 = 1.75.
    assert taker_fee(Decimal("100"), Decimal("0.50"), CRYPTO, fees_enabled=True) == Decimal("1.75000")
    # 100 * 0.07 * 0.01 * 0.99 = 0.0693, which the table displays as $0.07.
    assert taker_fee(Decimal("100"), Decimal("0.01"), CRYPTO, fees_enabled=True) == Decimal("0.06930")


def test_fee_disabled_and_zero_rate_pay_zero() -> None:
    assert taker_fee(Decimal("100"), Decimal("0.50"), SCHEDULE, fees_enabled=False) == Decimal("0")
    assert taker_fee(Decimal("100"), Decimal("0.50"), SCHEDULE, fees_enabled=0) == Decimal("0")
    assert taker_fee(Decimal("100"), Decimal("0.50"), {"rate": 0, "exponent": 1}, fees_enabled=True) == Decimal("0")
    assert taker_fee(Decimal("100"), Decimal("0.50"), None, fees_enabled=None) == Decimal("0")


def test_exponent_other_than_one_is_refused() -> None:
    try:
        taker_fee(Decimal("10"), Decimal("0.50"), {"rate": "0.04", "exponent": 2}, fees_enabled=True)
    except FeeError as exc:
        assert "exponent" in str(exc)
    else:
        raise AssertionError("expected FeeError")


def test_half_up_vs_half_even_sensitivity() -> None:
    # p*(1-p) = 0.25. raw = shares * rate * 0.25.
    cases = [
        (Decimal("1"), Decimal("0.5"), {"rate": "0.00002", "exponent": 1}),  # raw 0.000005
        (Decimal("1"), Decimal("0.5"), {"rate": "0.00006", "exponent": 1}),  # raw 0.000015
        (Decimal("1"), Decimal("0.5"), {"rate": "0.0001", "exponent": 1}),  # raw 0.000025
        (Decimal("100"), Decimal("0.50"), CRYPTO),
    ]
    rows = rounding_sensitivity(cases)
    assert rows[0]["half_up"] == Decimal("0.00001")
    assert rows[0]["half_even"] == Decimal("0")
    assert rows[0]["differ"] is True
    assert rows[1]["half_up"] == Decimal("0.00002")
    assert rows[1]["half_even"] == Decimal("0.00002")
    assert rows[2]["half_up"] == Decimal("0.00003")
    assert rows[2]["half_even"] == Decimal("0.00002")
    assert rows[3]["half_up"] == rows[3]["half_even"] == Decimal("1.75000")
    assert sum(1 for row in rows if row["differ"]) == 2


def test_each_refusal_and_partial_fill_slippage() -> None:
    bids, asks = _book([("0.40", "5"), ("0.39", "5")], [("0.50", "4"), ("0.60", "10")])
    refused = [
        taker_fill(bids, asks, side="BUY", size="1", fee_schedule=SCHEDULE, fees_enabled=True, ended=True),
        taker_fill(bids, asks, side="BUY", size="1", fee_schedule=SCHEDULE, fees_enabled=True, gap_frozen=True),
        taker_fill(bids, asks, side="BUY", size="1", fee_schedule=SCHEDULE, fees_enabled=True, anchored=False),
    ]
    assert [item.reason for item in refused] == ["ended", "gap", "unanchored"]
    for item in refused:
        assert item.status == "refused"
        assert item.filled == 0
        assert item.fee == 0
        assert "unmodeled" in item.lines()[1]

    crossed_bids, crossed_asks = _book([("0.70", "5")], [("0.60", "5")])
    crossed = taker_fill(
        crossed_bids, crossed_asks, side="BUY", size="1", fee_schedule=SCHEDULE, fees_enabled=True
    )
    assert crossed.reason == "crossed"
    assert crossed.filled == 0

    partial = taker_fill(bids, asks, side="BUY", size="20", fee_schedule=SCHEDULE, fees_enabled=True)
    assert partial.status == "partial"
    assert partial.filled == Decimal("14")
    assert partial.requested == Decimal("20")
    assert partial.touch == Decimal("0.50")
    # 4 @ 0.50 + 10 @ 0.60 = 8.00. The unfilled 6 is not a fill.
    assert partial.notional == Decimal("8.00")
    assert partial.vwap == Decimal("8.00") / Decimal("14")
    assert partial.slippage == partial.vwap - partial.touch
    # 4 * 0.04 * 0.50 * 0.50 = 0.04; 10 * 0.04 * 0.60 * 0.40 = 0.096.
    assert partial.fee == Decimal("0.04000") + Decimal("0.09600")
    assert partial.fee_asset == "USDC"
    assert len(partial.levels) == 2
    for line in partial.lines():
        assert "unmodeled" in line

    slipped = taker_fill(bids, asks, side="BUY", size="6", fee_schedule=SCHEDULE, fees_enabled=True)
    assert slipped.status == "filled"
    assert slipped.filled == Decimal("6")
    assert slipped.notional == Decimal("3.20")
    assert slipped.slippage == Decimal("3.20") / Decimal("6") - Decimal("0.50")

    empty_bids, empty_asks = _book([("0.40", "5")], [])
    none = taker_fill(empty_bids, empty_asks, side="BUY", size="3", fee_schedule=SCHEDULE, fees_enabled=True)
    assert none.status == "unfilled"
    assert none.filled == 0
    assert none.reason == "no_liquidity"


def test_sell_walks_bids_and_fee_is_usdc() -> None:
    bids, asks = _book([("0.40", "2"), ("0.30", "10")], [("0.55", "8")])
    sold = taker_fill(bids, asks, side="SELL", size="20", fee_schedule=SCHEDULE, fees_enabled=True)
    assert sold.status == "partial"
    assert sold.filled == Decimal("12")
    assert sold.touch == Decimal("0.40")
    # 2 @ 0.40 + 10 @ 0.30 = 3.80. The other 8 shares are not a fill.
    assert sold.notional == Decimal("3.80")
    assert sold.vwap == Decimal("3.80") / Decimal("12")
    assert sold.slippage == sold.touch - sold.vwap
    assert sold.fee_asset == "USDC"
    assert sold.fee > 0


def test_latency_uses_recv_time_plus_delay_and_refuses_gap_and_ended() -> None:
    replay = BookReplay(end_after=2)
    schedule = SCHEDULE
    replay.replace(
        1,
        "t",
        1000,
        [{"price": "0.40", "size": "5"}],
        [{"price": "0.50", "size": "10"}],
        kind="book",
        recv_wall="2026-10-03T14:00:00+00:00",
    )
    replay.price_change(
        2,
        "t",
        2000,
        "SELL",
        "0.50",
        "0",
        None,
        None,
        recv_wall="2026-10-03T14:00:01+00:00",
    )
    replay.price_change(
        3,
        "t",
        2000,
        "SELL",
        "0.55",
        "10",
        "0.40",
        "0.55",
        recv_wall="2026-10-03T14:00:01+00:00",
    )

    def fill(decision: str, latency: float):
        asof = replay.book_asof("t", _plus(decision, latency))
        return taker_fill(
            asof["bids"],
            asof["asks"],
            side="BUY",
            size="1",
            fee_schedule=schedule,
            fees_enabled=True,
            anchored=asof["anchored"],
            gap_frozen=asof["gap_frozen"],
            ended=asof["ended"],
        )

    early = fill("2026-10-03T14:00:00.100000+00:00", 0)
    assert early.status == "filled"
    assert early.touch == Decimal("0.50")
    late = fill("2026-10-03T14:00:00.100000+00:00", 1)
    assert late.touch == Decimal("0.55")

    replay.gap(4, ["t"], recv_wall="2026-10-03T14:00:02+00:00")
    frozen = fill("2026-10-03T14:00:02+00:00", 0)
    assert frozen.reason == "gap"

    replay.replace(
        5,
        "t",
        3000,
        [{"price": "0.40", "size": "5"}],
        [{"price": "0.50", "size": "10"}],
        kind="anchor",
        recv_wall="2026-10-03T14:00:03+00:00",
    )
    restored = fill("2026-10-03T14:00:03+00:00", 0)
    assert restored.status == "filled"

    replay.rest(6, "t", None, "periodic", None, None, "HTTP 404", recv_wall="2026-10-03T14:00:04+00:00")
    replay.rest(7, "t", None, "periodic", None, None, "HTTP 404", recv_wall="2026-10-03T14:00:05+00:00")
    still_live = fill("2026-10-03T14:00:04.500000+00:00", 0)
    assert still_live.reason != "ended"
    ended = fill("2026-10-03T14:00:05+00:00", 0)
    assert ended.reason == "ended"


def test_unanchored_before_any_book() -> None:
    replay = BookReplay()
    replay.price_change(
        1,
        "t",
        1000,
        "BUY",
        "0.40",
        "1",
        "0.40",
        "0.60",
        recv_wall="2026-10-03T14:00:00+00:00",
    )
    asof = replay.book_asof("t", "2026-10-03T14:00:01+00:00")
    assert asof["anchored"] is False
    assert asof["gap_frozen"] is False
    fill = taker_fill(
        asof["bids"],
        asof["asks"],
        side="BUY",
        size="1",
        fee_schedule=SCHEDULE,
        fees_enabled=True,
        anchored=False,
    )
    assert fill.reason == "unanchored"


def _plus(wall: str, latency_s: float) -> str:
    from datetime import datetime, timedelta, timezone

    moment = datetime.fromisoformat(wall)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return (moment + timedelta(seconds=latency_s)).isoformat()
