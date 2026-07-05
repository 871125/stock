from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import httpx
import pytest

from app.schemas.backtest import Timeframe
from app.services.bingx_client import BingXAPIError, BingXClient

_BASE_URL = "https://open-api.bingx.com"


def _row(time_ms: int, o: str, h: str, low: str, c: str, v: str) -> dict:
    return {"time": time_ms, "open": o, "high": h, "low": low, "close": c, "volume": v}


async def test_get_ohlcv_parses_single_page_and_sorts_ascending() -> None:
    start = datetime(2024, 1, 1, tzinfo=UTC)
    end = datetime(2024, 1, 1, 4, tzinfo=UTC)
    start_ms = int(start.timestamp() * 1000)

    # The live API returns rows newest-first.
    rows = [
        _row(start_ms + 3_600_000, "105", "115", "95", "110", "12"),
        _row(start_ms, "100", "110", "90", "105", "10"),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["X-SOURCE-KEY"] == "BX-AI-SKILL"
        return httpx.Response(200, json={"code": 0, "msg": "", "data": rows})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url=_BASE_URL) as http_client:
        client = BingXClient(http_client=http_client)
        candles = await client.get_ohlcv("BTC-USDT", Timeframe.LTF_1H, start, end)

    assert len(candles) == 2
    assert candles[0].timestamp == start  # sorted ascending despite descending input
    assert candles[0].open == 100
    assert candles[0].high == 110
    assert candles[0].low == 90
    assert candles[0].close == 105
    assert candles[0].volume == 10
    assert candles[1].close == 110


async def test_get_ohlcv_paginates_backward_until_start_is_covered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.services.bingx_client.asyncio.sleep", AsyncMock())

    # The live API silently caps returned rows below the requested `limit` (observed:
    # 1000 rows even when limit=1440), so pagination must continue on cursor position,
    # not on "got fewer rows than asked for". Simulate that with small, arbitrary page
    # sizes: a 6-hour range split across two 3-row pages.
    interval_ms = 3_600_000
    start = datetime(2024, 1, 1, tzinfo=UTC)
    start_ms = int(start.timestamp() * 1000)
    end = start + timedelta(hours=5)
    end_ms = int(end.timestamp() * 1000)

    first_page = [_row(end_ms - i * interval_ms, "100", "110", "90", "105", "1") for i in range(3)]
    second_page = [
        _row(start_ms + (2 - i) * interval_ms, "200", "210", "190", "205", "1") for i in range(3)
    ]

    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        data = first_page if len(calls) == 1 else second_page
        return httpx.Response(200, json={"code": 0, "msg": "", "data": data})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url=_BASE_URL) as http_client:
        client = BingXClient(http_client=http_client)
        candles = await client.get_ohlcv("BTC-USDT", Timeframe.LTF_1H, start, end)

    assert len(calls) == 2
    assert len(candles) == 6
    assert candles[0].timestamp == start
    assert candles[0].open == 200
    assert candles[-1].timestamp == end
    assert candles[-1].open == 100


async def test_get_ohlcv_raises_on_api_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 80001, "msg": "invalid symbol", "data": None})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url=_BASE_URL) as http_client:
        client = BingXClient(http_client=http_client)
        with pytest.raises(BingXAPIError):
            await client.get_ohlcv(
                "BAD-SYMBOL",
                Timeframe.LTF_1H,
                datetime(2024, 1, 1, tzinfo=UTC),
                datetime(2024, 1, 2, tzinfo=UTC),
            )


async def test_get_ohlcv_returns_empty_list_when_no_data() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 0, "msg": "", "data": []})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url=_BASE_URL) as http_client:
        client = BingXClient(http_client=http_client)
        candles = await client.get_ohlcv(
            "BTC-USDT",
            Timeframe.HTF_4H,
            datetime(2024, 1, 1, tzinfo=UTC),
            datetime(2024, 1, 2, tzinfo=UTC),
        )

    assert candles == []
