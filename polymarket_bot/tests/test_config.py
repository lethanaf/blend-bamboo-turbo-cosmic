from pathlib import Path

import pytest

from pmbot.config import ConfigError, LiveTradingDisabled, assert_phase1, load_config


def test_default_config_is_phase1(tmp_path: Path) -> None:
    source = Path(__file__).resolve().parents[1] / "config.yaml"
    config = load_config(source)
    assert config.live_trading is False
    assert config.max_markets == 30
    assert config.ws_assets_per_connection is None
    assert_phase1(config)


def test_live_flag_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("live_trading: true\nmax_markets: 30\nws_assets_per_connection: null\n", encoding="utf-8")
    config = load_config(path)
    with pytest.raises(LiveTradingDisabled):
        assert_phase1(config)


def test_bad_shard_rejected(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("live_trading: false\nws_assets_per_connection: 0\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(path)
