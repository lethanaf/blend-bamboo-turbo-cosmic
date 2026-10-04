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

- `GET /book` with `reason=periodic` every `reconcile_interval_s` (300) for every subscribed token that is not ended. A 404 is stored as a `rest_book` record with `error`, `status`, and `body`, and is not inserted into `snapshots`. After `book_404_end_after` consecutive 404s the token is not polled again.
- JSONL is buffered: one gzip member per 1s or 500 records, and on stop. `read_jsonl_gz` streams concatenated members and stops on a truncated tail.
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

`pytest`: 34 passed.

## Before the 2a replay

`.gitignore` used `data/`, which also ignored `src/pmbot/data/`. It is now `/data/`, so only the data directory at the package root is ignored. `src/pmbot/data/*.py` is tracked.

`read_jsonl_gz` streams with `gzip.open` and stops on `EOFError` / `zlib.error` / `BadGzipFile`. It no longer copies the unread tail on every member. A 3,600-member file (8,805,600 bytes, 2,446-byte members, incompressible payload) read in **0.168s**. The old member walker took **2.469s** on the same file (14.7×).

A second `record.py` on the same data directory exits 3. The lock is `data_dir/.recorder.lock` held with `flock`, so it drops if the process dies.

`GET /book` 404s log the response body (`status` and `body` are also stored on the `rest_book` record). After `book_404_end_after` consecutive 404s (3) the token is `token_ended`, dropped from polling, and unsubscribed (`operation: unsubscribe`, [dynamic subscription](https://docs.polymarket.com/market-data/websocket/overview)). Every `gamma_refresh_interval_s` (1800) Gamma is read again and newly recordable markets are subscribed (`operation: subscribe`) until `max_markets` live markets. A market's tokens stay on one socket.

## 2a book replay

`scripts/replay_book.py` rebuilds each token from tape order. `price_change` size is absolute; size 0 deletes the level. No fill simulator.

A periodic `rest_book` is scored two ways. **Unaligned** is the local book after every earlier tape event. **Aligned** applies, still in tape order, only events whose server timestamp is `<=` the snapshot's own timestamp. That includes a later tape row with an earlier exchange time, and skips an earlier row with a later exchange time. Prices and sizes compare as `Decimal`. Empty WS quotes use `best_bid "0"` / `best_ask "1"` only when that side has no level. A real level at `"0"` or `"1"` is a price match, not the sentinel.

Soak replay (`data/soak/books`, end-after 3). 722 duplicate WS frames in a 5s window were dropped (the stray writer). 50 extra bootstrap books were ignored. `price_change` frames with more than one change for the same token: **0**.

| Check | Checked | Mismatches | Rate |
|---|---:|---:|---:|
| Level, aligned | 1,090 | 8 | 0.734% |
| Level, unaligned | 1,090 | 45 | 4.13% |
| Touch vs REST top, aligned and unaligned | 1,090 | 0 | 0 |
| Level, quiet (±1s) | 0 | — | no such snapshot |
| WS quote, tape order | 1,766,236 | 31,268 | 1.77% |
| WS quote, quiet (±1s, ignoring that event) | 11,388 | 0 | 0 |

Every one of the 1,090 periodic books had some WS event within ±1s of its exchange timestamp, so the quiet subset is empty for levels. It is not empty for quotes.

All 1,090 level checks are `live`. A 404 has no payload, so an ended token never enters the level rate. WS quote checks after the end index: 0.

Gamma status of the same 1,090, queried 2026-10-04:

| | Aligned level | Unaligned level | WS quote |
|---|---:|---:|---:|
| open | 7 / 598 (1.17%) | 21 / 598 (3.51%) | 292 / 96,878 (0.301%) |
| resolved | 1 / 492 (0.20%) | 24 / 492 (4.88%) | 30,976 / 1,669,358 (1.86%) |

The open-market quote rate (0.301%) is the same kind of number as the earlier 24 / 9,330 (0.257%). The extra quote errors sit on markets Gamma now calls resolved. The 8 aligned level misses are one snapshot each, and the touch still matched: Fed (both outcomes), Iran (both), Lula (both), Putin Yes, and one GamerLegion Dota token.

Crossed books (`best bid >= best ask`) after an apply: **982**, on 22 tokens (18 now resolved, 4 not). At periodic snapshot time, aligned and unaligned: **0**.

The first 20 WS quote mismatches are all in the first three seconds, all on markets that are now resolved, and all one tick off. Several are a `best_bid_ask` sitting next to a same-timestamp `book` or `price_change` (including a size-0 delete). None of them is a multi-entry `price_change` for one token. The dump is `data/replay/soak_report_mismatches.json`.

## The 20 tokens that 404'd

Gamma on 2026-10-04, `closed=true` (the default `/markets?slug=` list hides them): all 10 markets are `closed=true`, `acceptingOrders=false`, `umaResolutionStatus=resolved`. Both tokens of each market. The recorder did not tag them during the soak; that rule was not in the process that wrote the tape. Replaying with `end_after=3` marks exactly these 20, and no others.

| Periodic 404s | Market |
|---:|---|
| 23, 23 | Valorant: Karmine Corp vs Nongshim RedForce |
| 20, 20 | LoL: GAM Esports vs Team WE |
| 20, 20 | Dota 2: LGD vs GamerLegion, game 1 |
| 20, 20 | CS2: Spirit vs ShindeN, map 2 |
| 14, 14 | Adana: Andreeva vs Boisson |
| 13, 13 | China Open: Rybakina vs Charaeva |
| 11, 11 | Dota 2: PARIVISION vs Aurora, game 2 |
| 10, 10 | CS2: Spirit vs ShindeN (series) |
| 9, 9 | LoL: HANJIN BRION vs JD Gaming |
| 5, 5 | Will SD Eibar win on 2026-10-03? |

## Fee rules, read before any fill simulator

What was known before the fill function existed, from [Fees](https://docs.polymarket.com/trading/fees), not from `base_fee`:

```
fee = C × feeRate × p × (1 - p)
```

Makers are not charged. Only takers pay. The published formula has no exponent; every `feeSchedule.exponent` stored in the catalog was 1, which matches that formula. `base_fee` 1000 is not `feeRate`.

The page's fee-precision paragraph: "Fees are rounded to 5 decimal places. The smallest fee charged is 0.00001 USDC. Anything smaller rounds to zero, so very small trades near the extremes may incur no fee at all." It does not say half-up versus half-even.

[Maker rebates](https://docs.polymarket.com/market-makers/maker-rebates) uses the same formula and the same 5-decimal / 0.00001 minimum, but its sports taker rate is 0.03 while the fees page lists sports at 0.05. The catalog had both `sports_fees_v3` 0.05 and `sports_v2` 0.03. A fill simulator has to use that market's `feeSchedule`, not the category table. Geopolitics on the fees page is rate 0, which matches the two `fees_enabled=0` markets already in the catalog.

## 2a follow-up, before any fill

Same soak tape, same duplicate rule, same end-after 3. Scores did not move: aligned levels 8 / 1,090, unaligned levels 45 / 1,090, quotes 31,268 / 1,766,236, quiet quotes 0 / 11,388, crossed-after-apply 982, touch-at-snapshot 0. Gamma-resolved quotes are 30,976 / 1,669,358.

### Resolved-market quote mismatches

Population: the 30,976 quote mismatches on tokens Gamma called resolved on 2026-10-04. All 30,976 have a recv time. The dumped first 20 are all inside 3.1s of session start (18 in the first 3 seconds, two at 3.098s). That startup transient is not the distribution below.

Seconds since the first `session_start` (`2026-10-03T14:28:07.893967+00:00`, `is_reconnect=false`):

| Age | Mismatches |
|---|---:|
| 0–3s | 18 |
| 3–30s | 158 |
| 30–60s | 456 |
| 1–5 min | 4,234 |
| 5–30 min | 18,486 |
| 30–60 min | 1,284 |
| 60–120 min | 6,340 |

Nearest-rank p50 is 974s, p90 is 6,447s, max is 7,192s. They run through the soak. They do not sit at startup.

Seconds before the ended index (recv time of the `rest_book` that tripped the third consecutive 404). 9,106 mismatches have no ended index. The other 21,870:

| Before the 404 | Mismatches |
|---|---:|
| 0–5 min | 0 |
| 5–10 min | 174 |
| 10–30 min | 1,794 |
| 30–60 min | 11,310 |
| 60–120 min | 8,592 |

Minimum 572s, median 3,351s, maximum 4,785s. None are after the ended index. **They do not cluster in the final minutes before the book ends.** The closest is nine and a half minutes out.

Seconds before the first `market_resolved` frame that names the token. The tape has 11 such frames and 20 tokens, and those 20 tokens are exactly the 20 that 404'd. The same 9,106 mismatches have no `market_resolved` on the tape (24 of the 44 Gamma-resolved tokens never got that frame during the soak). The other 21,870:

| Before `market_resolved` | Mismatches |
|---|---:|
| 0–30s | 752 |
| 30–60s | 1,216 |
| 1–30 min | 0 |
| 30–60 min | 17,758 |
| 60–120 min | 2,144 |

Minimum 0.3ms, median 2,664s. So 1,968 / 30,976 (6.4%) sit in the final minute before `market_resolved`, and then there is a hole until 30 minutes. That pocket is real and small. It is not "the 31k are the final window," and it is not the final window before the book 404.

### The 8 aligned level misses

Rule: a **tie** is a miss whose last applied event has server timestamp equal to the snapshot, or within 5ms before it (`0 <= snapshot_ts - last_ts <= 5`). Anything else is a **divergence**. Prices still compare as `Decimal`.

All 8 are ties. All 8 have `delta_ms = 0` (the last applied server timestamp equals the snapshot timestamp). Seven have one event inside that 5ms; the GamerLegion token has five. There is no divergence.

Level counts on those 8, summed across both sides: **extra 0, missing 0, size_diff 12**. The book has the same prices as the REST snapshot and different sizes. The touch still matched (aligned top mismatches stay 0).

| Market | Gamma | Side that differs | Size diffs |
|---|---|---|---:|
| Lula Yes | open | ask | 3 |
| Lula No | open | bid | 3 |
| Fed unchanged Yes | open | ask | 1 |
| Fed unchanged No | open | bid | 1 |
| Iran No | open | bid | 1 |
| Iran Yes | open | ask | 1 |
| Putin Yes | open | ask | 1 |
| GamerLegion (series) | resolved | ask | 1 |

### Duplicate frames

A websocket record is dropped when `hash(raw)` equals a frame already kept whose `recv_monotonic_ns` is between 0 and 5,000,000,000 ns earlier, inclusive. `hash` is Python's per-process string hash, so this is identical payload text inside one replay process, not a stable digest. After the map passes 200,000 entries it is pruned back to that same 5 second window. The soak dropped **722** under this rule. A second bootstrap `rest_book` for a token is a different counter (`ignored_extra_bootstrap`, 50) and is not one of the 722.

### Replay speed

One full soak replay, same machine, same checks (including the aligned rebuild of all 1,090 periodic books).

| Book | Time |
|---|---:|
| Before: price strings, scan the side to find an equal `Decimal` and to recompute best bid/ask | 501.2s |
| After: levels keyed by normalized `Decimal`; best bid/ask updated incrementally, rescanned only when that price is deleted | 100.1s |

5.0×. The accepted mismatch counts above are from the after run and match the before run.

## 2b taker fill

`pmbot.data.fill.taker_fill` walks a book. It does not submit orders and it does not change `live_trading`. There is no strategy.

Refused, and not a fill: crossed local book (`best bid >= best ask`), unanchored token, token frozen by a gap (cleared until the next anchor), ended token (the recv time of the record that tripped `end_after` 404s is `<=` the evaluation time). A walk that takes some but not all of the requested size is `partial`. The unfilled remainder is not a fill. An empty opposite side is `unfilled`, not a fill. Slippage is vwap minus touch on a buy, and touch minus vwap on a sell.

The book is the tape-order book of events with `recv_wall <= decision recv time + latency` (`BookReplay.book_asof`). Latency is the caller's number.

Fee, from the market's `feeSchedule`, not from `base_fee` and not from the category table:

```
fee = C × rate × p × (1 - p)
```

per walked level, then summed. `C` is the shares taken at that level and `p` is that level's price. Rounded to 5 decimal places, and a rounded result below 0.00001 is 0. `half-up` and `half-even` are both implemented; the Fees page does not say which. `rebateRate` is not credited. `fees_enabled` false (or 0) pays 0. A rate of 0 pays 0. `exponent != 1` raises `FeeError`, because [Market Details](https://docs.polymarket.com/market-data/market-details) says the exponent is applied to the price component and the published formula is only that curve at exponent 1. Every schedule in the soak catalog had exponent 1 (33 markets). The two `fees_enabled=0` markets have no schedule.

Half-up versus half-even, same formula: 100 shares at 0.50 with rate 0.07 is 1.75000 either way (the crypto row of the fee table). They differ on an exact half at the 6th decimal: raw 0.000005 is 0.00001 half-up and 0 half-even; raw 0.000025 is 0.00003 half-up and 0.00002 half-even; raw 0.000015 is 0.00002 either way.

Asset, read 2026-10-04. [Fees](https://docs.polymarket.com/trading/fees) says taker fees are calculated in **USDC** and does not name a different asset for buys and for sells. [Maker rebates](https://docs.polymarket.com/programs/maker-rebates) calls that same amount **pUSD** and pays rebates in pUSD, again with no buy/sell split. The older "collected in shares on buys, USDC on sells" sentence is not on either page (the old learn URLs redirect here). This fill charges the formula in USDC on both buys and sells and does not reduce the share count on a buy. The rebates page's sports taker rate is now 0.05, matching the fees page. The catalog still has one `sports_fees_v2` market at 0.03 and 21 `sports_fees_v3` at 0.05. A fill uses that market's `feeSchedule`.

The final-window behaviour above is unmodeled. `TakerFill.lines()` prints this beside every number: of 30976 resolved-market quote mismatches, 18 are in the first 3s; 9106 have no ended index and no `market_resolved` on the tape; of 21870 with an ended index, none are in the final 5 min (minimum 572s before the 404, median 3351s); of those 21870, 1968 are in the final 60s before `market_resolved` and the rest are at least 30 min earlier.
