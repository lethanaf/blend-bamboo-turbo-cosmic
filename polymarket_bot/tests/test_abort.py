import asyncio

from websockets.asyncio.server import serve

from pmbot.data.clob_ws import MarketConnection


def test_transport_abort_reconnects() -> None:
    async def scenario() -> None:
        state = {"n": 0}

        async def handler(ws) -> None:
            state["n"] += 1
            await ws.recv()
            if state["n"] >= 2:
                stop.set()
            try:
                await ws.wait_closed()
            except Exception:
                return

        async def noop(*_args) -> None:
            return None

        stop = asyncio.Event()
        async with serve(handler, "127.0.0.1", 0, ping_interval=None) as server:
            port = server.sockets[0].getsockname()[1]
            connection = MarketConnection(
                f"ws://127.0.0.1:{port}",
                "0",
                ["t1"],
                custom_feature_enabled=False,
                ping_interval_s=30,
                stale_s=5,
                backoff_initial_s=0.05,
                backoff_max_s=0.05,
                healthy_reset_s=30,
                on_message=noop,
                on_disconnect=noop,
                on_subscribed=noop,
            )
            task = asyncio.create_task(connection.run(stop))
            for _ in range(50):
                if connection._ws is not None:
                    break
                await asyncio.sleep(0.02)
            connection.abort()
            await asyncio.wait_for(task, timeout=5)
        assert state["n"] >= 2

    asyncio.run(scenario())
