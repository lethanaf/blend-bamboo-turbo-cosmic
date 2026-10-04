"""Load config.yaml. Paths are resolved relative to the config file."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from pmbot.endpoints import CLOB_BASE, GAMMA_BASE, WS_MARKET


class ConfigError(ValueError):
    pass


class LiveTradingDisabled(RuntimeError):
    """Raised when a Phase 1 entrypoint sees live_trading: true."""


@dataclass(frozen=True)
class Config:
    live_trading: bool
    gamma_base: str
    clob_base: str
    ws_market: str
    max_markets: int
    market_order: str
    market_order_ascending: bool
    gamma_page_size: int
    gamma_max_pages: int
    ws_assets_per_connection: int | None
    ping_interval_s: float
    reconnect_backoff_initial_s: float
    reconnect_backoff_max_s: float
    ws_stale_s: float
    backoff_reset_after_s: float
    reconcile_interval_s: float
    jsonl_flush_interval_s: float
    jsonl_flush_records: int
    rest_book_concurrency: int
    http_timeout_s: float
    http_attempts: int
    custom_feature_enabled: bool
    data_dir: Path
    log_level: str
    book_404_end_after: int = 3
    gamma_refresh_interval_s: float = 1800.0

    @property
    def sqlite_path(self) -> Path:
        return self.data_dir / "catalog.sqlite"

    @property
    def books_dir(self) -> Path:
        return self.data_dir / "books"

    @property
    def log_path(self) -> Path:
        return self.data_dir / "recorder.log"


def assert_phase1(config: Config) -> None:
    if config.live_trading:
        raise LiveTradingDisabled(
            "live_trading=true is refused. Phase 1 has no order path; live mode is Phase 6."
        )


def load_config(path: Path) -> Config:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ConfigError(f"{path} must be a mapping")
    root = path.resolve().parent

    def as_bool(key: str, default: bool) -> bool:
        value = raw.get(key, default)
        if not isinstance(value, bool):
            raise ConfigError(f"{key} must be a boolean, got {value!r}")
        return value

    def as_int(key: str, default: int, minimum: int) -> int:
        value = raw.get(key, default)
        if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
            raise ConfigError(f"{key} must be an integer >= {minimum}")
        return value

    def as_float(key: str, default: float, minimum: float) -> float:
        value = raw.get(key, default)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value < minimum:
            raise ConfigError(f"{key} must be a number >= {minimum}")
        return float(value)

    shard = raw.get("ws_assets_per_connection", None)
    if shard is not None and (isinstance(shard, bool) or not isinstance(shard, int) or shard < 1):
        raise ConfigError("ws_assets_per_connection must be null or an integer >= 1")

    data_dir = Path(raw.get("data_dir", "data"))
    if not data_dir.is_absolute():
        data_dir = root / data_dir

    return Config(
        live_trading=as_bool("live_trading", False),
        gamma_base=str(raw.get("gamma_base", GAMMA_BASE)).rstrip("/"),
        clob_base=str(raw.get("clob_base", CLOB_BASE)).rstrip("/"),
        ws_market=str(raw.get("ws_market", WS_MARKET)),
        max_markets=as_int("max_markets", 30, 1),
        market_order=str(raw.get("market_order", "volume24hr")),
        market_order_ascending=as_bool("market_order_ascending", False),
        gamma_page_size=as_int("gamma_page_size", 100, 1),
        gamma_max_pages=as_int("gamma_max_pages", 10, 1),
        ws_assets_per_connection=shard,
        ping_interval_s=as_float("ping_interval_s", 10, 1),
        reconnect_backoff_initial_s=as_float("reconnect_backoff_initial_s", 1, 0),
        reconnect_backoff_max_s=as_float("reconnect_backoff_max_s", 30, 0),
        ws_stale_s=as_float("ws_stale_s", 30, 1),
        backoff_reset_after_s=as_float("backoff_reset_after_s", 30, 0),
        reconcile_interval_s=as_float("reconcile_interval_s", 300, 1),
        jsonl_flush_interval_s=as_float("jsonl_flush_interval_s", 1, 0.05),
        jsonl_flush_records=as_int("jsonl_flush_records", 500, 1),
        rest_book_concurrency=as_int("rest_book_concurrency", 4, 1),
        http_timeout_s=as_float("http_timeout_s", 30, 1),
        http_attempts=as_int("http_attempts", 3, 1),
        custom_feature_enabled=as_bool("custom_feature_enabled", True),
        data_dir=data_dir,
        log_level=str(raw.get("log_level", "INFO")),
        book_404_end_after=as_int("book_404_end_after", 3, 1),
        gamma_refresh_interval_s=as_float("gamma_refresh_interval_s", 1800, 1),
    )
