"""Receive timestamps. Wall time is UTC; monotonic is for ordering inside a process."""

from __future__ import annotations

import time
from datetime import datetime, timezone


class Clock:
    def wall(self) -> datetime:
        return datetime.now(timezone.utc)

    def monotonic_ns(self) -> int:
        return time.monotonic_ns()

    def stamp(self) -> dict[str, str | int]:
        return {
            "recv_wall": self.wall().isoformat(),
            "recv_monotonic_ns": self.monotonic_ns(),
        }
