"""One recorder per data directory. The lock dies with the process.

Unix takes a non-blocking `fcntl.flock`. Windows has no `fcntl`, so the same
file is locked with `msvcrt.locking`. The backend is chosen from the platform
and can be passed in so both paths are testable anywhere.
"""

from __future__ import annotations

import os
from pathlib import Path


class DataDirBusy(RuntimeError):
    """Another recorder holds the data directory lock."""


def default_backend() -> str:
    if os.name == "nt":
        return "msvcrt"
    return "fcntl"


def _acquire_fcntl(fd: int) -> None:
    import fcntl

    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _release_fcntl(fd: int) -> None:
    import fcntl

    fcntl.flock(fd, fcntl.LOCK_UN)


def _acquire_msvcrt(fd: int) -> None:
    import msvcrt

    os.lseek(fd, 0, os.SEEK_SET)
    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)


def _release_msvcrt(fd: int) -> None:
    import msvcrt

    os.lseek(fd, 0, os.SEEK_SET)
    try:
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    except OSError:
        return


class DataDirLock:
    def __init__(self, data_dir: Path, *, backend: str | None = None) -> None:
        self.backend = backend or default_backend()
        if self.backend not in ("fcntl", "msvcrt"):
            raise ValueError(f"unknown lock backend {self.backend!r}")
        data_dir.mkdir(parents=True, exist_ok=True)
        self.path = data_dir / ".recorder.lock"
        # Binary mode so a Windows byte-range lock is not shifted by newline translation.
        self._fd = open(self.path, "a+b")
        try:
            if self.backend == "msvcrt":
                self._prepare_msvcrt_region()
                _acquire_msvcrt(self._fd.fileno())
            else:
                _acquire_fcntl(self._fd.fileno())
        except BlockingIOError as exc:
            self._busy(exc)
        except OSError as exc:
            if self.backend != "msvcrt":
                self._fd.close()
                raise
            self._busy(exc)
        self._write_owner()

    def _prepare_msvcrt_region(self) -> None:
        """Lock one byte at offset 0. The byte has to exist or the lock is vacuous."""
        self._fd.seek(0, os.SEEK_END)
        if self._fd.tell() < 1:
            self._fd.write(b"\0")
            self._fd.flush()
        self._fd.seek(0)

    def _write_owner(self) -> None:
        self._fd.seek(0)
        # Truncating a Windows range lock drops the locked byte. Overwrite instead.
        if self.backend == "fcntl":
            self._fd.truncate()
        self._fd.write(f"pid={os.getpid()}\n".encode())
        self._fd.flush()

    def _busy(self, exc: BaseException) -> None:
        self._fd.close()
        raise DataDirBusy(
            f"another recorder holds {self.path}; refusing to start a second writer"
        ) from exc

    def release(self) -> None:
        if self._fd.closed:
            return
        fd = self._fd.fileno()
        if self.backend == "msvcrt":
            _release_msvcrt(fd)
        else:
            _release_fcntl(fd)
        self._fd.close()
