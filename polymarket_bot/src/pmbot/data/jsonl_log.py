"""Hourly gzip JSONL tapes.

Records are buffered and written as one gzip member every `flush_interval_s`
or `flush_records` lines, and again on flush/close. A crash can lose the
unflushed buffer. Readers stream concatenated members and stop at a truncated
tail instead of copying the unread suffix on every member.
"""

from __future__ import annotations

import gzip
import json
import uuid
import zlib
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path

from pmbot.clock import Clock

GZIP_WBITS = 16 + zlib.MAX_WBITS


def hour_key(moment: datetime) -> str:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).strftime("%Y%m%dT%H")


def iter_jsonl_gz(path: Path) -> Iterator[dict]:
    """Yield records. A truncated final member ends the stream; earlier records stay."""
    try:
        handle = gzip.open(path, "rb")
    except (EOFError, zlib.error, gzip.BadGzipFile):
        return
    try:
        while True:
            try:
                line = handle.readline()
            except (EOFError, zlib.error, gzip.BadGzipFile):
                return
            if not line:
                return
            if not line.endswith(b"\n"):
                # Partial line from a truncated member. Drop it.
                return
            stripped = line.strip()
            if not stripped:
                continue
            try:
                yield json.loads(stripped)
            except json.JSONDecodeError:
                continue
    finally:
        handle.close()


def read_jsonl_gz(path: Path) -> list[dict]:
    return list(iter_jsonl_gz(path))


class HourlyJsonl:
    def __init__(
        self,
        root: Path,
        clock: Clock | None = None,
        *,
        flush_records: int = 500,
        flush_interval_s: float = 1.0,
    ) -> None:
        if flush_records < 1:
            raise ValueError("flush_records must be >= 1")
        if flush_interval_s < 0:
            raise ValueError("flush_interval_s must be >= 0")
        self.root = root
        self.clock = clock or Clock()
        self.flush_records = flush_records
        self.flush_interval_s = flush_interval_s
        self.root.mkdir(parents=True, exist_ok=True)
        self._buf: list[bytes] = []
        self._buf_path: Path | None = None
        self._last_flush_ns: int | None = None
        self.members_written = 0
        self.uncompressed_bytes = 0
        # One id per writer process. Two recorders sharing a directory no longer
        # look like one connection_id "0".
        self.session_id = str(uuid.uuid4())

    def write(self, record: dict) -> Path:
        if "recv_wall" not in record or "recv_monotonic_ns" not in record:
            raise ValueError("stored messages require recv_wall and recv_monotonic_ns")
        record.setdefault("session_id", self.session_id)
        path = self._path_for(self.clock.wall())
        if self._buf_path is not None and path != self._buf_path:
            self.flush()
        line = (json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        self._buf.append(line)
        self._buf_path = path
        now = self.clock.monotonic_ns()
        if self._last_flush_ns is None:
            self._last_flush_ns = now
        elapsed_s = (now - self._last_flush_ns) / 1_000_000_000
        if len(self._buf) >= self.flush_records or elapsed_s >= self.flush_interval_s:
            self.flush()
        return path

    def flush(self) -> None:
        if not self._buf or self._buf_path is None:
            self._last_flush_ns = self.clock.monotonic_ns()
            return
        payload = b"".join(self._buf)
        path = self._buf_path
        path.parent.mkdir(parents=True, exist_ok=True)
        member = zlib.compress(payload, wbits=GZIP_WBITS)
        with path.open("ab") as handle:
            handle.write(member)
        self._buf.clear()
        self._last_flush_ns = self.clock.monotonic_ns()
        self.members_written += 1
        self.uncompressed_bytes += len(payload)

    def close(self) -> None:
        self.flush()

    def _path_for(self, moment: datetime) -> Path:
        moment = moment.astimezone(timezone.utc)
        directory = self.root / moment.strftime("%Y%m%d")
        return directory / f"{moment.strftime('%H')}.jsonl.gz"
