import asyncio
import json
import sqlite3

import httpx

from pmbot.data.gamma_status import (
    catalog_condition_ids,
    fetch_gamma_status,
    gamma_status_of,
    rows_from_market,
    write_gamma_status,
)

OPEN_YES = "2213957649161627793381994368131485505647723208738124952452819345058597751695"
OPEN_NO = "44046525152074436753629616217525652949736205933417533647338274649282385796755"


def _market(condition_id: str, *, closed: bool, uma: str | None, tokens: list[str], slug: str) -> dict:
    market = {
        "conditionId": condition_id,
        "question": slug,
        "slug": slug,
        "closed": closed,
        "outcomes": '["Yes", "No"]',
        "clobTokenIds": json.dumps(tokens),
    }
    if uma is not None:
        market["umaResolutionStatus"] = uma
    return market


def test_status_is_resolved_when_closed_or_uma_says_so() -> None:
    assert gamma_status_of({"closed": False}) == "open"
    assert gamma_status_of({"closed": False, "umaResolutionStatus": "resolved"}) == "resolved"
    assert gamma_status_of({"closed": True, "umaResolutionStatus": ""}) == "resolved"
    assert gamma_status_of({"closed": True, "umaResolutionStatus": "Resolved"}) == "resolved"
    assert rows_from_market({"clobTokenIds": "[]", "closed": True}) == {}


def test_fetch_queries_open_then_closed_and_leaves_missing_tokens_out() -> None:
    calls: list[tuple[str, tuple[str, ...], str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        closed = request.url.params["closed"]
        ids = tuple(request.url.params.get_list("condition_ids"))
        limit = request.url.params["limit"]
        calls.append((closed, ids, limit))
        assert int(limit) >= len(ids)
        rows = []
        if "0xopen" in ids and closed == "false":
            rows.append(
                _market(
                    "0xopen",
                    closed=False,
                    uma=None,
                    tokens=[OPEN_YES, OPEN_NO],
                    slug="will-oprah",
                )
            )
        if "0xflip" in ids and closed == "false":
            rows.append(_market("0xflip", closed=False, uma=None, tokens=["flip-yes", "flip-no"], slug="flip"))
        if "0xflip" in ids and closed == "true":
            rows.append(
                _market(
                    "0xflip",
                    closed=True,
                    uma="resolved",
                    tokens=["flip-yes", "flip-no"],
                    slug="flip",
                )
            )
        if "0xclosed" in ids and closed == "true":
            rows.append(
                _market(
                    "0xclosed",
                    closed=True,
                    uma="resolved",
                    tokens=["closed-yes", "closed-no"],
                    slug="cfb-wash-usc",
                )
            )
        return httpx.Response(200, json=rows)

    async def run() -> dict:
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport, base_url="https://gamma.test") as http:
            return await fetch_gamma_status(
                http,
                ["0xopen", "0xclosed", "0xflip", "0xgone", "0xopen"],
            )

    rows = asyncio.run(run())
    assert calls[0][0] == "false"
    assert calls[1][0] == "true"
    assert set(calls[0][1]) == {"0xopen", "0xclosed", "0xflip", "0xgone"}
    assert rows[OPEN_YES]["gamma_status"] == "open"
    assert rows[OPEN_YES]["outcome"] == "Yes"
    assert rows[OPEN_YES]["slug"] == "will-oprah"
    assert rows["closed-yes"] == {
        "gamma_status": "resolved",
        "question": "cfb-wash-usc",
        "outcome": "Yes",
        "slug": "cfb-wash-usc",
    }
    assert rows["flip-yes"]["gamma_status"] == "resolved"
    assert "in_404_set" not in rows["closed-yes"]
    assert all("gone" not in token for token in rows)


def test_catalog_ids_and_write_skip_a_missing_table(tmp_path) -> None:
    empty = tmp_path / "empty.sqlite"
    sqlite3.connect(empty).close()
    assert catalog_condition_ids(empty) == []

    catalog = tmp_path / "catalog.sqlite"
    con = sqlite3.connect(catalog)
    con.execute("CREATE TABLE markets (condition_id TEXT)")
    con.execute("INSERT INTO markets VALUES ('0xb')")
    con.execute("INSERT INTO markets VALUES ('0xa')")
    con.execute("INSERT INTO markets VALUES ('')")
    con.commit()
    con.close()
    assert catalog_condition_ids(catalog) == ["0xa", "0xb"]

    path = tmp_path / "gamma_status.json"
    write_gamma_status(
        path,
        {"b": {"gamma_status": "open", "question": "Q", "outcome": "No", "slug": "s"}},
    )
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert list(saved) == ["b"]
    assert "unknown" not in json.dumps(saved)
