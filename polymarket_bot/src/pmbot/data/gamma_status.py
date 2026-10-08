"""Build ``data_dir/gamma_status.json`` from Gamma for catalog tokens.

``GET /markets`` defaults to ``closed=false`` and hides resolved markets. A
resolved market returns no rows unless ``closed=true``. An open market returns
no rows when ``closed=true``. Each condition-id chunk is queried twice, open
first and closed second, so a closed row overwrites. Tokens Gamma does not
return are left out of the file. They stay unlabeled. This does not scan the
tape and does not invent an ``unknown`` status.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import httpx

from pmbot.data.models import parse_json_list
from pmbot.endpoints import GAMMA_BASE
from pmbot.httputil import client, get_text


def gamma_status_of(market: dict) -> str:
    """``resolved`` when the market is closed or UMA says resolved, else ``open``."""
    uma = market.get("umaResolutionStatus")
    uma_text = uma.lower() if isinstance(uma, str) else ""
    if market.get("closed") is True or uma_text == "resolved":
        return "resolved"
    return "open"


def rows_from_market(market: dict) -> dict[str, dict]:
    """One file row per CLOB token. Empty when the market has no token ids."""
    if not isinstance(market, dict):
        return {}
    token_ids = [str(item) for item in parse_json_list(market.get("clobTokenIds")) if str(item)]
    if not token_ids:
        return {}
    outcomes = [str(item) for item in parse_json_list(market.get("outcomes"))]
    status = gamma_status_of(market)
    question = str(market.get("question") or "")
    slug = str(market.get("slug") or "")
    rows: dict[str, dict] = {}
    for index, token_id in enumerate(token_ids):
        rows[token_id] = {
            "gamma_status": status,
            "question": question,
            "outcome": outcomes[index] if index < len(outcomes) else "",
            "slug": slug,
        }
    return rows


def _condition_ids(condition_ids: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for raw in condition_ids:
        condition_id = str(raw or "").strip()
        if not condition_id or condition_id in seen:
            continue
        seen.add(condition_id)
        ordered.append(condition_id)
    return ordered


async def fetch_gamma_status(
    http: httpx.AsyncClient,
    condition_ids: list[str],
    *,
    attempts: int = 3,
    batch_size: int = 40,
) -> dict[str, dict]:
    """Token id to ``{gamma_status, question, outcome, slug}``.

    ``condition_ids`` is repeated on the query string. ``limit`` is at least
    the chunk length so a default page size cannot drop the tail of the batch.
    """
    ids = _condition_ids(condition_ids)
    if not ids:
        return {}
    size = batch_size if batch_size > 0 else 1
    out: dict[str, dict] = {}
    for start in range(0, len(ids), size):
        chunk = ids[start : start + size]
        # Open first. A later closed row for the same token overwrites.
        for closed in ("false", "true"):
            params: list[tuple[str, str]] = [("condition_ids", condition_id) for condition_id in chunk]
            params.append(("closed", closed))
            params.append(("limit", str(len(chunk))))
            response = await get_text(http, "/markets", params=params, attempts=attempts)
            batch = response.json()
            if not isinstance(batch, list):
                raise RuntimeError(f"Gamma /markets returned {type(batch).__name__}, expected a list")
            for item in batch:
                out.update(rows_from_market(item))
    return out


def catalog_condition_ids(catalog: Path) -> list[str]:
    con = sqlite3.connect(catalog)
    try:
        tables = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "markets" not in tables:
            return []
        return [
            str(row[0])
            for row in con.execute("SELECT condition_id FROM markets ORDER BY condition_id")
            if row[0]
        ]
    finally:
        con.close()


def write_gamma_status(path: Path, rows: dict[str, dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    ordered = {token: rows[token] for token in sorted(rows)}
    path.write_text(json.dumps(ordered, indent=2, sort_keys=True) + "\n", encoding="utf-8")


async def fetch_and_write(
    data_dir: Path,
    *,
    attempts: int = 3,
    batch_size: int = 40,
    timeout_s: float = 30.0,
) -> dict[str, dict]:
    """Read condition ids from ``catalog.sqlite`` and write ``gamma_status.json``."""
    catalog = data_dir / "catalog.sqlite"
    if not catalog.is_file():
        raise FileNotFoundError(f"no catalog at {catalog}")
    ids = catalog_condition_ids(catalog)
    async with client(GAMMA_BASE, timeout_s) as http:
        rows = await fetch_gamma_status(http, ids, attempts=attempts, batch_size=batch_size)
    write_gamma_status(data_dir / "gamma_status.json", rows)
    return rows
