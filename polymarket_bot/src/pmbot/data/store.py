"""SQLite WAL catalog plus the hourly gzip JSONL tape."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from pmbot.clock import Clock
from pmbot.data.jsonl_log import HourlyJsonl
from pmbot.data.models import ParsedMarket
from pmbot.data.universe import NegRiskEvent

SCHEMA = """
CREATE TABLE IF NOT EXISTS markets (
    condition_id TEXT PRIMARY KEY,
    gamma_id TEXT,
    slug TEXT,
    question TEXT,
    end_date TEXT,
    active INTEGER NOT NULL,
    closed INTEGER NOT NULL,
    enable_order_book INTEGER NOT NULL,
    accepting_orders INTEGER NOT NULL,
    neg_risk INTEGER NOT NULL,
    uma_resolution_statuses TEXT,
    fees_enabled INTEGER,
    fee_type TEXT,
    fee_schedule_json TEXT,
    maker_base_fee INTEGER,
    taker_base_fee INTEGER,
    order_min_size TEXT,
    order_price_min_tick_size TEXT,
    volume_24hr REAL,
    liquidity_num REAL,
    raw_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tokens (
    token_id TEXT PRIMARY KEY,
    condition_id TEXT NOT NULL,
    outcome TEXT,
    outcome_index INTEGER NOT NULL,
    fee_rate_raw TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    token_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    connection_id TEXT,
    best_bid TEXT,
    best_ask TEXT,
    best_bid_size TEXT,
    best_ask_size TEXT,
    bid_levels INTEGER,
    ask_levels INTEGER,
    server_timestamp TEXT,
    book_hash TEXT,
    recv_wall TEXT NOT NULL,
    recv_monotonic_ns INTEGER NOT NULL,
    jsonl_path TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS gaps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    connection_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    token_ids_json TEXT NOT NULL,
    recv_wall TEXT NOT NULL,
    recv_monotonic_ns INTEGER NOT NULL,
    jsonl_path TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS neg_risk_events (
    neg_risk_market_id TEXT PRIMARY KEY,
    event_id TEXT,
    event_slug TEXT,
    title TEXT,
    end_date TEXT,
    outcome_count INTEGER NOT NULL,
    augmented INTEGER NOT NULL,
    complete INTEGER NOT NULL,
    volume_24hr REAL,
    sibling_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


class Store:
    def __init__(
        self,
        sqlite_path: Path,
        books_dir: Path,
        clock: Clock | None = None,
        *,
        flush_records: int = 500,
        flush_interval_s: float = 1.0,
    ) -> None:
        self.clock = clock or Clock()
        sqlite_path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(sqlite_path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript(SCHEMA)
        self.tape = HourlyJsonl(
            books_dir,
            clock=self.clock,
            flush_records=flush_records,
            flush_interval_s=flush_interval_s,
        )

    def flush(self) -> None:
        self.tape.flush()

    def journal_mode(self) -> str:
        row = self.db.execute("PRAGMA journal_mode").fetchone()
        return str(row[0])

    def append(self, record: dict) -> str:
        return str(self.tape.write(record))

    def upsert_market(self, market: ParsedMarket) -> None:
        updated = self.clock.wall().isoformat()
        fees_enabled = None if market.fees_enabled is None else int(market.fees_enabled)
        self.db.execute(
            """
            INSERT INTO markets (
                condition_id, gamma_id, slug, question, end_date, active, closed,
                enable_order_book, accepting_orders, neg_risk, uma_resolution_statuses,
                fees_enabled, fee_type, fee_schedule_json, maker_base_fee, taker_base_fee,
                order_min_size, order_price_min_tick_size, volume_24hr, liquidity_num,
                raw_json, updated_at
            ) VALUES (?, ?, ?, ?, ?, 1, 0, 1, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(condition_id) DO UPDATE SET
                gamma_id=excluded.gamma_id,
                slug=excluded.slug,
                question=excluded.question,
                end_date=excluded.end_date,
                active=excluded.active,
                closed=excluded.closed,
                enable_order_book=excluded.enable_order_book,
                accepting_orders=excluded.accepting_orders,
                neg_risk=excluded.neg_risk,
                uma_resolution_statuses=excluded.uma_resolution_statuses,
                fees_enabled=excluded.fees_enabled,
                fee_type=excluded.fee_type,
                fee_schedule_json=excluded.fee_schedule_json,
                maker_base_fee=excluded.maker_base_fee,
                taker_base_fee=excluded.taker_base_fee,
                order_min_size=excluded.order_min_size,
                order_price_min_tick_size=excluded.order_price_min_tick_size,
                volume_24hr=excluded.volume_24hr,
                liquidity_num=excluded.liquidity_num,
                raw_json=excluded.raw_json,
                updated_at=excluded.updated_at
            """,
            (
                market.condition_id,
                market.gamma_id,
                market.slug,
                market.question,
                market.end_date,
                int(market.neg_risk),
                market.uma_resolution_statuses,
                fees_enabled,
                market.fee_type,
                None if market.fee_schedule is None else json.dumps(market.fee_schedule, separators=(",", ":")),
                market.maker_base_fee,
                market.taker_base_fee,
                market.order_min_size,
                market.order_price_min_tick_size,
                market.volume_24hr,
                market.liquidity_num,
                json.dumps(market.raw, separators=(",", ":")),
                updated,
            ),
        )
        for token in market.tokens:
            self.db.execute(
                """
                INSERT INTO tokens (token_id, condition_id, outcome, outcome_index, fee_rate_raw, updated_at)
                VALUES (?, ?, ?, ?, NULL, ?)
                ON CONFLICT(token_id) DO UPDATE SET
                    condition_id=excluded.condition_id,
                    outcome=excluded.outcome,
                    outcome_index=excluded.outcome_index,
                    updated_at=excluded.updated_at
                """,
                (token.token_id, market.condition_id, token.outcome, token.outcome_index, updated),
            )
        self.db.commit()

    def set_fee_rate(self, token_id: str, fee_rate: dict) -> None:
        self.db.execute(
            "UPDATE tokens SET fee_rate_raw=?, updated_at=? WHERE token_id=?",
            (json.dumps(fee_rate, separators=(",", ":")), self.clock.wall().isoformat(), token_id),
        )
        self.db.commit()

    def upsert_neg_risk_event(self, event: NegRiskEvent) -> None:
        siblings = [
            {
                "condition_id": sibling.condition_id,
                "slug": sibling.slug,
                "question": sibling.question,
                "group_item_title": sibling.group_item_title,
                "yes_token_id": sibling.yes_token_id,
                "placeholder": sibling.placeholder,
                "augmented": sibling.augmented,
            }
            for sibling in event.siblings
        ]
        self.db.execute(
            """
            INSERT INTO neg_risk_events (
                neg_risk_market_id, event_id, event_slug, title, end_date,
                outcome_count, augmented, complete, volume_24hr, sibling_json, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(neg_risk_market_id) DO UPDATE SET
                event_id=excluded.event_id,
                event_slug=excluded.event_slug,
                title=excluded.title,
                end_date=excluded.end_date,
                outcome_count=excluded.outcome_count,
                augmented=excluded.augmented,
                complete=excluded.complete,
                volume_24hr=excluded.volume_24hr,
                sibling_json=excluded.sibling_json,
                updated_at=excluded.updated_at
            """,
            (
                event.neg_risk_market_id,
                event.event_id,
                event.slug,
                event.title,
                event.end_date,
                event.outcome_count,
                int(event.augmented),
                int(event.complete),
                event.volume_24hr,
                json.dumps(siblings, separators=(",", ":")),
                self.clock.wall().isoformat(),
            ),
        )
        self.db.commit()

    def insert_snapshot(
        self,
        *,
        token_id: str,
        reason: str,
        connection_id: str | None,
        top: dict,
        server_timestamp: str | None,
        book_hash: str | None,
        recv_wall: str,
        recv_monotonic_ns: int,
        jsonl_path: str,
    ) -> None:
        self.db.execute(
            """
            INSERT INTO snapshots (
                token_id, reason, connection_id, best_bid, best_ask, best_bid_size,
                best_ask_size, bid_levels, ask_levels, server_timestamp, book_hash,
                recv_wall, recv_monotonic_ns, jsonl_path
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                token_id,
                reason,
                connection_id,
                top.get("best_bid"),
                top.get("best_ask"),
                top.get("best_bid_size"),
                top.get("best_ask_size"),
                top.get("bid_levels"),
                top.get("ask_levels"),
                server_timestamp,
                book_hash,
                recv_wall,
                recv_monotonic_ns,
                jsonl_path,
            ),
        )
        self.db.commit()

    def insert_gap(
        self,
        *,
        connection_id: str,
        reason: str,
        token_ids: list[str],
        recv_wall: str,
        recv_monotonic_ns: int,
        jsonl_path: str,
    ) -> None:
        self.db.execute(
            """
            INSERT INTO gaps (
                connection_id, reason, token_ids_json, recv_wall, recv_monotonic_ns, jsonl_path
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                connection_id,
                reason,
                json.dumps(token_ids),
                recv_wall,
                recv_monotonic_ns,
                jsonl_path,
            ),
        )
        self.db.commit()

    def close(self) -> None:
        self.tape.close()
        self.db.close()
