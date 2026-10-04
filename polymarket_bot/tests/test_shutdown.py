import asyncio
import logging
import signal

from dataclasses import replace

from websockets.asyncio.server import serve

from pmbot.data.clob_ws import MarketConnection
from pmbot.data.jsonl_log import read_jsonl_gz
from pmbot.data.recorder import Recorder, install_stop_signals
from test_reconnect import _config


class _Tokens:
    def __init__(self, token_ids: list[str]) -> None:
        self.token_ids = token_ids


class _Loop:
    def __init__(self) -> None:
        self.handlers: dict = {}

    def add_signal_handler(self, sig, callback) -> None:
        self.handlers[sig] = callback

    def remove_signal_handler(self, sig) -> None:
        self.handlers.pop(sig, None)

    def call_soon_threadsafe(self, callback) -> None:
        callback()


def test_add_signal_handler_sets_stop_without_replacing_signal_signal() -> None:
    before = signal.getsignal(signal.SIGINT)
    stop = asyncio.Event()
    loop = _Loop()
    restore = install_stop_signals(loop, stop)
    try:
        assert signal.SIGINT in loop.handlers
        assert signal.getsignal(signal.SIGINT) is before
        loop.handlers[signal.SIGINT]()
        assert stop.is_set()
    finally:
        restore()
    assert signal.getsignal(signal.SIGINT) is before


def test_ctrl_c_fallback_writes_session_stop_and_flushes(tmp_path) -> None:
    async def scenario() -> None:
        async def ws_handler(ws) -> None:
            await ws.recv()
            try:
                await ws.wait_closed()
            except Exception:
                return

        async with serve(ws_handler, "127.0.0.1", 0, ping_interval=None) as server:
            port = server.sockets[0].getsockname()[1]
            config = _config(tmp_path)
            async with Recorder(config) as recorder:
                stop = asyncio.Event()
                started: list[bool] = []

                async def on_start(connection_id: str, token_ids: list[str], is_reconnect: bool) -> None:
                    await recorder.on_session_start(connection_id, token_ids, is_reconnect)
                    started.append(True)

                class Shim:
                    def add_signal_handler(self, sig, callback) -> None:
                        raise NotImplementedError

                    def remove_signal_handler(self, sig) -> None:
                        raise AssertionError(sig)

                    def call_soon_threadsafe(self, callback) -> None:
                        callback()

                restore = install_stop_signals(Shim(), stop)
                try:
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
                        on_message=recorder.on_message,
                        on_disconnect=recorder.on_disconnect,
                        on_subscribed=recorder.on_subscribed,
                        on_session_start=on_start,
                        on_session_stop=recorder.on_session_stop,
                    )
                    task = asyncio.create_task(connection.run(stop))
                    for _ in range(50):
                        if started:
                            break
                        await asyncio.sleep(0.02)
                    assert started
                    await asyncio.sleep(0.05)
                    handler = signal.getsignal(signal.SIGINT)
                    assert callable(handler)
                    handler(signal.SIGINT, None)
                    await asyncio.wait_for(task, timeout=5)
                    # run() flushes in its finally. Do that same flush here.
                    recorder.store.flush()
                    assert recorder.store.tape.bytes_written > 0
                    records = []
                    for path in (tmp_path / "books").rglob("*.jsonl.gz"):
                        records.extend(read_jsonl_gz(path))
                    stops = [record for record in records if record["kind"] == "session_stop"]
                    assert stops
                    assert stops[-1]["reason"] == "shutdown"
                    assert stop.is_set()
                finally:
                    restore()

    asyncio.run(scenario())


def test_progress_line_every_interval(tmp_path, caplog) -> None:
    config = _config(tmp_path)
    recorder = Recorder(config)
    try:
        recorder.messages = 4
        recorder.gaps = 1
        recorder.ended.add("dead")
        recorder.markets = [_Tokens(["live", "dead"]), _Tokens(["dead"])]
        recorder.store.tape.bytes_written = 3 * 1024 * 1024
        assert (
            recorder.progress_line()
            == "progress messages=4 markets_live=1 ended=1 gaps=1 mb_written=3.00"
        )

        async def run() -> None:
            stop = asyncio.Event()
            task = asyncio.create_task(recorder._progress_loop(stop, interval_s=0.05))
            for _ in range(40):
                if any(rec.message.startswith("progress ") for rec in caplog.records):
                    break
                await asyncio.sleep(0.02)
            stop.set()
            await asyncio.wait_for(task, timeout=2)

        caplog.set_level(logging.INFO, logger="pmbot.data.recorder")
        asyncio.run(run())
        assert any("messages=4" in rec.message and "mb_written=3.00" in rec.message for rec in caplog.records)
    finally:
        recorder.store.close()


def test_diversified_refresh_filters_inside_discovery(tmp_path, monkeypatch) -> None:
    seen: dict = {}

    async def fake_discover(http, config, *, skip=None, limit=None, now=None, exclude_ending_within_s=None):
        seen["limit"] = limit
        seen["exclude"] = exclude_ending_within_s
        seen["now"] = now
        seen["skip"] = set(skip or ())
        if skip is not None:
            skip.add("0xshort")
        return []

    monkeypatch.setattr("pmbot.data.recorder.discover_markets", fake_discover)
    config = replace(_config(tmp_path), universe="diversified", max_markets=2, exclude_ending_within_s=21600)

    async def run() -> None:
        async with Recorder(config) as recorder:
            recorder.skipped_ending.add("0xold")
            added = await recorder.refresh_markets()
            assert added == []
            assert recorder.skipped_ending == {"0xold", "0xshort"}

    asyncio.run(run())
    assert seen["limit"] == 2
    assert seen["exclude"] == 21600
    assert seen["now"] is not None
    assert seen["skip"] == {"0xold"}

