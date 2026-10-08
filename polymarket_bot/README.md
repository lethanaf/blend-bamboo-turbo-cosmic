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

The book is the tape-order book of events with `recv_wall <= decision recv time + latency` (`BookReplay.book_asof`). Latency is the caller's number. `same_ms="both"` also returns the other same-server-timestamp reading, described below.

Fee, from the market's `feeSchedule`, not from `base_fee` and not from the category table:

```
fee = C × rate × p × (1 - p)
```

per walked level, then summed. `C` is the shares taken at that level and `p` is that level's price. One visible level is one match. Rounded to 5 decimal places, and a rounded result below 0.00001 is 0. `half-up` and `half-even` are both implemented; the Fees page does not say which. `fee_per_match_vs_per_order` sums the raw level fees and rounds once. That number is a sensitivity, not the fee. The Fees page applies the fee at match time, and this tape cannot see the maker orders inside a level. `rebateRate` is not credited. `fees_enabled` false (or 0) pays 0. A rate of 0 pays 0. `exponent != 1` raises `FeeError`, because [Market Details](https://docs.polymarket.com/market-data/market-details) says the exponent is applied to the price component and the published formula is only that curve at exponent 1. Every schedule in the soak catalog had exponent 1 (33 markets). The two `fees_enabled=0` markets have no schedule.

Half-up versus half-even, same formula: 100 shares at 0.50 with rate 0.07 is 1.75000 either way (the crypto row of the fee table). They differ on an exact half at the 6th decimal: raw 0.000005 is 0.00001 half-up and 0 half-even; raw 0.000025 is 0.00003 half-up and 0.00002 half-even; raw 0.000015 is 0.00002 either way.

Asset, read 2026-10-04. [Fees](https://docs.polymarket.com/trading/fees) says taker fees are calculated in **USDC** and does not name a different asset for buys and for sells. [Maker rebates](https://docs.polymarket.com/programs/maker-rebates) calls that same amount **pUSD** and pays rebates in pUSD, again with no buy/sell split. The older "collected in shares on buys, USDC on sells" sentence is not on either page (the old learn URLs redirect here). This fill charges the formula in USDC on both buys and sells and does not reduce the share count on a buy. The rebates page's sports taker rate is now 0.05, matching the fees page. The catalog still has one `sports_fees_v2` market at 0.03 and 21 `sports_fees_v3` at 0.05. A fill uses that market's `feeSchedule`.

`TakerFill.lines()` prints one caveat beside every number. With no report loaded, the text is `unmodeled: quote mismatches are not adjusted for in this number`. `caveat_from_report` reads `quote_tape_order` from a replay JSON and prints the counts from that file. It does not hard-code a final-window sentence. The histogram above is not a final-window effect.

## Quote mismatches are same-server-timestamp ties

A quote mismatch is a **tie** when another WS event for the same token has the same server timestamp, earlier or later on the tape. The mismatch's own event counts as one. A missing timestamp is not a tie. This is not the 5ms aligned-level rule above.

| Population | Mismatches | Tie | Not a tie | Tie fraction |
|---|---:|---:|---:|---:|
| Gamma resolved | 30,976 | 28,382 | 2,594 | 91.6% |
| Gamma open | 292 | 248 | 44 | 84.9% |

Ties dominate both populations.

Mismatches per 1,000 WS events. A WS event is one `note_ws`: a `book`, each `price_change`, a `best_bid_ask`, a `last_trade_price`, or a `tick_size_change`. Slug prefixes: esports in play `cs2-` `dota2-` `lol-` `val-`; sports in play `fif-` `unl-` `es2-` `wta-`; everything else is other, including long-dated sports and bitcoin.

| Bucket | WS events | Mismatches | Per 1,000 |
|---|---:|---:|---:|
| Esports in play | 1,254,159 | 16,452 | 13.12 |
| Sports in play | 435,567 | 14,524 | 33.35 |
| In play (those two) | 1,689,726 | 30,976 | 18.33 |
| Other | 119,697 | 292 | 2.44 |

On this soak every resolved-market mismatch sits on an in-play slug, and every open-market mismatch sits on an other slug. That is the mismatch split, not a claim that every other market is still open (bitcoin-above-74k is resolved and contributed none of the 30,976).

Because ties dominate, `book_asof(..., same_ms="both")` returns two books. `tape` applies the group in recv order. `defer` is the book from before that server timestamp, and only while the query lands on the group. Once the query is strictly later than the last mutation of that timestamp, defer collapses to tape. It is not a permutation of repeated writes to the same price. A fill reports the range between those two readings. The lock-in scan below had no positive net on either reading.

Quote checks on this pass were unchanged: 31,268 / 1,766,236. The caveat loaded from that report is `unmodeled: 31268 quote mismatches out of 1766236 checks are not adjusted for in this number`.

## Forward cursor

`book_asof` keeps one cursor per token. It applies each mutation once and never rescans. A query that goes backward raises.

`recv_wall` is not monotonic inside a session. `RecvClock` counted **64** backward `recv_wall` steps and **64** backward `recv_monotonic_ns` steps, on 3 sessions, all on `connection_id` `0`. Every backward wall value falls between `2026-10-03T15:22:30.444829+00:00` and `2026-10-03T15:23:04.586075+00:00`, which is the stray second writer. That writer reused connection id 0, so the monotonic clock is a different process epoch and steps backward on the same records. It is not a usable fallback.

The cursor stays on `recv_wall`. A stamp earlier than the previous one is clamped to that previous stamp and the event is still applied once, in tape order. The high-water clamp fired on **534** mutations. The ingest counter `token_recv_wall_backward` is **170**: it adopts the earlier wall as the new last, so it counts step-downs, not every mutation under the high-water mark.

10,000 forward queries, cold cursors, sampled in time order across the soak muts: **8.736s** (874 µs each). That includes each sampled token's one-time `recv_wall` parse. The full binary scan below then walked every event in about 50s.

## Lock-in scan

`scripts/scan_lockin.py` does not send orders and does not change `live_trading`. It buys both tokens of one condition at the ask. Net per share is `1 - (ask_yes + ask_no)` minus taker fees on both legs, from that market's `feeSchedule`, one match per leg. Touch size is the min of the two ask sizes. The catalog minimum is 5 shares on all 35 markets. Below that is not executable. One shot per window, not a sum of every tick. Windows can overlap across markets, so a sum would not be a portfolio. There were no windows.

Gamma is the payoff source. A complete set pays 1 USDC per share. 44 tokens are Gamma-resolved; 20 of them have a `market_resolved` frame on the tape and **24 do not**. Both tokens resolved: that 1 is already determined. Both open: it is paid at resolution. Mixed pairs: none in this catalog.

Latency is the arrival book at the decision time plus 0, 100, 250, or 500 ms, through `book_asof`. A latency-0 window is a maximal interval where the after-fee net is positive. It survives a latency only if the arrival book at that later time is still net-positive.

| | Result |
|---|---|
| Binary markets scanned | 35 |
| Decision times | 886,490 |
| Tightest ask sum, either size | 1.001 |
| Both asks, size ≥ 5, sum ≥ 1 | 856,177 |
| Size below 5 | 4,502, and none of those sums were under 1 |
| Fee-killed intervals (gross > 0, net ≤ 0) | 0 |
| Net-positive windows | 0 |
| Still positive at 100 / 250 / 500 ms | 0 / 0 / 0 |
| Return per day | no shot |

Refused and not counted as edge: leg A unanchored 5, leg B unanchored 30, leg A gap-frozen 32, leg B gap-frozen 3, leg A crossed 491, leg A no ask 14,542, leg B no ask 10,708.

**No executable touch had `ask_yes + ask_no < 1`.** Fees never had a gross edge to take. Latency has nothing to keep. There is no duration distribution, no touch size, and no return per day.

Neg-risk multi-outcome sets were not summed. A sum of outcome asks pays 1 only if the catalog holds every outcome. The stored event payload has no sibling market list and no outcome count, so every group is incomplete. Nine groups: Brazil 3, balance of power 2, Finland/Albania 2, and one each for the Fed, Shakhtar, England, Eibar, Spain, and the House. Yes+No of one of those conditions is already in the binary scan. The group sum is not.

Not modeled, and not in the numbers above: gas, queue position, rejects, one leg filling and the other missing, walking past the touch, a true per-match fee versus one fee per visible price level, voids (the gamma file has no void flag), rebates. The per-order rounding sensitivity had no shot to move.

`unmodeled: 31268 quote mismatches out of 1766236 checks are not adjusted for in this number`

The dump is `data/replay/lockin_scan.json`. It does not replace `data/replay/soak_report.json`.

## The 25,811 other decision times

886,490 binary buy decisions. 856,177 had both asks, size at least 5, and `ask_yes + ask_no >= 1`. 4,502 were below the 5-share minimum, and none of those sums were under 1. The other **25,811** are neither. First failing leg only, A before B. Inside a leg the order is ended, then gap-frozen, then unanchored, then crossed, then a missing ask.

| Reason | Count | Of which |
|---|---:|---|
| ended | 0 | |
| gap_frozen | 35 | A 32, B 3 |
| unanchored | 35 | A 5, B 30 |
| crossed | 491 | A 491, B 0 |
| a_no_ask | 14,542 | A had no ask; A was not ended, frozen, unanchored, or crossed |
| b_no_ask | 10,708 | A was a live ask; B had no ask |
| Total | 25,811 | |

## session_id

Every new tape record carries `session_id`, one uuid per writer process (`HourlyJsonl`). Two recorders that both use `connection_id` `0` no longer share a clock. Replay uses `session_id` when the record has one, and `connection_id` when it does not. The soak tape has no `session_id`, so the 64 backward steps between 15:22:30Z and 15:23:04Z are still one connection.

## Mint and sell

The other binary lock-in is mint one pair for 1 USDC and sell both bids. Gross per share is `bid_yes + bid_no - 1`, minus taker fees on both sells. Same size rule, same latencies, same one-shot windows.

On the soak tape the highest bid sum, at any size, was **0.999**. No touch had `bid_yes + bid_no > 1`. Fee-killed intervals: 0. Windows: 0. Nothing survives 0, 100, 250, or 500 ms.

## 24h universe

`config.yaml` stays `universe: volume` (the soak selection). `config.24h.yaml` is `universe: diversified`:

- top 24h volume, round-robin across `feeType` categories
- drop a market whose `endDate` is within 6 hours, including one already past
- plus up to 4 complete neg-risk events, highest event volume first

A complete event is every sibling `GET /events` returned, each with an open book. The event is stored with `negRiskMarketID`, the sibling list, the outcome count, and a flag on augmented/placeholder outcomes (`negRiskOther` or a title of Other/Placeholder, and the event's `negRiskAugmented`). An event that does not fit in `max_markets` is skipped whole. It is not cut down to a subset. This file was not started; there is no 24h tape.

The complete-set scan (`scan_outcome_set`) buys every YES ask and, separately, mints and sells every YES bid. It runs only when the catalog row says the sibling list is complete. This soak catalog has no sibling list. Those sums were not taken. **Nothing survives.**

No orders. `live_trading` is false.

## Before the 24h run

The data-directory lock uses `fcntl` on Unix and `msvcrt` on Windows, picked at runtime, so `scripts/record.py` imports on both. Ctrl+C uses `loop.add_signal_handler` when the loop has it. When that raises `NotImplementedError`, `signal.signal` sets the same stop event. Either way the sockets write `session_stop` (`reason=shutdown`) and the tape is flushed. A `KeyboardInterrupt` that still lands in `run()` sets that stop event before the drain.

A diversified refresh applies `exclude_ending_within_s` inside `discover_markets`, before the limit. Markets inside the horizon are added to `skip` and kept on the recorder, so a page that all ends within 6 hours cannot fill the slot count and get requested again forever. `config.yaml` is still `universe: volume`.

Every 10 minutes the process logs `progress messages= markets_live= ended= gaps= mb_written=`. `mb_written` is gzip bytes this process has flushed, divided by 1024². It is not the size of older files in the directory.

`python scripts/analyze_day.py <data_dir>` replays `books/`, scores aligned levels and quote ties, scans binary buy and sell, and scans complete neg-risk sets from `neg_risk_events` (sum of YES asks, sum of YES bids, each leg's own feeSchedule, at 0/100/250/500 ms). It prints one markdown report and does not send orders. It runs on Windows and WSL. The recording was not started.

## Gamma status file and quote event split

`ties_dominate` counts every quote mismatch, labeled or not. It is `n/a` when the tape has no quote mismatch to classify, not `False`. `resolved` and `open` are that same count split by a Gamma label. The recorder does not write `gamma_status.json`. `python scripts/analyze_day.py <data_dir> --fetch-gamma` builds it for the catalog's condition ids: `GET /markets` with `closed=false`, then the same ids with `closed=true`, because the default list hides resolved markets. A token Gamma does not return stays out of the file.

Quote checks are also split into `price_change` and `best_bid_ask`, with ties inside each. A `price_change` that carries neither a best bid nor a best ask is not a quote check. No recording was started.
