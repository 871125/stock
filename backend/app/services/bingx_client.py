"""BingX OHLCV data access.

Order execution will be added once backtesting is validated (see spec section 6).

Endpoint: GET /openApi/swap/v3/quote/klines (public market data, no signing
required). Verified against the live API directly (the published skill docs
describe a different, Binance-style array response that does not match what
the endpoint actually returns):

    {"code": 0, "msg": "", "data": [
        {"open": "71292.7", "high": "71370.2", "low": "70712.9",
         "close": "71123.1", "volume": "2422.8632", "time": 1717617600000},
        ...
    ]}

`data` is sorted **descending** by time (most recent candle first), and
`startTime`/`endTime` bound the range while `limit` caps how many of the most
recent candles in that range come back. The docs claim a max of 1440, but the
live endpoint was observed silently capping at 1000 regardless of the
requested limit -- so pagination does *not* treat "got fewer rows than
requested" as "reached the start". Instead it keeps requesting backward pages
(using each page's oldest candle as the next `endTime`) until a page is empty
or the cursor reaches `start`, then sorts the full result ascending.
"""

import asyncio
from datetime import UTC, datetime

import httpx

from app.core.config import get_settings
from app.schemas.backtest import Candle, Timeframe

_KLINES_PATH = "/openApi/swap/v3/quote/klines"
_MAX_LIMIT = 1440
_SOURCE_HEADER = {"X-SOURCE-KEY": "BX-AI-SKILL"}
_INTERVAL_MS = {
    Timeframe.LTF_1H: 60 * 60 * 1000,
    Timeframe.HTF_4H: 4 * 60 * 60 * 1000,
}


class BingXAPIError(RuntimeError):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(f"BingX error {code}: {message}")
        self.code = code


class BingXClient:
    def __init__(self, http_client: httpx.AsyncClient | None = None) -> None:
        settings = get_settings()
        self._base_url = settings.bingx_base_url
        self._http_client = http_client

    async def get_ohlcv(
        self,
        symbol: str,
        timeframe: Timeframe,
        start: datetime,
        end: datetime,
    ) -> list[Candle]:
        interval_ms = _INTERVAL_MS[timeframe]
        start_ms = int(start.timestamp() * 1000)
        cursor_end_ms = int(end.timestamp() * 1000)

        client = self._http_client or httpx.AsyncClient(base_url=self._base_url, timeout=10.0)
        owns_client = self._http_client is None
        candles: list[Candle] = []

        try:
            while cursor_end_ms >= start_ms:
                if candles:
                    await asyncio.sleep(1.1)  # stay under the 1 req/s rate limit

                response = await client.get(
                    _KLINES_PATH,
                    params={
                        "symbol": symbol,
                        "interval": timeframe.value,
                        "startTime": start_ms,
                        "endTime": cursor_end_ms,
                        "limit": _MAX_LIMIT,
                    },
                    headers=_SOURCE_HEADER,
                )
                response.raise_for_status()
                payload = response.json()

                if payload["code"] != 0:
                    raise BingXAPIError(payload["code"], payload["msg"])

                rows = payload["data"]
                if not rows:
                    break

                candles.extend(
                    Candle(
                        timestamp=datetime.fromtimestamp(int(row["time"]) / 1000, tz=UTC),
                        open=float(row["open"]),
                        high=float(row["high"]),
                        low=float(row["low"]),
                        close=float(row["close"]),
                        volume=float(row["volume"]),
                    )
                    for row in rows
                )

                oldest_time_ms = int(rows[-1]["time"])  # rows are newest-first
                next_cursor_end_ms = oldest_time_ms - interval_ms
                if next_cursor_end_ms >= cursor_end_ms:
                    break  # no backward progress; avoid an infinite loop
                cursor_end_ms = next_cursor_end_ms
        finally:
            if owns_client:
                await client.aclose()

        candles.sort(key=lambda c: c.timestamp)
        return candles
