# pmbot — Phase 1

Public Polymarket market discovery and order-book recorder. It does not sign or send orders. `live_trading: true` is refused.

## What was verified live (2026-10-03)

`scripts/probe.py` called the public endpoints and saved raw bodies under `data/probe/20261003T124434Z/`.

| Call | Result |
|---|---|
| `GET https://clob.polymarket.com/ok` | `200`, body `"OK"` (text, including quotes) |
| `GET https://clob.polymarket.com/time` | `200`, body `1791031474` (unix seconds, text) |
| `GET https://gamma-api.polymarket.com/markets?closed=false&limit=50` | `200`, JSON list. `clobTokenIds`, `outcomes`, and `outcomePrices` are JSON strings |
| `GET https://clob.polymarket.com/book?token_id=...` | keys `market`, `asset_id`, `timestamp`, `hash`, `bids`, `asks`, `min_order_size`, `tick_size`, `neg_risk`, `last_trade_price` |
| `GET https://clob.polymarket.com/fee-rate?token_id=...` | `{"base_fee": 1000}` |
| `wss://ws-subscriptions-clob.polymarket.com/ws/market` for 60s | text `PING` every 10s got text `PONG` (5). Events: `book` 2, `new_market` 31 |

On that book, bids were sorted ascending and asks descending. Index 0 is not the best price. Top of book uses max bid and min ask. The docs do not specify sort order.

Gamma `feeSchedule` on the same market was `{rate: 0.04, exponent: 1, takerOnly: true, rebateRate: 0.25}` with `makerBaseFee`/`takerBaseFee` 1000. `base_fee` matches the base-fee fields, not `feeSchedule.rate`. Phase 1 stores both and does not compute a fee.

`SubscriptionRequest.assets_ids` in [asyncapi.json](https://docs.polymarket.com/asyncapi.json) has no `maxItems`. The default is one WebSocket. Set `ws_assets_per_connection` to shard; a market's tokens are not split across sockets.

## Run

Python 3.11+. From this directory:

```bash
uv venv --python 3.11 .venv
uv pip install --python .venv/bin/python -e ".[dev]"
.venv/bin/python scripts/probe.py
.venv/bin/python scripts/discover.py
.venv/bin/python scripts/record.py --seconds 120
.venv/bin/pytest
```

`discover.py` writes `data/catalog.sqlite` (WAL) and fee-rate payloads. `record.py` adds a bootstrap `GET /book` per token, then the market socket. Tape files are `data/books/YYYYMMDD/HH.jsonl.gz` (UTC). Every record has `recv_wall` and `recv_monotonic_ns`.

On a socket drop after a successful subscribe, the recorder writes a `gap` record, reconnects, resubscribes, and re-snapshots that connection's tokens with `GET /book` (`reason=reconnect`). A failed first connect does not write a gap. A clean stop does not write a gap.

## Not in this phase

No paper fills, no strategy, no live orders, no gas, no slippage model. Fee math is not applied. `custom_feature_enabled` also delivers global `new_market` / `market_resolved` events, not only the subscribed tokens. Receive time is local read time; the exchange timestamp stays inside the payload. A crash can drop the unflushed buffer (up to 1s or 500 records) and leave a truncated final gzip member, which the reader skips. Parquet is not produced.

## Tape fixes

- `GET /book` with `reason=periodic` every `reconcile_interval_s` (300) for every subscribed token. A 404 is stored as a `rest_book` record with `error` and is not inserted into `snapshots`.
- JSONL is buffered: one gzip member per 1s or 500 records, and on stop. `read_jsonl_gz` skips a truncated final member.
- Reconnect backoff returns to the initial delay after the socket has stayed up for `backoff_reset_after_s` (30).
- Each socket writes `session_start` and `session_stop`. `new_market` / `market_resolved` frames whose tokens are not subscribed are tagged `"scope": "global"`.

## Measured

`data/catalog.sqlite` from the first 30-market discovery: 60 tokens, `base_fee` 1000 on 56, `base_fee` 0 on 4. Those 4 tokens are exactly the two `fees_enabled=0` markets ("Will the U.S. invade Iran before 2027?" and "Putin out as President of Russia by December 31, 2026?"). Symmetric difference is empty.

Two-minute buffered tape (`data/ratio2m`, 14,474 records, 122 gzip members): 1,613,713 gzip bytes / 12,739,361 uncompressed = **7.89×**. The old per-line tape was 1.86× (2,404,240 / 4,474,009, 4,992 members). That run wrote `session_start` and `session_stop` (`reason=shutdown`), 18 `scope=global` frames, 60 bootstrap `rest_book`s, 0 gaps. `periodic_rounds=0` because 120s < 300s. Clock skew against `GET /time` was 0.

Two-hour soak (`config.soak.yaml`, `--seconds 7200`, 2026-10-03 14:28:05Z to 16:28:10Z, one socket, 60 tokens):

| Item | Result |
|---|---|
| Forced drop | 14:38:58Z, TCP `shutdown(SHUT_RDWR)` on the market-socket fd to `ws-subscriptions-clob.polymarket.com` |
| Gap | 1 row. `recv_wall=2026-10-03T14:38:58.811060+00:00`, reason `ConnectionClosedError: no close frame received or sent`, 60 tokens, file `books/20261003/14.jsonl.gz` |
| Re-snapshot | `session_start` `is_reconnect=true` at 14:39:00.116Z (1.3s later). 60 `rest_book` records with `reason=reconnect`: 58 books stored, 2 tokens `404` |
| Periodic | `periodic_rounds=23` (every 300s, first 14:33:07Z, last 16:23:42Z). Tape: 1,380 `rest_book` records (`23×60`). SQLite `snapshots` `reason=periodic`: **1,090**. The other 290 are `rest_book` error records (20 distinct tokens returned 404 at least once; the same 2 tokens 404'd on bootstrap, reconnect, and all 23 rounds) |
| Stop | `session_stop` at 16:28:10Z, `reason=cancelled` (socket task still closing when the 5s drain expired). No second gap. In-process counters: `messages=954972 snapshots=1208 periodic_snapshots=1090 gaps=1` |
| Tape | 101,933,809 gzip / 840,205,335 raw = **8.24×** across hours 14, 15, and 16. Every hour parsed with leftover 0 |

A second recorder was started by mistake at 15:22:30Z into the same `data/soak` directory and killed within a few seconds. It added one extra `session_start` (`is_reconnect=false`), 60 bootstrap `rest_book`s, about 3.6k websocket frames, and 5 markets / 10 tokens in the soak catalog (35 markets / 70 tokens). It did not write a gap or a periodic round. The gap, the reconnect snapshots, and the 23 periodic rounds belong to the original process. Do not treat the soak catalog's market count as 30.

`pytest`: 25 passed. Some high-volume tokens 404 on `GET /book` for the whole run; the recorder logs that and keeps going.