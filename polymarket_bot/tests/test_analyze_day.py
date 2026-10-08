import json
import sys
from pathlib import Path

from pmbot.clock import Clock
from pmbot.data.models import parse_market
from pmbot.data.store import Store
from pmbot.data.universe import NegRiskEvent, Sibling

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))

import analyze_day  # noqa: E402


def _market(condition_id: str, slug: str, yes: str, no: str) -> object:
    parsed = parse_market(
        {
            "conditionId": condition_id,
            "id": condition_id,
            "slug": slug,
            "question": slug,
            "endDate": "2026-10-05T00:00:00Z",
            "active": True,
            "closed": False,
            "enableOrderBook": True,
            "acceptingOrders": True,
            "negRisk": True,
            "outcomes": '["Yes", "No"]',
            "clobTokenIds": json.dumps([yes, no]),
            "feesEnabled": False,
            "orderMinSize": 5,
        }
    )
    assert parsed is not None
    return parsed


def _sibling(market, *, placeholder: bool = False) -> Sibling:
    yes = next(token.token_id for token in market.tokens if token.outcome.lower() == "yes")
    return Sibling(
        condition_id=market.condition_id,
        slug=market.slug,
        question=market.question,
        group_item_title=market.question,
        yes_token_id=yes,
        placeholder=placeholder,
        augmented=placeholder,
        market=market,
    )


def _book(asset: str, bid: str, ask: str, wall: str, mono: int, *, size: str = "10", ts: str = "1000") -> dict:
    raw = json.dumps(
        {
            "event_type": "book",
            "asset_id": asset,
            "timestamp": ts,
            "bids": [{"price": bid, "size": size}],
            "asks": [{"price": ask, "size": size}],
        },
        separators=(",", ":"),
    )
    return {
        "kind": "ws",
        "connection_id": "0",
        "session_id": "analyze-test",
        "raw": raw,
        "recv_wall": wall,
        "recv_monotonic_ns": mono,
    }


def test_missing_directory_exits(tmp_path: Path) -> None:
    assert analyze_day.main([str(tmp_path / "missing")]) == 2


def test_report_includes_windows_blocked_reasons_and_complete_sets(tmp_path: Path) -> None:
    pair = _market("0xpair", "pair", "pair-yes", "pair-no")
    left = _market("0xleft", "left", "left-yes", "left-no")
    right = _market("0xright", "right", "right-yes", "right-no")
    store = Store(tmp_path / "catalog.sqlite", tmp_path / "books", clock=Clock())
    for market in (pair, left, right):
        store.upsert_market(market)
    store.upsert_neg_risk_event(
        NegRiskEvent(
            neg_risk_market_id="0xset",
            event_id="1",
            slug="set-event",
            title="Set",
            end_date="2026-10-05T00:00:00Z",
            outcome_count=2,
            augmented=True,
            complete=True,
            reason=None,
            volume_24hr=10.0,
            siblings=(_sibling(left, placeholder=True), _sibling(right)),
        )
    )
    store.upsert_neg_risk_event(
        NegRiskEvent(
            neg_risk_market_id="0xpartial",
            event_id="2",
            slug="partial-event",
            title="Partial",
            end_date="2026-10-05T00:00:00Z",
            outcome_count=3,
            augmented=False,
            complete=False,
            reason="missing",
            volume_24hr=1.0,
            siblings=(_sibling(left),),
        )
    )
    t0 = "2026-10-04T12:00:00+00:00"
    t1 = "2026-10-04T12:00:01+00:00"
    mono = 1
    rows = [
        _book("pair-yes", "0.20", "0.40", t0, mono),
        _book("pair-no", "0.20", "0.40", t0, mono + 1),
        _book("left-yes", "0.10", "0.30", t0, mono + 2),
        _book("left-no", "0.10", "0.80", t0, mono + 3),
        _book("right-yes", "0.10", "0.30", t0, mono + 4),
        _book("right-no", "0.10", "0.80", t0, mono + 5),
        _book("pair-yes", "0.20", "0.70", t1, mono + 6),
        _book("pair-no", "0.20", "0.70", t1, mono + 7),
        _book("left-yes", "0.10", "0.30", t1, mono + 8, size="11", ts="2000"),
    ]
    for row in rows:
        store.tape.write(row)
    store.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    store.close()

    report = analyze_day.build_report(tmp_path)
    assert "## Book replay" in report
    assert "level_aligned" in report
    assert "ties_dominate: n/a" in report
    assert "ties_dominate: False" not in report
    assert "- all quote mismatches 0:" in report
    assert "quote_price_change:" in report
    assert "quote_best_bid_ask:" in report
    assert "- price_change quote mismatches" in report
    assert "- best_bid_ask quote mismatches" in report
    assert "--fetch-gamma" in report
    assert "## Binary buy" in report
    assert "| pair | 1.000 | 10 | 2.00 | yes | yes | yes |" in report
    assert "## Binary sell" in report
    assert "bid_yes + bid_no > 1" in report
    assert "partial-event: incomplete, not summed" in report
    assert "scanning set-event" in report
    assert "augmented/placeholder" in report
    assert "## Complete-set buy" in report
    assert "| set-event | 1.000 censored | 10 | 4.00 | yes | yes | yes |" in report
    assert "## Complete-set sell" in report
    assert "YES-bid sum over 1" in report
    assert "## Not modeled" in report
    assert "gas" in report
    assert "queue position" in report
    assert "No orders. live_trading is false." in report
    assert analyze_day.main([str(tmp_path)]) == 0


def test_fetch_gamma_flag_writes_before_the_report(tmp_path, monkeypatch, capsys) -> None:
    market = _market("0xabc", "slug", "yes-tok", "no-tok")
    store = Store(tmp_path / "catalog.sqlite", tmp_path / "books", Clock())
    store.upsert_market(market)
    store.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    store.close()

    async def fake(data_dir: Path, **kwargs):
        from pmbot.data.gamma_status import write_gamma_status

        write_gamma_status(
            Path(data_dir) / "gamma_status.json",
            {"yes-tok": {"gamma_status": "open", "question": "Q", "outcome": "Yes", "slug": "s"}},
        )
        return {"yes-tok": {"gamma_status": "open"}}

    monkeypatch.setattr(analyze_day, "fetch_and_write", fake)
    assert analyze_day.main([str(tmp_path), "--fetch-gamma"]) == 0
    saved = json.loads((tmp_path / "gamma_status.json").read_text(encoding="utf-8"))
    assert saved["yes-tok"]["gamma_status"] == "open"
    out = capsys.readouterr().out
    assert "gamma file: `gamma_status.json`" in out
    assert "ties_dominate: n/a" in out


def test_fetch_gamma_does_not_run_without_a_catalog(tmp_path, monkeypatch) -> None:
    called = False

    async def fake(data_dir: Path, **kwargs):
        nonlocal called
        called = True
        return {}

    monkeypatch.setattr(analyze_day, "fetch_and_write", fake)
    assert analyze_day.main([str(tmp_path), "--fetch-gamma"]) == 2
    assert called is False
