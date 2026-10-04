from pmbot.data.scope import lifecycle_scope

SUB = {"token-yes", "token-no"}


def test_unsubscribed_new_market_is_global() -> None:
    raw = '{"event_type":"new_market","assets_ids":["other"],"condition_id":"0x1"}'
    assert lifecycle_scope(raw, SUB) == "global"


def test_subscribed_lifecycle_is_not_tagged() -> None:
    raw = '{"event_type":"market_resolved","winning_asset_id":"token-yes","assets_ids":["token-yes","token-no"]}'
    assert lifecycle_scope(raw, SUB) is None


def test_price_change_is_not_tagged() -> None:
    raw = '{"event_type":"price_change","price_changes":[{"asset_id":"other","price":"0.5","size":"0","side":"BUY"}]}'
    assert lifecycle_scope(raw, SUB) is None


def test_string_clob_token_ids() -> None:
    raw = '{"event_type":"new_market","clob_token_ids":"[\\"zzz\\"]"}'
    assert lifecycle_scope(raw, SUB) == "global"
