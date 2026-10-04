import json
from pathlib import Path

from pmbot.data.models import parse_market, select_markets

FIXTURE = Path(__file__).parent / "fixtures" / "market.json"


def test_parses_probe_market() -> None:
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
    market = parse_market(raw)
    assert market is not None
    assert market.condition_id.startswith("0x")
    assert [token.outcome for token in market.tokens] == ["Yes", "No"]
    assert market.tokens[0].token_id != market.tokens[1].token_id
    assert market.fee_schedule is not None
    assert market.fee_schedule["rate"] == 0.04
    assert market.fee_schedule["exponent"] == 1
    assert market.fee_schedule["takerOnly"] is True
    assert market.fee_schedule["rebateRate"] == 0.25
    assert market.maker_base_fee == 1000
    assert market.taker_base_fee == 1000
    # These are different quantities. Do not treat base fee as the schedule rate.
    assert market.maker_base_fee != market.fee_schedule["rate"]
    assert market.fees_enabled is True
    assert market.fee_type == "politics_fees"
    assert market.uma_resolution_statuses == "[]"
    assert market.neg_risk is True
    assert market.order_price_min_tick_size == "0.001"


def test_skips_untradable() -> None:
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
    for key, value in (
        ("enableOrderBook", False),
        ("acceptingOrders", False),
        ("active", False),
        ("closed", True),
        ("clobTokenIds", "[]"),
    ):
        broken = dict(raw)
        broken[key] = value
        assert parse_market(broken) is None
    assert select_markets([raw, dict(raw, conditionId="dup")], 30)[0].condition_id == raw["conditionId"]
    assert len(select_markets([raw, raw], 30)) == 1
