#!/usr/bin/env python3
"""Write review zips for the current phase. Excludes the virtualenv."""

from __future__ import annotations

import os
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = Path("/workspace/review")
SKIP_DIRS = {".venv", "__pycache__", ".pytest_cache"}
PROBE_KEEP = {
    "summary.json",
    "selected_market.json",
    "fee_rate.raw",
    "book.raw",
    "ok.raw",
    "time.raw",
}


def include(rel: str, source_only: bool) -> bool:
    parts = Path(rel).parts
    if any(part in SKIP_DIRS or part.endswith(".egg-info") for part in parts):
        return False
    if rel.endswith(".jsonl.gz") or rel.endswith(".sqlite-wal") or rel.endswith(".sqlite-shm"):
        return False
    if not source_only:
        return True
    if rel.startswith("data/probe/"):
        return Path(rel).name in PROBE_KEEP
    if rel.startswith("data/"):
        return False
    return True


def pack(path: Path, source_only: bool) -> int:
    count = 0
    with ZipFile(path, "w", ZIP_DEFLATED) as archive:
        for dirpath, dirnames, filenames in os.walk(ROOT):
            dirnames[:] = [
                name for name in dirnames if name not in SKIP_DIRS and not name.endswith(".egg-info")
            ]
            for filename in filenames:
                full = Path(dirpath) / filename
                rel = full.relative_to(ROOT).as_posix()
                if not include(rel, source_only):
                    continue
                archive.write(full, f"polymarket_bot/{rel}")
                count += 1
    return count


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    source = OUT_DIR / "polymarket_bot_phase1_source.zip"
    full = OUT_DIR / "polymarket_bot_phase1_full.zip"
    print(f"source files={pack(source, True)} bytes={source.stat().st_size}")
    print(f"full files={pack(full, False)} bytes={full.stat().st_size}")


if __name__ == "__main__":
    main()
