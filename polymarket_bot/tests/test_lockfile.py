from pathlib import Path

import pytest

from pmbot.data.lockfile import DataDirBusy, DataDirLock


def test_second_writer_is_refused(tmp_path: Path) -> None:
    first = DataDirLock(tmp_path)
    with pytest.raises(DataDirBusy):
        DataDirLock(tmp_path)
    first.release()
    second = DataDirLock(tmp_path)
    second.release()
