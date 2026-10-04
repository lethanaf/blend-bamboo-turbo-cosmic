"""One recorder per data directory. The lock dies with the process."""

from __future__ import annotations

import fcntl
import os
from pathlib import Path


class DataDirBusy(RuntimeError):
    """Another recorder holds the data directory lock."""


class DataDirLock:
    def __init__(self, data_dir: Path) -> None:
        data_dir.mkdir(parents=True, exist_ok=True)
        self.path = data_dir / ".recorder.lock"
        self._fd = open(self.path, "a+", encoding="utf-8")
        try:
            fcntl.flock(self._fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self._fd.close()
            raise DataDirBusy(
                f"another recorder holds {self.path}; refusing to start a second writer"
            ) from exc
        self._fd.seek(0)
        self._fd.truncate()
        self._fd.write(f"pid={os.getpid()}\n")
        self._fd.flush()

    def release(self) -> None:
        if self._fd.closed:
            return
        fcntl.flock(self._fd.fileno(), fcntl.LOCK_UN)
        self._fd.close()
