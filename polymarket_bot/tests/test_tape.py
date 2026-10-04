import json
from datetime import datetime, timezone
from pathlib import Path

from pmbot.clock import Clock
from pmbot.data.jsonl_log import HourlyJsonl, read_jsonl_gz


class JumpClock(Clock):
    def __init__(self) -> None:
        self.moment = datetime(2026, 10, 3, 15, 0, tzinfo=timezone.utc)
        self.mono = 0

    def wall(self) -> datetime:
        return self.moment

    def monotonic_ns(self) -> int:
        return self.mono


def _record(kind: str, clock: JumpClock) -> dict:
    clock.mono += 1
    return {"kind": kind, "recv_wall": clock.wall().isoformat(), "recv_monotonic_ns": clock.mono}


def test_batches_until_flush_count(tmp_path: Path) -> None:
    clock = JumpClock()
    tape = HourlyJsonl(tmp_path, clock, flush_records=3, flush_interval_s=10_000)
    tape.write(_record("a", clock))
    tape.write(_record("b", clock))
    assert list(tmp_path.rglob("*.jsonl.gz")) == []
    tape.write(_record("c", clock))
    path = next(tmp_path.rglob("*.jsonl.gz"))
    assert [row["kind"] for row in read_jsonl_gz(path)] == ["a", "b", "c"]
    assert tape.members_written == 1


def test_interval_flush_and_truncated_tail(tmp_path: Path) -> None:
    clock = JumpClock()
    tape = HourlyJsonl(tmp_path, clock, flush_records=100, flush_interval_s=1)
    tape.write(_record("a", clock))
    clock.mono = 2_000_000_000
    tape.write(_record("b", clock))
    path = next(tmp_path.rglob("*.jsonl.gz"))
    assert tape.members_written == 1
    original = path.read_bytes()
    path.write_bytes(original + b"\x1f\x8b\x08\x00truncated-tail")
    assert [row["kind"] for row in read_jsonl_gz(path)] == ["a", "b"]


def test_each_writer_stamps_its_own_session_id(tmp_path: Path) -> None:
    clock = JumpClock()
    first = HourlyJsonl(tmp_path / "a", clock, flush_records=10, flush_interval_s=10_000)
    second = HourlyJsonl(tmp_path / "b", clock, flush_records=10, flush_interval_s=10_000)
    assert first.session_id != second.session_id
    first.write(_record("one", clock))
    second.write(_record("two", clock))
    first.close()
    second.close()
    left = read_jsonl_gz(next((tmp_path / "a").rglob("*.jsonl.gz")))[0]
    right = read_jsonl_gz(next((tmp_path / "b").rglob("*.jsonl.gz")))[0]
    assert left["session_id"] == first.session_id
    assert right["session_id"] == second.session_id
    assert left["session_id"] != right["session_id"]


def test_close_flushes_partial_buffer(tmp_path: Path) -> None:
    clock = JumpClock()
    tape = HourlyJsonl(tmp_path, clock, flush_records=500, flush_interval_s=10_000)
    tape.write(_record("only", clock))
    assert list(tmp_path.rglob("*.jsonl.gz")) == []
    tape.close()
    path = next(tmp_path.rglob("*.jsonl.gz"))
    assert read_jsonl_gz(path)[0]["kind"] == "only"
    json.loads(json.dumps({"ok": True}))
