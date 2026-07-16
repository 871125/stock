"""_refresh_ltf_cache/_refresh_htf_cache should only re-fetch and recompute
the full pivot/trend history (_LTF_LIVE_FETCH_DAYS / HTF_LOOKBACK_BUFFER_DAYS
of candles) when a new candle has actually closed since the last computation.
`detect_pivots` is a purely local sliding-window computation over already-
*closed* candles (see pivot.py), so a closed bar's pivot status can never
change once computed -- reusing the cached result is what keeps a polling
bot from re-fetching ~90/180 days of history on every 8-second armed poll,
which is what tripped BingX's rate limiter (code 100410).
"""

from datetime import UTC, datetime, timedelta

from app.bot import engine
from app.schemas.backtest import Candle, Timeframe

NOW = datetime(2026, 7, 16, 10, 0, tzinfo=UTC)


def _candle(ts: datetime, price: float) -> Candle:
    return Candle(timestamp=ts, open=price, high=price + 1, low=price - 1, close=price, volume=1.0)


class _FakeMarketClient:
    """Tracks every get_ohlcv call and serves canned responses keyed by
    timeframe and request span: a "recent" (<=2 day) cheap window, or a
    "full" (multi-day) history fetch."""

    def __init__(self) -> None:
        self.calls: list[tuple[Timeframe, str]] = []
        self.recent: dict[Timeframe, list[Candle]] = {}
        self.full: dict[Timeframe, list[Candle]] = {}

    async def get_ohlcv(self, symbol, timeframe, start, end):
        kind = "full" if (end - start) > timedelta(days=2) else "recent"
        self.calls.append((timeframe, kind))
        return (self.full if kind == "full" else self.recent).get(timeframe, [])

    def full_call_count(self, timeframe: Timeframe) -> int:
        return sum(1 for tf, kind in self.calls if tf == timeframe and kind == "full")


async def test_refresh_ltf_cache_primes_from_full_fetch_on_first_call():
    fake = _FakeMarketClient()
    closed = [_candle(NOW - timedelta(hours=h), 100 + h) for h in range(3, 0, -1)]
    forming = _candle(NOW, 999)
    fake.recent[Timeframe.LTF_1H] = [*closed, forming]
    fake.full[Timeframe.LTF_1H] = closed

    cache = engine._MarketDataCache()
    current = await engine._refresh_ltf_cache(cache, fake, "BTC-USDT", NOW)

    assert current is forming
    assert cache.ltf_primed is True
    assert cache.ltf_last_closed_ts == closed[-1].timestamp
    assert cache.closed_ltf_candles == closed
    assert fake.full_call_count(Timeframe.LTF_1H) == 1


async def test_refresh_ltf_cache_skips_full_fetch_when_no_new_close():
    fake = _FakeMarketClient()
    closed = [_candle(NOW - timedelta(hours=h), 100 + h) for h in range(3, 0, -1)]
    fake.recent[Timeframe.LTF_1H] = [*closed, _candle(NOW, 999)]
    fake.full[Timeframe.LTF_1H] = closed

    cache = engine._MarketDataCache()
    await engine._refresh_ltf_cache(cache, fake, "BTC-USDT", NOW)
    assert fake.full_call_count(Timeframe.LTF_1H) == 1

    # A later poll (8s on, well within the same hour): same closed candles,
    # but the forming candle's price has moved.
    later = NOW + timedelta(seconds=8)
    fake.recent[Timeframe.LTF_1H] = [*closed, _candle(NOW, 1234)]
    current = await engine._refresh_ltf_cache(cache, fake, "BTC-USDT", later)

    assert current.close == 1234  # fresh forming-candle data even on a cache hit
    assert fake.full_call_count(Timeframe.LTF_1H) == 1  # no redundant full refetch


async def test_refresh_ltf_cache_refetches_full_when_a_new_hour_closes():
    fake = _FakeMarketClient()
    closed = [_candle(NOW - timedelta(hours=h), 100 + h) for h in range(3, 0, -1)]
    fake.recent[Timeframe.LTF_1H] = [*closed, _candle(NOW, 999)]
    fake.full[Timeframe.LTF_1H] = closed

    cache = engine._MarketDataCache()
    await engine._refresh_ltf_cache(cache, fake, "BTC-USDT", NOW)
    assert fake.full_call_count(Timeframe.LTF_1H) == 1

    # An hour later: the previously-forming candle is now closed, and a new
    # one has started forming.
    later = NOW + timedelta(hours=1)
    new_closed = [*closed, _candle(NOW, 999)]
    fake.recent[Timeframe.LTF_1H] = [*new_closed, _candle(later, 1000)]
    fake.full[Timeframe.LTF_1H] = new_closed

    current = await engine._refresh_ltf_cache(cache, fake, "BTC-USDT", later)

    assert current.timestamp == later
    assert cache.ltf_last_closed_ts == new_closed[-1].timestamp
    assert cache.closed_ltf_candles == new_closed
    assert fake.full_call_count(Timeframe.LTF_1H) == 2


async def test_refresh_ltf_cache_returns_none_with_no_data():
    fake = _FakeMarketClient()  # no recent/full data configured
    cache = engine._MarketDataCache()

    current = await engine._refresh_ltf_cache(cache, fake, "BTC-USDT", NOW)

    assert current is None
    assert cache.ltf_primed is False
    assert fake.full_call_count(Timeframe.LTF_1H) == 0


async def test_refresh_htf_cache_primes_and_then_skips_full_fetch():
    fake = _FakeMarketClient()
    closed = [_candle(NOW - timedelta(hours=4 * h), 100 + h) for h in range(3, 0, -1)]
    fake.recent[Timeframe.HTF_4H] = [*closed, _candle(NOW, 999)]
    fake.full[Timeframe.HTF_4H] = closed

    cache = engine._MarketDataCache()
    assert await engine._refresh_htf_cache(cache, fake, "BTC-USDT", NOW) is True
    assert cache.htf_primed is True
    assert fake.full_call_count(Timeframe.HTF_4H) == 1

    later = NOW + timedelta(seconds=8)
    assert await engine._refresh_htf_cache(cache, fake, "BTC-USDT", later) is True
    assert fake.full_call_count(Timeframe.HTF_4H) == 1  # cache hit, no new 4h close yet


async def test_refresh_htf_cache_refetches_full_when_a_new_candle_closes():
    fake = _FakeMarketClient()
    closed = [_candle(NOW - timedelta(hours=4 * h), 100 + h) for h in range(3, 0, -1)]
    fake.recent[Timeframe.HTF_4H] = [*closed, _candle(NOW, 999)]
    fake.full[Timeframe.HTF_4H] = closed

    cache = engine._MarketDataCache()
    await engine._refresh_htf_cache(cache, fake, "BTC-USDT", NOW)
    assert fake.full_call_count(Timeframe.HTF_4H) == 1

    later = NOW + timedelta(hours=4)
    new_closed = [*closed, _candle(NOW, 999)]
    fake.recent[Timeframe.HTF_4H] = [*new_closed, _candle(later, 1000)]
    fake.full[Timeframe.HTF_4H] = new_closed

    assert await engine._refresh_htf_cache(cache, fake, "BTC-USDT", later) is True
    assert cache.htf_last_closed_ts == new_closed[-1].timestamp
    assert fake.full_call_count(Timeframe.HTF_4H) == 2


async def test_refresh_htf_cache_returns_false_with_no_data():
    fake = _FakeMarketClient()
    cache = engine._MarketDataCache()

    assert await engine._refresh_htf_cache(cache, fake, "BTC-USDT", NOW) is False
    assert cache.htf_primed is False
