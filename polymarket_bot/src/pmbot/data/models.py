"""Parsed Gamma markets. Raw JSON is kept so unused fields are not dropped."""

from __future__ import annotations

import json
from dataclasses import dataclass


@dataclass(frozen=True)
class Token:
    token_id: str
    outcome: str
    outcome_index: int


@dataclass(frozen=True)
class ParsedMarket:
    condition_id: str
    gamma_id: str
    slug: str
    question: str
    end_date: str | None
    active: bool
    closed: bool
    enable_order_book: bool
    accepting_orders: bool
    neg_risk: bool
    uma_resolution_statuses: str | None
    fees_enabled: bool | None
    fee_type: str | None
    fee_schedule: dict | None
    maker_base_fee: int | None
    taker_base_fee: int | None
    order_min_size: str | None
    order_price_min_tick_size: str | None
    volume_24hr: float | None
    liquidity_num: float | None
    tokens: tuple[Token, ...]
    raw: dict

    @property
    def token_ids(self) -> list[str]:
        return [token.token_id for token in self.tokens]


def parse_json_list(value: object) -> list:
    if isinstance(value, list):
        return value
    if isinstance(value, str) and value.strip():
        try:
            loaded = json.loads(value)
        except json.JSONDecodeError:
            return []
        return loaded if isinstance(loaded, list) else []
    return []


def _optional_int(value: object) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _optional_float(value: object) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_market(raw: dict, *, require_active: bool = True) -> ParsedMarket | None:
    """Return a recordable market, or None.

    Recordable means the order book is enabled, orders are accepted, the
    market is not closed, and at least one CLOB token id is present.
    `require_active` is the discovery default. Neg-risk siblings that Gamma
    marks inactive but still accepting orders are parsed with it off.
    """
    if not isinstance(raw, dict):
        return None
    if raw.get("closed") is True:
        return None
    if require_active and raw.get("active") is not True:
        return None
    if raw.get("enableOrderBook") is not True:
        return None
    if raw.get("acceptingOrders") is not True:
        return None
    condition_id = raw.get("conditionId")
    if not isinstance(condition_id, str) or not condition_id:
        return None
    token_ids = [str(item) for item in parse_json_list(raw.get("clobTokenIds")) if str(item)]
    if not token_ids:
        return None
    outcomes = [str(item) for item in parse_json_list(raw.get("outcomes"))]
    tokens = tuple(
        Token(
            token_id=token_id,
            outcome=outcomes[index] if index < len(outcomes) else str(index),
            outcome_index=index,
        )
        for index, token_id in enumerate(token_ids)
    )
    fee_schedule = raw.get("feeSchedule")
    if fee_schedule is not None and not isinstance(fee_schedule, dict):
        fee_schedule = None
    uma = raw.get("umaResolutionStatuses")
    uma_text = uma if isinstance(uma, str) else (json.dumps(uma) if uma is not None else None)
    tick = raw.get("orderPriceMinTickSize")
    min_size = raw.get("orderMinSize")
    return ParsedMarket(
        condition_id=condition_id,
        gamma_id=str(raw.get("id") or ""),
        slug=str(raw.get("slug") or ""),
        question=str(raw.get("question") or ""),
        end_date=raw.get("endDate") if isinstance(raw.get("endDate"), str) else None,
        active=raw.get("active") is True,
        closed=False,
        enable_order_book=True,
        accepting_orders=True,
        neg_risk=bool(raw.get("negRisk")),
        uma_resolution_statuses=uma_text,
        fees_enabled=raw.get("feesEnabled") if isinstance(raw.get("feesEnabled"), bool) else None,
        fee_type=raw.get("feeType") if isinstance(raw.get("feeType"), str) else None,
        fee_schedule=fee_schedule,
        maker_base_fee=_optional_int(raw.get("makerBaseFee")),
        taker_base_fee=_optional_int(raw.get("takerBaseFee")),
        order_min_size=None if min_size is None else str(min_size),
        order_price_min_tick_size=None if tick is None else str(tick),
        volume_24hr=_optional_float(raw.get("volume24hr")),
        liquidity_num=_optional_float(raw.get("liquidityNum")),
        tokens=tokens,
        raw=raw,
    )


def select_markets(payloads: list[dict], max_markets: int) -> list[ParsedMarket]:
    chosen: list[ParsedMarket] = []
    seen: set[str] = set()
    for payload in payloads:
        parsed = parse_market(payload)
        if parsed is None or parsed.condition_id in seen:
            continue
        seen.add(parsed.condition_id)
        chosen.append(parsed)
        if len(chosen) >= max_markets:
            break
    return chosen
