import asyncio

import httpx

from pmbot.config import Config
from pmbot.data.jsonl_log import read_jsonl_gz
from pmbot.data.recorder import Recorder
from pmbot.endpoints import CLOB_BASE, GAMMA_BASE, WS_MARKET


def _config(tmp_path) -> Config:
    return Config(
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
        ping_interval_s=30,
        reconnect_backoff_initial_s=0,
        reconnect_backoff_max_s=0,
        ws_stale_s=5,
        backoff_reset_after_s=30,
        reconcile_interval_s=300,
        jsonl_flush_interval_s=10,
        jsonl_flush_records=500,
        rest_book_concurrency=1,
        http_timeout_s=5,
        http_attempts=1,
        custom_feature_enabled=True,
        data_dir=tmp_path,
        log_level="WARNING",
        book_404_end_after=3,
    )


def test_consecutive_404_stops_polling_and_logs_body(tmp_path) -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(404, text='{"error":"no book"}')

    async def scenario() -> list[dict]:
        config = _config(tmp_path)
        async with Recorder(config) as recorder:
            await recorder.http.aclose()
            recorder.http = httpx.AsyncClient(
                transport=httpx.MockTransport(handler),
                base_url="https://clob.test",
            )
            for _ in range(4):
                await recorder.snapshot_tokens(["tok"], reason="periodic", connection_id=None)
            recorder.store.flush()
            records: list[dict] = []
            for path in tmp_path.rglob("*.jsonl.gz"):
                records.extend(read_jsonl_gz(path))
            assert "tok" in recorder.ended
        return records

    records = asyncio.run(scenario())
    assert calls["n"] == 3
    assert sum(1 for row in records if row["kind"] == "token_ended") == 1
    bodies = [row.get("body") for row in records if row["kind"] == "rest_book"]
    assert bodies
    assert all(row.get("status") == 404 and "no book" in row.get("body", "") for row in records if row["kind"] == "rest_book")
