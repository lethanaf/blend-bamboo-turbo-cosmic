import json
from pathlib import Path

from pmbot.data.book import top_of_book

FIXTURE = Path(__file__).parent / "fixtures" / "book.json"


def test_best_prices_are_not_index_zero() -> None:
    book = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert book["asks"][0]["price"] == "0.999"
    top = top_of_book(book)
    assert top["best_bid"] is None
    assert top["best_ask"] == "0.001"
    assert top["best_ask_size"] == "203224.49"
    assert top["ask_levels"] == 47
    assert top["bid_levels"] == 0


def test_best_bid_is_max() -> None:
    top = top_of_book(
        {
            "bids": [{"price": "0.10", "size": "1"}, {"price": "0.40", "size": "5"}],
            "asks": [{"price": "0.90", "size": "2"}, {"price": "0.45", "size": "3"}],
        }
    )
    assert top["best_bid"] == "0.40"
    assert top["best_bid_size"] == "5"
    assert top["best_ask"] == "0.45"
    assert top["best_ask_size"] == "3"
