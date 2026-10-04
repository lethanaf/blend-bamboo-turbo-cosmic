import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from pmbot.clock import Clock
from pmbot.data.jsonl_log import read_jsonl_gz
from pmbot.data.models import parse_market
from pmbot.data.store import Store

FIXTURE = Path(__file__).parent / "fixtures" / "market.json"


class FakeClock(Clock):
    def __init__(self) -> None:
        self.moment = datetime(2026, 10, 3, 12, 30, tzinfo=timezone.utc)
        self.tick = 100

    def wall(self) -> datetime:
        return self.moment

    def monotonic_ns(self) -> int:
        self.tick += 1
        return self.tick


def test_wal_and_hourly_gzip(tmp_path: Path) -> None:
    clock = FakeClock()
    store = Store(tmp_path / "catalog.sqlite", tmp_path / "books", clock=clock)
    assert store.journal_mode() == "wal"
    market = parse_market(json.loads(FIXTURE.read_text(encoding="utf-8")))
    assert market is not None
    store.upsert_market(market)
    store.set_fee_rate(market.tokens[0].token_id, {"base_fee": 1000})
    first = store.append({"kind": "ws", "raw": "PONG", **clock.stamp()})
    clock.moment += timedelta(hours=1)
    second = store.append({"kind": "gap", "reason": "test", **clock.stamp()})
    store.close()

    assert first.endswith("20261003/12.jsonl.gz")
    assert second.endswith("20261003/13.jsonl.gz")
    hour12 = read_jsonl_gz(Path(first))
    hour13 = read_jsonl_gz(Path(second))
    assert hour12[0]["kind"] == "ws"
    assert hour13[0]["kind"] == "gap"
    for record in hour12 + hour13:
        assert "recv_wall" in record
        assert isinstance(record["recv_monotonic_ns"], int)

    reopened = Store(tmp_path / "catalog.sqlite", tmp_path / "books", clock=clock)
    row = reopened.db.execute(
        "SELECT outcome, fee_rate_raw FROM tokens WHERE token_id=?",
        (market.tokens[0].token_id,),
    ).fetchone()
    assert row["outcome"] == "Yes"
    assert json.loads(row["fee_rate_raw"]) == {"base_fee": 1000}
    other = reopened.db.execute(
        "SELECT fee_rate_raw FROM tokens WHERE token_id=?",
        (market.tokens[1].token_id,),
    ).fetchone()
    assert other["fee_rate_raw"] is None
    reopened.close()


def test_append_requires_timestamps(tmp_path: Path) -> None:
    store = Store(tmp_path / "catalog.sqlite", tmp_path / "books")
    with pytest.raises(ValueError):
        store.append({"kind": "ws", "raw": "{}"})
    store.close()
