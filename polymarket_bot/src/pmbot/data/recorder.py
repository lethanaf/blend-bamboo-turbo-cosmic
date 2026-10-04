"""Discover markets, snapshot books, and record the market WebSocket."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

import httpx

from pmbot.clock import Clock
from pmbot.config import Config, assert_phase1
from pmbot.data.book import top_of_book
from pmbot.data.clob_rest import get_book, get_fee_rate, get_ok, get_server_time
from pmbot.data.clob_ws import MarketConnection
from pmbot.data.gamma import discover_diversified, discover_markets
from pmbot.data.universe import ending_within
from pmbot.data.lockfile import DataDirLock
from pmbot.data.models import ParsedMarket
from pmbot.data.shard import oversized_groups, shard_markets
from pmbot.data.store import Store
from pmbot.httputil import client

log = logging.getLogger(__name__)


class Recorder:
    def __init__(self, config: Config, clock: Clock | None = None) -> None:
        assert_phase1(config)
        self.config = config
        self.clock = clock or Clock()
        self.store = Store(
            config.sqlite_path,
            config.books_dir,
            clock=self.clock,
            flush_records=config.jsonl_flush_records,
            flush_interval_s=config.jsonl_flush_interval_s,
        )
        self.http: httpx.AsyncClient | None = None
        self.markets: list[ParsedMarket] = []
        self.messages = 0
        self.gaps = 0
        self.snapshots = 0
        self.periodic_rounds = 0
        self.periodic_snapshots = 0
        self.ended: set[str] = set()
        self.polled: set[str] = set()
        self.fail_streak: dict[str, int] = {}
        self.live_connections: list[MarketConnection] = []
        self._conn_tasks: list[asyncio.Task] = []
        self._stop: asyncio.Event | None = None

    async def __aenter__(self) -> Recorder:
        self.http = client(self.config.clob_base, self.config.http_timeout_s)
        return self

    async def __aexit__(self, *_exc) -> None:
        self.store.flush()
        if self.http is not None:
            await self.http.aclose()
        self.store.close()

    async def check_exchange(self) -> None:
        assert self.http is not None
        ok = await get_ok(self.http, self.config.http_attempts)
        server = await get_server_time(self.http, self.config.http_attempts)
        local = int(datetime.now(timezone.utc).timestamp())
        log.info("clob /ok=%s /time=%s local=%s skew_s=%s", ok, server, local, local - server)

    async def catalog(self) -> list[ParsedMarket]:
        assert self.http is not None
        async with client(self.config.gamma_base, self.config.http_timeout_s) as gamma:
            if self.config.universe == "diversified":
                self.markets, events = await discover_diversified(gamma, self.config)
                for event in events:
                    self.store.upsert_neg_risk_event(event)
            else:
                self.markets = await discover_markets(gamma, self.config)
        if not self.markets:
            raise RuntimeError("Gamma returned no recordable markets")
        for market in self.markets:
            self.store.upsert_market(market)
        await self._fetch_fee_rates()
        return self.markets

    async def _fetch_fee_rates(self, token_ids: list[str] | None = None) -> None:
        assert self.http is not None
        if token_ids is None:
            token_ids = [token_id for market in self.markets for token_id in market.token_ids]
        semaphore = asyncio.Semaphore(self.config.rest_book_concurrency)

        async def one(token_id: str) -> None:
            async with semaphore:
                try:
                    payload = await get_fee_rate(self.http, token_id, self.config.http_attempts)
                except Exception:
                    log.exception("fee-rate failed token=%s", token_id)
                    return
                self.store.set_fee_rate(token_id, payload)

        await asyncio.gather(*(one(token_id) for token_id in token_ids))

    async def snapshot_tokens(self, token_ids: list[str], reason: str, connection_id: str | None) -> None:
        assert self.http is not None
        token_ids = [token_id for token_id in token_ids if token_id not in self.ended]
        semaphore = asyncio.Semaphore(self.config.rest_book_concurrency)

        async def one(token_id: str) -> None:
            async with semaphore:
                if token_id in self.ended:
                    return
                try:
                    book = await get_book(self.http, token_id, self.config.http_attempts)
                except httpx.HTTPStatusError as exc:
                    body = exc.response.text[:2000]
                    status = exc.response.status_code
                    stamp = self.clock.stamp()
                    self.store.append(
                        {
                            "kind": "rest_book",
                            "reason": reason,
                            "connection_id": connection_id,
                            "token_id": token_id,
                            "error": f"HTTPStatusError: {exc}",
                            "status": status,
                            "body": body,
                            **stamp,
                        }
                    )
                    log.warning(
                        "book snapshot failed token=%s reason=%s status=%s body=%s",
                        token_id,
                        reason,
                        status,
                        body,
                    )
                    if status == 404:
                        self._note_404(token_id, body)
                    return
                except Exception as exc:
                    stamp = self.clock.stamp()
                    self.store.append(
                        {
                            "kind": "rest_book",
                            "reason": reason,
                            "connection_id": connection_id,
                            "token_id": token_id,
                            "error": f"{type(exc).__name__}: {exc}",
                            **stamp,
                        }
                    )
                    log.warning("book snapshot failed token=%s reason=%s error=%s", token_id, reason, exc)
                    return
                self.fail_streak[token_id] = 0
                stamp = self.clock.stamp()
                path = self.store.append(
                    {
                        "kind": "rest_book",
                        "reason": reason,
                        "connection_id": connection_id,
                        "token_id": token_id,
                        "payload": book,
                        **stamp,
                    }
                )
                top = top_of_book(book)
                server_ts = book.get("timestamp")
                book_hash = book.get("hash")
                self.store.insert_snapshot(
                    token_id=token_id,
                    reason=reason,
                    connection_id=connection_id,
                    top=top,
                    server_timestamp=None if server_ts is None else str(server_ts),
                    book_hash=None if book_hash is None else str(book_hash),
                    recv_wall=str(stamp["recv_wall"]),
                    recv_monotonic_ns=int(stamp["recv_monotonic_ns"]),
                    jsonl_path=path,
                )
                self.snapshots += 1
                if reason == "periodic":
                    self.periodic_snapshots += 1

        await asyncio.gather(*(one(token_id) for token_id in token_ids))

    def _note_404(self, token_id: str, body: str) -> None:
        streak = self.fail_streak.get(token_id, 0) + 1
        self.fail_streak[token_id] = streak
        if streak < self.config.book_404_end_after or token_id in self.ended:
            return
        self.ended.add(token_id)
        self.polled.discard(token_id)
        stamp = self.clock.stamp()
        self.store.append(
            {
                "kind": "token_ended",
                "token_id": token_id,
                "consecutive_404": streak,
                "body": body,
                **stamp,
            }
        )
        log.warning("token ended after %s consecutive 404s token=%s", streak, token_id)
        for conn in self.live_connections:
            if token_id in conn._subscribed_ids:
                asyncio.create_task(conn.unsubscribe_assets([token_id]))

    async def refresh_markets(self) -> list[ParsedMarket]:
        """Subscribe to newly recordable markets until max_markets are live."""
        assert self.http is not None
        live = [market for market in self.markets if any(token not in self.ended for token in market.token_ids)]
        slots = self.config.max_markets - len(live)
        if slots <= 0:
            log.info("gamma refresh slots=0 live=%s", len(live))
            return []
        known = {market.condition_id for market in self.markets}
        async with client(self.config.gamma_base, self.config.http_timeout_s) as gamma:
            found = await discover_markets(gamma, self.config, skip=known, limit=slots)
        if self.config.universe == "diversified":
            now = self.clock.wall()
            found = [
                market
                for market in found
                if not ending_within(market.end_date, now, self.config.exclude_ending_within_s)
            ]
        if not found:
            log.info("gamma refresh added=0 live=%s", len(live))
            return []
        new_tokens: list[str] = []
        for market in found:
            self.store.upsert_market(market)
            self.markets.append(market)
            for token_id in market.token_ids:
                if token_id in self.ended:
                    continue
                self.polled.add(token_id)
                new_tokens.append(token_id)
        await self._fetch_fee_rates(new_tokens)
        await self.snapshot_tokens(new_tokens, reason="bootstrap", connection_id=None)
        await self._subscribe_tokens(new_tokens)
        log.info("gamma refresh added_markets=%s tokens=%s", len(found), len(new_tokens))
        return found

    async def _subscribe_tokens(self, token_ids: list[str]) -> None:
        pending = [token_id for token_id in token_ids if token_id not in self.ended]
        if not pending:
            return
        # Keep a market's tokens together. Group by the market that owns them.
        groups: list[list[str]] = []
        wanted = set(pending)
        for market in self.markets:
            group = [token_id for token_id in market.token_ids if token_id in wanted]
            if group:
                groups.append(group)
        for group in groups:
            conn = self._connection_with_room(group)
            if conn is None:
                self._start_connection(group)
                continue
            await conn.subscribe_assets(group)

    def _connection_with_room(self, token_ids: list[str]) -> MarketConnection | None:
        cap = self.config.ws_assets_per_connection
        for conn in self.live_connections:
            if cap is None or len(conn.asset_ids) + len(token_ids) <= cap:
                return conn
        return None

    def _start_connection(self, token_ids: list[str]) -> MarketConnection:
        if self._stop is None:
            raise RuntimeError("cannot open a socket before run()")
        connection = self._make_connection(len(self.live_connections), token_ids)
        self.live_connections.append(connection)
        self._conn_tasks.append(asyncio.create_task(connection.run(self._stop)))
        log.info("opened websocket conn=%s tokens=%s", connection.connection_id, len(token_ids))
        return connection

    async def on_message(self, connection_id: str, raw: str, scope: str | None = None) -> None:
        stamp = self.clock.stamp()
        record: dict = {
            "kind": "ws",
            "connection_id": connection_id,
            "raw": raw,
            **stamp,
        }
        if scope:
            record["scope"] = scope
        self.store.append(record)
        self.messages += 1

    async def on_session_start(self, connection_id: str, token_ids: list[str], is_reconnect: bool) -> None:
        stamp = self.clock.stamp()
        self.store.append(
            {
                "kind": "session_start",
                "connection_id": connection_id,
                "token_ids": token_ids,
                "is_reconnect": is_reconnect,
                **stamp,
            }
        )
        log.info("session_start conn=%s reconnect=%s tokens=%s", connection_id, is_reconnect, len(token_ids))

    async def on_session_stop(self, connection_id: str, token_ids: list[str], reason: str) -> None:
        stamp = self.clock.stamp()
        self.store.append(
            {
                "kind": "session_stop",
                "connection_id": connection_id,
                "token_ids": token_ids,
                "reason": reason,
                **stamp,
            }
        )
        log.info("session_stop conn=%s reason=%s", connection_id, reason)

    async def on_disconnect(self, connection_id: str, token_ids: list[str], reason: str) -> None:
        stamp = self.clock.stamp()
        path = self.store.append(
            {
                "kind": "gap",
                "connection_id": connection_id,
                "reason": reason,
                "token_ids": token_ids,
                **stamp,
            }
        )
        self.store.insert_gap(
            connection_id=connection_id,
            reason=reason,
            token_ids=token_ids,
            recv_wall=str(stamp["recv_wall"]),
            recv_monotonic_ns=int(stamp["recv_monotonic_ns"]),
            jsonl_path=path,
        )
        self.gaps += 1
        log.warning("gap conn=%s tokens=%s reason=%s", connection_id, len(token_ids), reason)

    async def on_subscribed(self, connection_id: str, token_ids: list[str], is_reconnect: bool) -> None:
        log.info(
            "subscribed conn=%s assets=%s reconnect=%s",
            connection_id,
            len(token_ids),
            is_reconnect,
        )
        if is_reconnect:
            await self.snapshot_tokens(token_ids, reason="reconnect", connection_id=connection_id)

    def connections(self) -> list[MarketConnection]:
        groups = [market.token_ids for market in self.markets]
        if self.config.ws_assets_per_connection is not None:
            for group in oversized_groups(groups, self.config.ws_assets_per_connection):
                log.warning(
                    "market has %s tokens, above ws_assets_per_connection=%s; leaving it on one socket",
                    len(group),
                    self.config.ws_assets_per_connection,
                )
        shards = shard_markets(groups, self.config.ws_assets_per_connection)
        log.info(
            "websocket shards=%s tokens=%s per_connection=%s (docs publish no assets_ids maxItems)",
            len(shards),
            sum(len(shard) for shard in shards),
            self.config.ws_assets_per_connection,
        )
        self.live_connections = [
            self._make_connection(index, shard) for index, shard in enumerate(shards)
        ]
        return self.live_connections

    def _make_connection(self, index: int, token_ids: list[str]) -> MarketConnection:
        return MarketConnection(
            self.config.ws_market,
            str(index),
            token_ids,
            custom_feature_enabled=self.config.custom_feature_enabled,
            ping_interval_s=self.config.ping_interval_s,
            stale_s=self.config.ws_stale_s,
            backoff_initial_s=self.config.reconnect_backoff_initial_s,
            backoff_max_s=self.config.reconnect_backoff_max_s,
            healthy_reset_s=self.config.backoff_reset_after_s,
            on_message=self.on_message,
            on_disconnect=self.on_disconnect,
            on_subscribed=self.on_subscribed,
            on_session_start=self.on_session_start,
            on_session_stop=self.on_session_stop,
        )

    def abort_sockets(self) -> None:
        for conn in getattr(self, "live_connections", []):
            conn.abort()

    async def _reconcile_loop(self, stop: asyncio.Event, token_ids: list[str]) -> None:
        if not self.polled:
            self.polled.update(token_ids)
        interval = self.config.reconcile_interval_s
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
                return
            except TimeoutError:
                current = [token_id for token_id in self.polled if token_id not in self.ended]
                log.info("periodic reconcile tokens=%s ended=%s", len(current), len(self.ended))
                await self.snapshot_tokens(current, reason="periodic", connection_id=None)
                self.periodic_rounds += 1

    async def _gamma_refresh_loop(self, stop: asyncio.Event) -> None:
        interval = self.config.gamma_refresh_interval_s
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
                return
            except TimeoutError:
                try:
                    await self.refresh_markets()
                except Exception:
                    log.exception("gamma refresh failed")

    async def _flush_loop(self, stop: asyncio.Event) -> None:
        interval = self.config.jsonl_flush_interval_s
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
                return
            except TimeoutError:
                self.store.flush()

    async def run(self, stop: asyncio.Event) -> None:
        await self.check_exchange()
        await self.catalog()
        token_ids = [token_id for market in self.markets for token_id in market.token_ids]
        self.polled.update(token_ids)
        await self.snapshot_tokens(token_ids, reason="bootstrap", connection_id=None)
        self._stop = stop
        self._conn_tasks = [asyncio.create_task(conn.run(stop)) for conn in self.connections()]
        extra = [
            asyncio.create_task(self._reconcile_loop(stop, token_ids)),
            asyncio.create_task(self._flush_loop(stop)),
            asyncio.create_task(self._gamma_refresh_loop(stop)),
        ]
        try:
            await stop.wait()
        finally:
            for task in extra:
                task.cancel()
            # stop is already set. Let the socket task write session_stop=shutdown
            # before cancelling anything still stuck.
            if self._conn_tasks:
                _done, pending = await asyncio.wait(self._conn_tasks, timeout=5)
                for task in pending:
                    task.cancel()
            await asyncio.gather(*extra, *self._conn_tasks, return_exceptions=True)
            self.store.flush()
            log.info(
                "stopped messages=%s snapshots=%s periodic_rounds=%s periodic_snapshots=%s gaps=%s",
                self.messages,
                self.snapshots,
                self.periodic_rounds,
                self.periodic_snapshots,
                self.gaps,
            )


async def run_recorder(config: Config, seconds: float | None = None) -> None:
    lock = DataDirLock(config.data_dir)
    stop = asyncio.Event()
    timer: asyncio.Task | None = None
    if seconds is not None:

        async def _stop_later() -> None:
            await asyncio.sleep(seconds)
            stop.set()

        timer = asyncio.create_task(_stop_later())
    loop = asyncio.get_running_loop()
    import signal

    recorder_box: dict[str, Recorder] = {}

    def _on_usr1() -> None:
        recorder = recorder_box.get("recorder")
        log.warning("SIGUSR1 forcing network drop")
        if recorder is not None:
            recorder.abort_sockets()

    for sig_name in ("SIGINT", "SIGTERM"):
        sig = getattr(signal, sig_name, None)
        if sig is None:
            continue
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass
    if hasattr(signal, "SIGUSR1"):
        try:
            loop.add_signal_handler(signal.SIGUSR1, _on_usr1)
        except NotImplementedError:
            pass
    try:
        async with Recorder(config) as recorder:
            recorder_box["recorder"] = recorder
            await recorder.run(stop)
    finally:
        if timer is not None:
            timer.cancel()
        lock.release()
