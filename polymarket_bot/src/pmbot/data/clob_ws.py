"""Market-channel client.

Subscribe: {"assets_ids", "type": "market", "custom_feature_enabled"}
Heartbeat: text frame PING every 10 seconds; server answers PONG.
Protocol-level ping is disabled so keepalive matches the docs.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable

import websockets

from pmbot.data.scope import lifecycle_scope

log = logging.getLogger(__name__)

OnMessage = Callable[[str, str, str | None], Awaitable[None]]
OnDisconnect = Callable[[str, list[str], str], Awaitable[None]]
OnSubscribed = Callable[[str, list[str], bool], Awaitable[None]]
OnSessionStart = Callable[[str, list[str], bool], Awaitable[None]]
OnSessionStop = Callable[[str, list[str], str], Awaitable[None]]


class MarketConnection:
    def __init__(
        self,
        url: str,
        connection_id: str,
        asset_ids: list[str],
        *,
        custom_feature_enabled: bool,
        ping_interval_s: float,
        stale_s: float,
        backoff_initial_s: float,
        backoff_max_s: float,
        healthy_reset_s: float,
        on_message: OnMessage,
        on_disconnect: OnDisconnect,
        on_subscribed: OnSubscribed,
        on_session_start: OnSessionStart | None = None,
        on_session_stop: OnSessionStop | None = None,
    ) -> None:
        if not asset_ids:
            raise ValueError("asset_ids is empty")
        self.url = url
        self.connection_id = connection_id
        self.asset_ids = list(asset_ids)
        self._subscribed_ids = set(self.asset_ids)
        self.custom_feature_enabled = custom_feature_enabled
        self.ping_interval_s = ping_interval_s
        self.stale_s = stale_s
        self.backoff_initial_s = backoff_initial_s
        self.backoff_max_s = backoff_max_s
        self.healthy_reset_s = healthy_reset_s
        self.on_message = on_message
        self.on_disconnect = on_disconnect
        self.on_subscribed = on_subscribed
        self.on_session_start = on_session_start
        self.on_session_stop = on_session_stop
        self.was_subscribed = False
        self._session_open = False
        self._next_backoff = backoff_initial_s
        self._ws = None
        self._send_lock = asyncio.Lock()
        self._pending_ops: list[dict] = []

    async def run(self, stop: asyncio.Event) -> None:
        self._next_backoff = self.backoff_initial_s
        while not stop.is_set():
            if not self.asset_ids:
                if await self._sleep(stop, 1):
                    return
                continue
            try:
                await self._one(stop)
            except asyncio.CancelledError:
                await self._end_session("cancelled")
                raise
            except Exception as exc:
                log.warning("ws %s failed: %s", self.connection_id, exc)
                await self._end_session(f"{type(exc).__name__}: {exc}")
                if self.was_subscribed:
                    await self.on_disconnect(
                        self.connection_id,
                        list(self.asset_ids),
                        f"{type(exc).__name__}: {exc}",
                    )
                if await self._sleep(stop, self._next_backoff):
                    return
                self._next_backoff = min(
                    max(self._next_backoff * 2, self.backoff_initial_s),
                    self.backoff_max_s,
                )
                continue
            await self._end_session("shutdown" if stop.is_set() else "session ended")
            return

    async def _one(self, stop: asyncio.Event) -> None:
        async with websockets.connect(
            self.url,
            ping_interval=None,
            open_timeout=20,
            max_size=16 * 1024 * 1024,
        ) as ws:
            self._ws = ws
            try:
                async with self._send_lock:
                    # asset_ids is the source of truth across reconnects, including
                    # tokens added or removed while the socket was down.
                    subscribe = {
                        "assets_ids": list(self.asset_ids),
                        "type": "market",
                        "custom_feature_enabled": self.custom_feature_enabled,
                    }
                    await ws.send(json.dumps(subscribe))
                    self._pending_ops.clear()
                is_reconnect = self.was_subscribed
                self.was_subscribed = True
                self._session_open = True
                if self.on_session_start is not None:
                    await self.on_session_start(self.connection_id, list(self.asset_ids), is_reconnect)
                hook = asyncio.create_task(self._subscribed(is_reconnect))
                ping_task = asyncio.create_task(self._ping(ws, stop))
                reset_task = asyncio.create_task(self._reset_backoff_when_healthy(stop))
                try:
                    while not stop.is_set():
                        raw = await self._recv(ws, stop)
                        if raw is None:
                            break
                        scope = lifecycle_scope(raw, self._subscribed_ids)
                        await self.on_message(self.connection_id, raw, scope)
                finally:
                    ping_task.cancel()
                    reset_task.cancel()
                    await _drain(ping_task, reset_task)
                    await hook
            finally:
                self._ws = None
        if not stop.is_set():
            raise ConnectionError("market socket ended")

    async def subscribe_assets(self, asset_ids: list[str]) -> None:
        """Docs: dynamic subscribe, operation=subscribe, without reconnecting."""
        async with self._send_lock:
            added = [asset_id for asset_id in asset_ids if asset_id not in self._subscribed_ids]
            if not added:
                return
            for asset_id in added:
                self.asset_ids.append(asset_id)
                self._subscribed_ids.add(asset_id)
            await self._send_locked(
                {
                    "assets_ids": added,
                    "operation": "subscribe",
                    "custom_feature_enabled": self.custom_feature_enabled,
                }
            )

    async def unsubscribe_assets(self, asset_ids: list[str]) -> None:
        """Docs: dynamic unsubscribe, operation=unsubscribe."""
        async with self._send_lock:
            remove = [asset_id for asset_id in asset_ids if asset_id in self._subscribed_ids]
            if not remove:
                return
            self._subscribed_ids.difference_update(remove)
            self.asset_ids = [asset_id for asset_id in self.asset_ids if asset_id in self._subscribed_ids]
            await self._send_locked({"assets_ids": remove, "operation": "unsubscribe"})

    async def _send_locked(self, payload: dict) -> None:
        """Caller holds `_send_lock`."""
        ws = self._ws
        if ws is None:
            self._pending_ops.append(payload)
            return
        try:
            await ws.send(json.dumps(payload))
        except Exception:
            log.exception("ws %s dynamic %s failed", self.connection_id, payload.get("operation"))
            self._pending_ops.append(payload)

    def abort(self) -> None:
        """RST the live socket. Used to force a network drop; not a clean close."""
        ws = self._ws
        transport = getattr(ws, "transport", None) if ws is not None else None
        if transport is not None:
            transport.abort()
            log.warning("aborted transport conn=%s", self.connection_id)

    async def _reset_backoff_when_healthy(self, stop: asyncio.Event) -> None:
        if await self._sleep(stop, self.healthy_reset_s):
            return
        self._next_backoff = self.backoff_initial_s

    async def _end_session(self, reason: str) -> None:
        if not self._session_open:
            return
        self._session_open = False
        if self.on_session_stop is None:
            return
        try:
            await self.on_session_stop(self.connection_id, list(self.asset_ids), reason)
        except Exception:
            log.exception("on_session_stop failed conn=%s", self.connection_id)

    async def _subscribed(self, is_reconnect: bool) -> None:
        try:
            await self.on_subscribed(self.connection_id, list(self.asset_ids), is_reconnect)
        except Exception:
            log.exception("on_subscribed failed conn=%s", self.connection_id)

    async def _recv(self, ws, stop: asyncio.Event) -> str | None:
        recv_task = asyncio.create_task(ws.recv())
        stop_task = asyncio.create_task(stop.wait())
        done, pending = await asyncio.wait(
            {recv_task, stop_task},
            timeout=self.stale_s,
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
        await _drain(*pending)
        if not done or recv_task not in done:
            if not done:
                raise ConnectionError(f"no frame for {self.stale_s}s")
            return None
        message = recv_task.result()
        if isinstance(message, bytes):
            return message.decode("utf-8", errors="replace")
        return str(message)

    async def _ping(self, ws, stop: asyncio.Event) -> None:
        while not stop.is_set():
            if await self._sleep(stop, self.ping_interval_s):
                return
            await ws.send("PING")

    async def _sleep(self, stop: asyncio.Event, seconds: float) -> bool:
        if seconds <= 0:
            return stop.is_set()
        try:
            await asyncio.wait_for(stop.wait(), timeout=seconds)
            return True
        except asyncio.TimeoutError:
            return stop.is_set()


async def _drain(*tasks: asyncio.Task) -> None:
    for task in tasks:
        if not task.done():
            task.cancel()
    for task in tasks:
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
