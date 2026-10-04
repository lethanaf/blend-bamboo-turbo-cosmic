import asyncio

import httpx

from pmbot.config import Config
from pmbot.data.recorder import Recorder
from pmbot.endpoints import CLOB_BASE, GAMMA_BASE, WS_MARKET


def test_periodic_reconcile_writes_rest_book(tmp_path) -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/book"
        seen.append(request.url.params["token_id"])
        return httpx.Response(
            200,
            json={
                "bids": [{"price": "0.2", "size": "1"}],
                "asks": [{"price": "0.3", "size": "1"}],
                "timestamp": "1",
                "hash": "h",
            },
        )

    async def scenario() -> None:
        config = Config(
            live_trading=False,
            gamma_base=GAMMA_BASE,
            clob_base=CLOB_BASE,
            ws_market=WS_MARKET,
            max_markets=30,
            market_order="volume24hr",
            market_order_ascending=False,
            gamma_page_size=100,
            gamma_max_pages=1,
            ws_assets_per_connection=None,
            ping_interval_s=10,
            reconnect_backoff_initial_s=1,
            reconnect_backoff_max_s=30,
            ws_stale_s=30,
            backoff_reset_after_s=30,
            reconcile_interval_s=0.05,
            jsonl_flush_interval_s=1,
            jsonl_flush_records=500,
            rest_book_concurrency=2,
            http_timeout_s=5,
            http_attempts=1,
            custom_feature_enabled=True,
            data_dir=tmp_path,
            log_level="WARNING",
        )
        stop = asyncio.Event()
        async with Recorder(config) as recorder:
            await recorder.http.aclose()
            recorder.http = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://clob.test")

            async def stopper() -> None:
                await asyncio.sleep(0.13)
                stop.set()

            timer = asyncio.create_task(stopper())
            await recorder._reconcile_loop(stop, ["tok"])
            await timer
            recorder.store.flush()
            reasons = [row["reason"] for row in recorder.store.db.execute("SELECT reason FROM snapshots")]
        assert "periodic" in reasons
        assert seen == ["tok"] * len(seen)
        assert len(seen) >= 1

    asyncio.run(scenario())
