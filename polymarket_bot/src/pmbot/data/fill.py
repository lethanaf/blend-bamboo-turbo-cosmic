"""Taker fill against a local book. No strategy and no order submission.

The fee is the published taker formula, not `base_fee` and not the category table:

    fee = C × rate × p × (1 - p)

`rate` and `exponent` come from that market's `feeSchedule`. Exponent other
than 1 raises. The published formula has no exponent; Market Details says
`exponent` is applied to the price component, which is this formula only when
the exponent is 1.

Rounding is to 5 decimal places. A rounded result below 0.00001 is 0. The
mode is `half-up` or `half-even`; the Fees page does not say which. Rebates
are not subtracted.

Asset, read 2026-10-04 from the current docs (the old learn URLs redirect):

- https://docs.polymarket.com/trading/fees says taker fees are calculated in
  USDC. It does not name a different asset for buys and for sells.
- https://docs.polymarket.com/programs/maker-rebates calls that same amount
  pUSD and pays maker rebates in pUSD. It also does not split buys and sells.
- Neither page still says fees are collected in shares on buys. This function
  charges the formula amount in USDC on both buys and sells, and does not
  reduce the share quantity on a buy. pUSD on the rebates page is the same
  unit under the other name.

Fee-disabled markets (`fees_enabled` false) pay 0 even if a schedule is
present. A rate of 0 pays 0.

Latency is not applied here. `BookReplay.book_asof` selects the book at
decision recv time plus latency; this function only walks the book it is given.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_EVEN, ROUND_HALF_UP

from pmbot.data.bookbuild import Side

FEE_QUANTUM = Decimal("0.00001")
# Measured on the soak tape (Gamma-resolved quote mismatches only). Printed beside fill numbers.
# Not a startup effect: 18 of 30976 are in the first 3s. Not the final minutes before the
# book 404s: of the 21870 with an ended index, the closest is 572s before it.
UNMODELED_FINAL_WINDOW = (
    "unmodeled final window: of 30976 resolved-market quote mismatches, "
    "18 are in the first 3s of the session; "
    "9106 have no ended index and no market_resolved on the tape; "
    "of 21870 with an ended index, none are in the final 5 min "
    "(minimum 572s before the 404, median 3351s); "
    "of those 21870, 1968 are in the final 60s before market_resolved "
    "and the rest are at least 30 min earlier. This number does not adjust for that."
)

_ROUNDING = {
    "half-up": ROUND_HALF_UP,
    "half-even": ROUND_HALF_EVEN,
}


class FeeError(ValueError):
    """The market's feeSchedule cannot be applied with the published formula."""


def _decimal(value: object, name: str) -> Decimal:
    try:
        return Decimal(str(value))
    except Exception as exc:
        raise FeeError(f"{name} is not a decimal: {value!r}") from exc


def taker_fee(
    shares: Decimal,
    price: Decimal,
    fee_schedule: dict | None,
    *,
    fees_enabled: bool | None = None,
    rounding: str = "half-up",
) -> Decimal:
    """USDC fee for one level. Rebates are not credited."""
    if rounding not in _ROUNDING:
        raise FeeError(f"rounding must be half-up or half-even, got {rounding!r}")
    if fees_enabled is False or fees_enabled == 0:
        return Decimal("0")
    if not fee_schedule:
        if fees_enabled is True or fees_enabled == 1:
            raise FeeError("fees_enabled is true but feeSchedule is missing")
        return Decimal("0")
    exponent = fee_schedule.get("exponent", 1)
    if _decimal(exponent, "exponent") != 1:
        raise FeeError(
            f"feeSchedule.exponent {exponent!r} != 1. "
            "The published formula is C × rate × p × (1-p), which is only the exponent-1 curve."
        )
    rate = _decimal(fee_schedule.get("rate", 0), "rate")
    if rate == 0:
        return Decimal("0")
    if shares < 0 or price < 0:
        raise FeeError("shares and price must be >= 0")
    raw = shares * rate * price * (Decimal(1) - price)
    if raw < 0:
        raise FeeError(f"negative fee {raw}")
    rounded = raw.quantize(FEE_QUANTUM, rounding=_ROUNDING[rounding])
    if rounded < FEE_QUANTUM:
        return Decimal("0")
    return rounded


def rounding_sensitivity(
    cases: list[tuple[Decimal, Decimal, dict]],
) -> list[dict]:
    """Half-up versus half-even on the same (shares, price, schedule) cases."""
    rows = []
    for shares, price, schedule in cases:
        half_up = taker_fee(shares, price, schedule, fees_enabled=True, rounding="half-up")
        half_even = taker_fee(shares, price, schedule, fees_enabled=True, rounding="half-even")
        rows.append(
            {
                "shares": shares,
                "price": price,
                "rate": schedule.get("rate"),
                "half_up": half_up,
                "half_even": half_even,
                "differ": half_up != half_even,
            }
        )
    return rows


@dataclass(frozen=True)
class LevelFill:
    price: Decimal
    size: Decimal
    fee_half_up: Decimal
    fee_half_even: Decimal


@dataclass(frozen=True)
class TakerFill:
    status: str
    reason: str | None
    side: str
    requested: Decimal
    filled: Decimal
    touch: Decimal | None
    vwap: Decimal | None
    slippage: Decimal | None
    notional: Decimal
    fee: Decimal
    fee_half_up: Decimal
    fee_half_even: Decimal
    fee_asset: str
    levels: tuple[LevelFill, ...]
    unmodeled: str = UNMODELED_FINAL_WINDOW

    def lines(self) -> list[str]:
        """Every number sits next to the unmodeled final-window sentence."""
        note = self.unmodeled
        return [
            f"status={self.status} reason={self.reason}  [{note}]",
            f"requested={self.requested} filled={self.filled}  [{note}]",
            f"touch={self.touch} vwap={self.vwap} slippage={self.slippage}  [{note}]",
            f"notional={self.notional} fee={self.fee} fee_asset={self.fee_asset}  [{note}]",
            f"fee_half_up={self.fee_half_up} fee_half_even={self.fee_half_even}  [{note}]",
        ]


def _positive_size(size: Decimal | str | int) -> Decimal:
    amount = Decimal(str(size))
    if amount <= 0:
        raise ValueError("size must be positive")
    return amount


def _refused(side: str, requested: Decimal, reason: str) -> TakerFill:
    zero = Decimal("0")
    return TakerFill(
        status="refused",
        reason=reason,
        side=side,
        requested=requested,
        filled=zero,
        touch=None,
        vwap=None,
        slippage=None,
        notional=zero,
        fee=zero,
        fee_half_up=zero,
        fee_half_even=zero,
        fee_asset="USDC",
        levels=(),
    )


def taker_fill(
    bids: Side,
    asks: Side,
    *,
    side: str,
    size: Decimal | str | int,
    fee_schedule: dict | None,
    fees_enabled: bool | None = None,
    rounding: str = "half-up",
    anchored: bool = True,
    gap_frozen: bool = False,
    ended: bool = False,
) -> TakerFill:
    """Walk the opposite side. Unfilled remainder is not a fill.

    BUY lifts asks from the touch up. SELL hits bids from the touch down.
    Slippage is vwap minus touch for a buy, and touch minus vwap for a sell,
    so a positive number is a worse price than the touch. A crossed book,
    an unanchored token, a token frozen by a gap, or an ended token is refused
    and is not a fill.
    """
    if side not in ("BUY", "SELL"):
        raise ValueError("side must be BUY or SELL")
    requested = _positive_size(size)
    if ended:
        return _refused(side, requested, "ended")
    if gap_frozen:
        return _refused(side, requested, "gap")
    if not anchored:
        return _refused(side, requested, "unanchored")
    if bids.best is not None and asks.best is not None and bids.best >= asks.best:
        return _refused(side, requested, "crossed")

    if side == "BUY":
        ladder = sorted(asks.levels.items())
        touch = asks.best
    else:
        ladder = sorted(bids.levels.items(), reverse=True)
        touch = bids.best

    remaining = requested
    filled = Decimal("0")
    notional = Decimal("0")
    fee_up = Decimal("0")
    fee_even = Decimal("0")
    levels: list[LevelFill] = []
    for price, (amount, _price_str, _size_str) in ladder:
        if amount <= 0 or remaining <= 0:
            continue
        take = amount if amount < remaining else remaining
        level_up = taker_fee(take, price, fee_schedule, fees_enabled=fees_enabled, rounding="half-up")
        level_even = taker_fee(take, price, fee_schedule, fees_enabled=fees_enabled, rounding="half-even")
        levels.append(LevelFill(price=price, size=take, fee_half_up=level_up, fee_half_even=level_even))
        filled += take
        notional += take * price
        fee_up += level_up
        fee_even += level_even
        remaining -= take

    if filled == 0:
        return TakerFill(
            status="unfilled",
            reason="no_liquidity",
            side=side,
            requested=requested,
            filled=Decimal("0"),
            touch=touch,
            vwap=None,
            slippage=None,
            notional=Decimal("0"),
            fee=Decimal("0"),
            fee_half_up=Decimal("0"),
            fee_half_even=Decimal("0"),
            fee_asset="USDC",
            levels=(),
        )

    vwap = notional / filled
    if touch is None:
        slippage = None
    elif side == "BUY":
        slippage = vwap - touch
    else:
        slippage = touch - vwap
    chosen = fee_up if rounding == "half-up" else fee_even
    if rounding not in _ROUNDING:
        raise FeeError(f"rounding must be half-up or half-even, got {rounding!r}")
    status = "filled" if filled == requested else "partial"
    return TakerFill(
        status=status,
        reason=None,
        side=side,
        requested=requested,
        filled=filled,
        touch=touch,
        vwap=vwap,
        slippage=slippage,
        notional=notional,
        fee=chosen,
        fee_half_up=fee_up,
        fee_half_even=fee_even,
        fee_asset="USDC",
        levels=tuple(levels),
    )
