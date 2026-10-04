import os
import sys
import types
from pathlib import Path

import pytest

from pmbot.data.lockfile import DataDirBusy, DataDirLock, default_backend


def test_second_writer_is_refused(tmp_path: Path) -> None:
    first = DataDirLock(tmp_path)
    with pytest.raises(DataDirBusy):
        DataDirLock(tmp_path)
    first.release()
    second = DataDirLock(tmp_path)
    second.release()


def test_default_backend_matches_the_platform() -> None:
    if os.name == "nt":
        assert default_backend() == "msvcrt"
    else:
        assert default_backend() == "fcntl"


def test_fcntl_backend_refuses_a_second_writer_on_any_platform(tmp_path: Path, monkeypatch) -> None:
    locks: set[tuple[int, int]] = set()
    fake = types.ModuleType("fcntl")
    fake.LOCK_EX = 2
    fake.LOCK_NB = 4
    fake.LOCK_UN = 8

    def flock(fd: int, op: int) -> None:
        st = os.fstat(fd)
        key = (st.st_dev, st.st_ino)
        if op & fake.LOCK_UN:
            locks.discard(key)
            return
        if key in locks and op & fake.LOCK_NB:
            raise BlockingIOError(11, "Resource temporarily unavailable")
        locks.add(key)

    fake.flock = flock
    monkeypatch.setitem(sys.modules, "fcntl", fake)
    first = DataDirLock(tmp_path, backend="fcntl")
    with pytest.raises(DataDirBusy):
        DataDirLock(tmp_path, backend="fcntl")
    first.release()
    second = DataDirLock(tmp_path, backend="fcntl")
    second.release()
    assert not locks


def test_msvcrt_backend_refuses_a_second_writer_on_any_platform(tmp_path: Path, monkeypatch) -> None:
    held: set[tuple[int, int, int]] = set()
    fake = types.ModuleType("msvcrt")
    fake.LK_NBLCK = 1
    fake.LK_UNLCK = 0

    def locking(fd: int, mode: int, nbytes: int) -> None:
        if nbytes != 1:
            raise OSError("lock length")
        pos = os.lseek(fd, 0, os.SEEK_CUR)
        st = os.fstat(fd)
        key = (st.st_dev, st.st_ino, pos)
        if mode == fake.LK_NBLCK:
            if key in held:
                raise PermissionError(13, "Permission denied")
            held.add(key)
            return
        if mode == fake.LK_UNLCK:
            held.discard(key)
            return
        raise OSError(f"bad mode {mode}")

    fake.locking = locking
    monkeypatch.setitem(sys.modules, "msvcrt", fake)
    first = DataDirLock(tmp_path, backend="msvcrt")
    assert (tmp_path / ".recorder.lock").stat().st_size >= 1
    with pytest.raises(DataDirBusy):
        DataDirLock(tmp_path, backend="msvcrt")
    first.release()
    second = DataDirLock(tmp_path, backend="msvcrt")
    second.release()
    assert not held


def test_lockfile_does_not_import_fcntl_at_module_scope() -> None:
    source = Path(__file__).resolve().parents[1] / "src" / "pmbot" / "data" / "lockfile.py"
    for line in source.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if line.startswith(" ") or line.startswith("\t") or stripped.startswith("#"):
            continue
        assert not stripped.startswith("import fcntl")
        assert not stripped.startswith("from fcntl")
