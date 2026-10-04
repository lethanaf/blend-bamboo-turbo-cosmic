import asyncio
import json

import httpx
from websockets.asyncio.server import serve

from pmbot.config import Config
from pmbot.data.clob_ws import MarketConnection
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
        jsonl_flush_interval_s=1,
        jsonl_flush_records=500,
        rest_book_concurrency=2,
        http_timeout_s=5,
        http_attempts=1,
        custom_feature_enabled=True,
        data_dir=tmp_path,
        log_level="WARNING",
    )


def test_reconnect_writes_gap_then_rest_snapshot(tmp_path) -> None:
    books: list[str] = []
    subscribes: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/book"
        token_id = request.url.params["token_id"]
        books.append(token_id)
        return httpx.Response(
            200,
            json={
                "asset_id": token_id,
                "market": "0xabc",
                "bids": [{"price": "0.40", "size": "5"}],
                "asks": [{"price": "0.70", "size": "1"}, {"price": "0.55", "size": "2"}],
                "timestamp": "99",
                "hash": "h",
            },
        )

    async def scenario() -> None:
        state = {"n": 0}

        async def ws_handler(ws) -> None:
            state["n"] += 1
            subscribes.append(json.loads(await ws.recv()))
            await ws.send(
                json.dumps(
                    {
                        "event_type": "book",
                        "asset_id": "t1",
                        "bids": [{"price": "0.40", "size": "5"}],
                        "asks": [{"price": "0.55", "size": "2"}],
                    }
                )
            )
            if state["n"] == 1:
                await ws.close()
                return
            await ws.wait_closed()

        async with serve(ws_handler, "127.0.0.1", 0, ping_interval=None) as server:
            port = server.sockets[0].getsockname()[1]
            config = _config(tmp_path)
            async with Recorder(config) as recorder:
                await recorder.http.aclose()
                recorder.http = httpx.AsyncClient(
                    transport=httpx.MockTransport(handler),
                    base_url="https://clob.test",
                )
                stop = asyncio.Event()

                async def subscribed(connection_id: str, token_ids: list[str], is_reconnect: bool) -> None:
                    await recorder.on_subscribed(connection_id, token_ids, is_reconnect)
                    if is_reconnect:
                        stop.set()

                connection = MarketConnection(
                    f"ws://127.0.0.1:{port}",
                    "0",
                    ["t1"],
                    custom_feature_enabled=True,
                    ping_interval_s=30,
                    stale_s=5,
                    backoff_initial_s=0,
                    backoff_max_s=0,
                    healthy_reset_s=30,
                    on_message=recorder.on_message,
                    on_disconnect=recorder.on_disconnect,
                    on_subscribed=subscribed,
                    on_session_start=recorder.on_session_start,
                    on_session_stop=recorder.on_session_stop,
                )
                await asyncio.wait_for(connection.run(stop), timeout=5)
                assert recorder.gaps == 1
                assert recorder.snapshots == 1
                gap_rows = recorder.store.db.execute("SELECT reason, token_ids_json FROM gaps").fetchall()
                assert len(gap_rows) == 1
                assert "t1" in gap_rows[0]["token_ids_json"]
                snap = recorder.store.db.execute(
                    "SELECT reason, best_bid, best_ask, connection_id FROM snapshots"
                ).fetchone()
                assert snap["reason"] == "reconnect"
                assert snap["best_bid"] == "0.40"
                assert snap["best_ask"] == "0.55"
                assert snap["connection_id"] == "0"
                recorder.store.flush()
                tape = list((tmp_path / "books").rglob("*.jsonl.gz"))
                records = []
                for path in tape:
                    records.extend(read_jsonl_gz(path))
                kinds = [record["kind"] for record in records]
                assert "gap" in kinds
                assert "rest_book" in kinds
                assert "ws" in kinds
                assert "session_start" in kinds
                assert "session_stop" in kinds
                starts = [r for r in records if r["kind"] == "session_start"]
                assert starts[0]["is_reconnect"] is False
                assert starts[-1]["is_reconnect"] is True
                for record in records:
                    assert isinstance(record["recv_wall"], str) and record["recv_wall"]
                    assert isinstance(record["recv_monotonic_ns"], int)
                gap_at = next(i for i, record in enumerate(records) if record["kind"] == "gap")
                rest_at = next(i for i, record in enumerate(records) if record["kind"] == "rest_book")
                assert gap_at < rest_at

    asyncio.run(scenario())
    assert books == ["t1"]
    assert len(subscribes) == 2
    assert subscribes[0]["type"] == "market"
    assert subscribes[0]["assets_ids"] == ["t1"]
    assert subscribes[0]["custom_feature_enabled"] is True
    assert subscribes[1]["assets_ids"] == ["t1"]
