import asyncio

import httpx

from pmbot.data.clob_rest import get_book, get_fee_rate, get_ok, get_server_time


def test_public_reads_use_documented_paths() -> None:
    seen: list[tuple[str, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.path, dict(request.url.params)))
        if request.url.path == "/ok":
            return httpx.Response(200, text='"OK"')
        if request.url.path == "/time":
            return httpx.Response(200, text="1791031474")
        if request.url.path == "/book":
            return httpx.Response(200, json={"bids": [], "asks": [{"price": "0.42", "size": "3"}], "timestamp": "1"})
        if request.url.path == "/fee-rate":
            return httpx.Response(200, json={"base_fee": 1000})
        return httpx.Response(404)

    async def run() -> None:
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport, base_url="https://clob.polymarket.com") as http:
            assert await get_ok(http, 1) == '"OK"'
            assert await get_server_time(http, 1) == 1791031474
            book = await get_book(http, "token-a", 1)
            fee = await get_fee_rate(http, "token-a", 1)
            assert book["asks"][0]["price"] == "0.42"
            assert fee == {"base_fee": 1000}

    asyncio.run(run())
    assert seen == [
        ("/ok", {}),
        ("/time", {}),
        ("/book", {"token_id": "token-a"}),
        ("/fee-rate", {"token_id": "token-a"}),
    ]
