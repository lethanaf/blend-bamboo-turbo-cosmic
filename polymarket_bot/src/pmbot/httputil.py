"""Small retry helper for public GET requests. Throttle and 5xx are retried."""

from __future__ import annotations

import asyncio
import logging

import httpx

log = logging.getLogger(__name__)

RETRYABLE = {429, 500, 502, 503, 504}
USER_AGENT = "pmbot/0.1 (phase1 public recorder)"


def client(base_url: str, timeout_s: float) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=base_url,
        timeout=httpx.Timeout(timeout_s),
        headers={"User-Agent": USER_AGENT},
    )


async def get_text(
    http: httpx.AsyncClient,
    path: str,
    params: dict | None = None,
    attempts: int = 3,
) -> httpx.Response:
    delay = 0.5
    last: httpx.Response | None = None
    for attempt in range(1, attempts + 1):
        response = await http.get(path, params=params)
        last = response
        if response.status_code not in RETRYABLE or attempt == attempts:
            response.raise_for_status()
            return response
        log.warning("GET %s -> %s, retry %s/%s", path, response.status_code, attempt, attempts)
        await asyncio.sleep(delay)
        delay *= 2
    assert last is not None
    last.raise_for_status()
    return last
