from pmbot.data.shard import oversized_groups, shard_markets


def test_no_documented_cap_is_one_connection() -> None:
    groups = [["yes1", "no1"], ["yes2", "no2"]]
    assert shard_markets(groups, None) == [["yes1", "no1", "yes2", "no2"]]
    assert oversized_groups(groups, None) == []


def test_shards_keep_a_market_together() -> None:
    groups = [["y1", "n1"], ["y2", "n2"], ["y3", "n3"]]
    assert shard_markets(groups, 3) == [["y1", "n1"], ["y2", "n2"], ["y3", "n3"]]
    assert shard_markets(groups, 4) == [["y1", "n1", "y2", "n2"], ["y3", "n3"]]


def test_oversized_market_is_not_split() -> None:
    groups = [["a", "b", "c", "d", "e"], ["y", "n"]]
    assert shard_markets(groups, 2) == [["a", "b", "c", "d", "e"], ["y", "n"]]
    assert oversized_groups(groups, 2) == [["a", "b", "c", "d", "e"]]
