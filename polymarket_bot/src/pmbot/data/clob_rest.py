"""Public CLOB reads used by Phase 1. No order routes."""

from __future__ import annotations

import httpx

from pmbot.httputil import get_text


async def get_ok(http: httpx.AsyncClient, attempts: int) -> str:
    response = await get_text(http, "/ok", attempts=attempts)
    return response.text.strip()


async def get_server_time(http: httpx.AsyncClient, attempts: int) -> int:
    response = await get_text(http, "/time", attempts=attempts)
    return int(response.text.strip())


async def get_book(http: httpx.AsyncClient, token_id: str, attempts: int) -> dict:
    response = await get_text(http, "/book", params={"token_id": token_id}, attempts=attempts)
    body = response.json()
    if not isinstance(body, dict):
        raise RuntimeError(f"/book returned {type(body).__name__}")
    return body


async def get_fee_rate(http: httpx.AsyncClient, token_id: str, attempts: int) -> dict:
    """Raw GET /fee-rate body.

    Probe 2026-10-03 returned {"base_fee": 1000}. That integer matches Gamma
    makerBaseFee/takerBaseFee and is not feeSchedule.rate (0.04 on the same
    market). Callers store the object unchanged.
    """
    response = await get_text(http, "/fee-rate", params={"token_id": token_id}, attempts=attempts)
    body = response.json()
    if not isinstance(body, dict):
        raise RuntimeError(f"/fee-rate returned {type(body).__name__}")
    return body
