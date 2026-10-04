#!/usr/bin/env python3
"""Live probe: one call each to /ok, /time, /book, /fee-rate, then 60s of market WS.

A single Gamma /markets page is fetched only to choose a real token id.
Nothing here places or signs an order.

Writes raw bodies under data/probe/<UTC stamp>/ and prints a short summary.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
import websockets

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from pmbot.endpoints import CLOB_BASE, GAMMA_BASE, WS_MARKET  # noqa: E402

WS_SECONDS = 60
PING_EVERY_S = 10


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def stamp_dir() -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = ROOT / "data" / "probe" / stamp
    path.mkdir(parents=True, exist_ok=True)
    return path


def write_bytes(path: Path, body: bytes) -> None:
    path.write_bytes(body)


def parse_json_list(value: object) -> list:
    if isinstance(value, list):
        return value
    if isinstance(value, str) and value:
        try:
            loaded = json.loads(value)
        except json.JSONDecodeError:
            return []
        return loaded if isinstance(loaded, list) else []
    return []


def market_score(market: dict) -> float:
    for key in ("volumeNum", "volume24hr", "volume", "liquidityNum", "liquidity"):
        raw = market.get(key)
        if raw is None or raw == "":
            continue
        try:
            return float(raw)
        except (TypeError, ValueError):
            continue
    return 0.0


def selectable(market: dict) -> tuple[str, str] | None:
    if market.get("closed") is True or market.get("active") is False:
        return None
    if market.get("enableOrderBook") is False:
        return None
    tokens = [str(t) for t in parse_json_list(market.get("clobTokenIds")) if str(t)]
    if not tokens:
        return None
    outcomes = [str(o) for o in parse_json_list(market.get("outcomes"))]
    label = outcomes[0] if outcomes else "0"
    return tokens[0], label


async def get_raw(client: httpx.AsyncClient, url: str, params: dict | None = None) -> tuple[int, bytes, str]:
    response = await client.get(url, params=params)
    ctype = response.headers.get("content-type", "")
    return response.status_code, response.content, ctype


async def listen(token_ids: list[str], out_path: Path, seconds: int) -> dict:
    counts: dict[str, int] = {}
    samples: dict[str, object] = {}
    n = 0
    pongs = 0
    started = time.monotonic()
    async with websockets.connect(
        WS_MARKET,
        ping_interval=None,
        max_size=16 * 1024 * 1024,
        open_timeout=20,
    ) as ws:
        sub = {
            "assets_ids": token_ids,
            "type": "market",
            "custom_feature_enabled": True,
        }
        await ws.send(json.dumps(sub))

        async def heartbeat() -> None:
            while True:
                await asyncio.sleep(PING_EVERY_S)
                await ws.send("PING")

        pinger = asyncio.create_task(heartbeat())
        try:
            with out_path.open("a", encoding="utf-8") as handle:
                while time.monotonic() - started < seconds:
                    timeout = seconds - (time.monotonic() - started)
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
                    except asyncio.TimeoutError:
                        break
                    if isinstance(raw, bytes):
                        raw = raw.decode("utf-8", errors="replace")
                    wall = utc_now()
                    mono = time.monotonic_ns()
                    record = {
                        "recv_wall": wall,
                        "recv_monotonic_ns": mono,
                        "raw": raw,
                    }
                    handle.write(json.dumps(record, ensure_ascii=False) + "\n")
                    n += 1
                    if raw == "PONG":
                        pongs += 1
                        counts["PONG"] = counts.get("PONG", 0) + 1
                        samples.setdefault("PONG", "PONG")
                        continue
                    try:
                        parsed = json.loads(raw)
                    except json.JSONDecodeError:
                        counts["non_json"] = counts.get("non_json", 0) + 1
                        samples.setdefault("non_json", raw[:500])
                        continue
                    events = parsed if isinstance(parsed, list) else [parsed]
                    for event in events:
                        if isinstance(event, dict):
                            kind = str(event.get("event_type") or event.get("type") or "object")
                        else:
                            kind = type(event).__name__
                        counts[kind] = counts.get(kind, 0) + 1
                        samples.setdefault(kind, event)
        finally:
            pinger.cancel()
            try:
                await pinger
            except asyncio.CancelledError:
                pass
    return {"messages": n, "pongs": pongs, "counts": counts, "samples": samples}


async def main() -> int:
    out = stamp_dir()
    timeout = httpx.Timeout(30.0)
    summary: dict = {
        "started_wall": utc_now(),
        "docs": {
            "gamma": GAMMA_BASE,
            "clob": CLOB_BASE,
            "ws": WS_MARKET,
            "ws_assets_max_items": None,
            "ws_assets_note": (
                "SubscriptionRequest.assets_ids in "
                "https://docs.polymarket.com/asyncapi.json has no maxItems. "
                "No numeric per-connection cap is published."
            ),
        },
    }
    async with httpx.AsyncClient(timeout=timeout, headers={"User-Agent": "pmbot-probe/0.1"}) as client:
        status, body, ctype = await get_raw(client, f"{CLOB_BASE}/ok")
        write_bytes(out / "ok.raw", body)
        summary["ok"] = {"status": status, "content_type": ctype, "body": body.decode("utf-8", errors="replace")[:500]}

        status, body, ctype = await get_raw(client, f"{CLOB_BASE}/time")
        write_bytes(out / "time.raw", body)
        summary["time"] = {"status": status, "content_type": ctype, "body": body.decode("utf-8", errors="replace")[:500]}

        status, body, ctype = await get_raw(
            client,
            f"{GAMMA_BASE}/markets",
            {"closed": "false", "limit": "50"},
        )
        write_bytes(out / "gamma_markets.raw", body)
        summary["gamma_markets"] = {"status": status, "content_type": ctype, "bytes": len(body)}
        markets = json.loads(body)
        if not isinstance(markets, list):
            raise SystemExit(f"Gamma /markets did not return a list: {type(markets).__name__}")

        chosen = None
        chosen_token = None
        chosen_outcome = None
        ranked = sorted(markets, key=market_score, reverse=True)
        for market in ranked:
            picked = selectable(market)
            if picked is None:
                continue
            chosen = market
            chosen_token, chosen_outcome = picked
            break
        if chosen is None or chosen_token is None:
            raise SystemExit("No open order-book market with clobTokenIds in the Gamma page")

        (out / "selected_market.json").write_text(json.dumps(chosen, indent=2), encoding="utf-8")
        tokens = [str(t) for t in parse_json_list(chosen.get("clobTokenIds"))]
        summary["selected"] = {
            "question": chosen.get("question"),
            "conditionId": chosen.get("conditionId"),
            "slug": chosen.get("slug"),
            "token_id": chosen_token,
            "outcome": chosen_outcome,
            "token_count": len(tokens),
            "keys": sorted(chosen.keys()),
        }

        status, body, ctype = await get_raw(client, f"{CLOB_BASE}/book", {"token_id": chosen_token})
        write_bytes(out / "book.raw", body)
        book_preview: dict = {"status": status, "content_type": ctype}
        try:
            book = json.loads(body)
            book_preview["keys"] = sorted(book.keys()) if isinstance(book, dict) else type(book).__name__
            if isinstance(book, dict):
                bids = book.get("bids") or []
                asks = book.get("asks") or []
                book_preview["bid_levels"] = len(bids)
                book_preview["ask_levels"] = len(asks)
                book_preview["best_bid"] = bids[0] if bids else None
                book_preview["best_ask"] = asks[0] if asks else None
                book_preview["tick_size"] = book.get("tick_size")
                book_preview["min_order_size"] = book.get("min_order_size")
                book_preview["neg_risk"] = book.get("neg_risk")
                book_preview["hash"] = book.get("hash")
                book_preview["timestamp"] = book.get("timestamp")
        except json.JSONDecodeError:
            book_preview["body"] = body.decode("utf-8", errors="replace")[:500]
        summary["book"] = book_preview

        status, body, ctype = await get_raw(client, f"{CLOB_BASE}/fee-rate", {"token_id": chosen_token})
        write_bytes(out / "fee_rate.raw", body)
        fee_preview: dict = {
            "status": status,
            "content_type": ctype,
            "body": body.decode("utf-8", errors="replace")[:500],
        }
        summary["fee_rate"] = fee_preview

    ws_path = out / "ws.jsonl"
    ws_summary = await listen(tokens[:2] or [chosen_token], ws_path, WS_SECONDS)
    # Samples can be large; keep one trimmed copy for the summary only.
    trimmed_samples = {}
    for kind, sample in ws_summary["samples"].items():
        encoded = json.dumps(sample, ensure_ascii=False) if not isinstance(sample, str) else sample
        trimmed_samples[kind] = encoded[:800]
    summary["websocket"] = {
        "seconds": WS_SECONDS,
        "assets_ids": tokens[:2] or [chosen_token],
        "messages": ws_summary["messages"],
        "pongs": ws_summary["pongs"],
        "counts": ws_summary["counts"],
        "sample_prefix": trimmed_samples,
    }
    summary["finished_wall"] = utc_now()
    summary["output_dir"] = str(out)
    (out / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
