import asyncio
import time

from websockets.asyncio.server import serve

from pmbot.data.clob_ws import MarketConnection


def test_healthy_session_resets_backoff_to_initial() -> None:
    async def scenario() -> None:
        subscribe_at: list[float] = []
        state = {"n": 0}

        async def handler(ws) -> None:
            state["n"] += 1
            await ws.recv()
            subscribe_at.append(time.perf_counter())
            if state["n"] == 1:
                await ws.close()
                return
            if state["n"] == 2:
                await asyncio.sleep(0.08)
                await ws.close()
                return
            await asyncio.sleep(0.05)
            stop.set()
            await ws.wait_closed()

        async def on_message(connection_id: str, raw: str, scope: str | None) -> None:
            return None

        async def on_disconnect(connection_id: str, token_ids: list[str], reason: str) -> None:
            return None

        async def on_subscribed(connection_id: str, token_ids: list[str], is_reconnect: bool) -> None:
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
                backoff_initial_s=0.2,
                backoff_max_s=5,
                healthy_reset_s=0.05,
                on_message=on_message,
                on_disconnect=on_disconnect,
                on_subscribed=on_subscribed,
            )
            await asyncio.wait_for(connection.run(stop), timeout=5)
        assert len(subscribe_at) >= 3
        gap = subscribe_at[2] - subscribe_at[1]
        # Session 2 stays up 0.08s, which resets backoff to 0.2s.
        # Without the reset the wait would be 0.4s (0.08 + 0.4 = 0.48).
        assert gap < 0.40, gap

    asyncio.run(scenario())
