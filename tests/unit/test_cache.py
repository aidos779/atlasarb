from decimal import Decimal

from src.config.scanner_config import ScannerConfig
from src.domain.enums import VenueType
from src.domain.market import BookLevel, CanonicalSymbol, OrderBook, PriceQuote
from src.scanner.cache.market_state_cache import MarketStateCache

SYM = CanonicalSymbol("ETH", "USDT", VenueType.CEX)


def _quote(bid, ask):
    return PriceQuote("binance", SYM, Decimal(bid), Decimal(ask), Decimal(ask))


def test_untracked_pair_rejected():
    cache = MarketStateCache(ScannerConfig())
    cache.upsert_price(_quote("100", "101"))
    assert cache.get_price("binance", "ETH/USDT") is None


def test_valid_price_stored_and_warmup():
    cache = MarketStateCache(ScannerConfig())
    cache.track("binance", SYM)
    for _ in range(3):
        cache.upsert_price(_quote("100", "101"))
    assert cache.get_price("binance", "ETH/USDT") is not None
    assert cache.is_warmed_up("binance", "ETH/USDT")


def test_crossed_price_rejected():
    cache = MarketStateCache(ScannerConfig())
    cache.track("binance", SYM)
    cache.upsert_price(_quote("102", "101"))  # bid > ask
    assert cache.get_price("binance", "ETH/USDT") is None
    assert cache.rejected_prices == 1


def test_crossed_book_rejected():
    cache = MarketStateCache(ScannerConfig())
    cache.track("binance", SYM)
    book = OrderBook("binance", SYM, [BookLevel(Decimal("102"), Decimal("1"))],
                     [BookLevel(Decimal("101"), Decimal("1"))])  # crossed
    cache.upsert_book(book)
    assert cache.get_book("binance", "ETH/USDT") is None


def test_event_emitted_on_write():
    cache = MarketStateCache(ScannerConfig())
    cache.track("binance", SYM)
    events = []
    cache.subscribe(lambda b, q, v: events.append((b, q, v)))
    cache.upsert_price(_quote("100", "101"))
    assert ("ETH", "USDT", "binance") in events
