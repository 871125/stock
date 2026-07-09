"""BingX Swap (perpetual futures) authenticated trading client.

Separate from `bingx_client.py` (public OHLCV, unsigned) -- this one signs
every request with the account's API key/secret and is used only by the live
bot (`app/bot`) to query balance/positions and place/cancel real orders.

Signing scheme (BingX Swap V2, HMAC-SHA256): every request carries a
`timestamp` (ms) parameter; all parameters (including `timestamp`, excluding
`signature` itself) are joined in ascending key order as
`key1=value1&key2=value2...`, HMAC-SHA256'd with the API secret, and the hex
digest is sent as the `signature` parameter alongside an `X-BX-APIKEY`
header. Parameters travel in the query string for both GET and POST (BingX
does not use a JSON body for trade endpoints).

Endpoint paths/params below are written from best-known BingX Swap V2/V3
documentation but have NOT been exercised against a live account from this
session -- verify every one of them against BingX VST before relying on this
client (see docs/backtest_results.md's sibling plan notes / README). Wrong
values fail loudly (BingXAPIError / non-2xx), so iterating against VST is
safe.

One-way position mode is assumed throughout (`positionSide="BOTH"`) -- the
backtested strategy never holds more than one position at a time, so hedge
mode adds nothing but complexity. If the BingX account defaults to hedge
mode, switch it to one-way in account settings before running the bot.
"""

import hashlib
import hmac
import time
from dataclasses import dataclass

import httpx

from app.core.config import get_settings
from app.services.bingx_client import BingXAPIError

_BALANCE_PATH = "/openApi/swap/v2/user/balance"
_LEVERAGE_PATH = "/openApi/swap/v2/trade/leverage"
_ORDER_PATH = "/openApi/swap/v2/trade/order"
_POSITIONS_PATH = "/openApi/swap/v2/user/positions"

_ONE_WAY_POSITION_SIDE = "BOTH"


@dataclass
class OrderResult:
    order_id: str
    status: str
    avg_price: float | None = None
    executed_qty: float | None = None


@dataclass
class PositionInfo:
    symbol: str
    quantity: float  # signed: positive = long, negative = short, 0 = flat
    entry_price: float | None = None


def sign_params(secret: str, params: dict[str, str]) -> str:
    """HMAC-SHA256 signature over params joined in ascending key order."""
    return hmac.new(secret.encode(), _signing_string(params).encode(), hashlib.sha256).hexdigest()


def _signing_string(params: dict[str, str]) -> str:
    return "&".join(f"{key}={params[key]}" for key in sorted(params))


class BingXTradeClient:
    def __init__(self, http_client: httpx.AsyncClient | None = None) -> None:
        settings = get_settings()
        self._base_url = settings.bingx_trade_base_url
        self._api_key = settings.bingx_api_key
        self._api_secret = settings.bingx_api_secret
        self._http_client = http_client

    async def _request(self, method: str, path: str, params: dict[str, str]) -> dict:
        signed = dict(params)
        signed["timestamp"] = str(int(time.time() * 1000))
        # The signature must be computed over -- and sent in -- the exact same
        # key order (ascending), since BingX verifies the HMAC against the
        # literal query string it receives rather than re-sorting server-side.
        query_string = _signing_string(signed)
        signature = sign_params(self._api_secret, signed)
        query_string += f"&signature={signature}"

        client = self._http_client or httpx.AsyncClient(base_url=self._base_url, timeout=10.0)
        owns_client = self._http_client is None
        try:
            response = await client.request(
                method,
                path,
                params=query_string,
                headers={"X-BX-APIKEY": self._api_key},
            )
            response.raise_for_status()
            payload = response.json()
        finally:
            if owns_client:
                await client.aclose()

        if payload.get("code", 0) != 0:
            raise BingXAPIError(payload.get("code", -1), payload.get("msg", "unknown error"))
        return payload["data"]

    async def get_available_balance(self, asset: str = "USDT") -> float:
        data = await self._request("GET", _BALANCE_PATH, {})
        balance = data.get("balance", data)  # tolerate either {balance: {...}} or {...}
        if isinstance(balance, list):
            balance = next((b for b in balance if b.get("asset") == asset), {})
        return float(balance.get("availableMargin", balance.get("balance", 0.0)))

    async def set_leverage(
        self, symbol: str, leverage: int, side: str = _ONE_WAY_POSITION_SIDE
    ) -> None:
        await self._request(
            "POST",
            _LEVERAGE_PATH,
            {"symbol": symbol, "side": side, "leverage": str(leverage)},
        )

    async def place_market_order(
        self,
        symbol: str,
        side: str,  # "BUY" or "SELL"
        quantity: float,
        reduce_only: bool = False,
        position_side: str = _ONE_WAY_POSITION_SIDE,
    ) -> OrderResult:
        return await self._place_order(
            symbol=symbol,
            side=side,
            order_type="MARKET",
            quantity=quantity,
            reduce_only=reduce_only,
            position_side=position_side,
        )

    async def place_stop_market_order(
        self,
        symbol: str,
        side: str,
        stop_price: float,
        quantity: float,
        position_side: str = _ONE_WAY_POSITION_SIDE,
    ) -> OrderResult:
        return await self._place_order(
            symbol=symbol,
            side=side,
            order_type="STOP_MARKET",
            quantity=quantity,
            reduce_only=True,
            position_side=position_side,
            stop_price=stop_price,
        )

    async def place_take_profit_market_order(
        self,
        symbol: str,
        side: str,
        stop_price: float,
        quantity: float,
        position_side: str = _ONE_WAY_POSITION_SIDE,
    ) -> OrderResult:
        return await self._place_order(
            symbol=symbol,
            side=side,
            order_type="TAKE_PROFIT_MARKET",
            quantity=quantity,
            reduce_only=True,
            position_side=position_side,
            stop_price=stop_price,
        )

    async def _place_order(
        self,
        symbol: str,
        side: str,
        order_type: str,
        quantity: float,
        reduce_only: bool,
        position_side: str,
        stop_price: float | None = None,
    ) -> OrderResult:
        params = {
            "symbol": symbol,
            "side": side,
            "positionSide": position_side,
            "type": order_type,
            "quantity": str(quantity),
            "reduceOnly": str(reduce_only).lower(),
        }
        if stop_price is not None:
            params["stopPrice"] = str(stop_price)

        data = await self._request("POST", _ORDER_PATH, params)
        order = data.get("order", data)
        return OrderResult(
            order_id=str(order.get("orderId", order.get("order_id", ""))),
            status=str(order.get("status", "")),
            avg_price=_maybe_float(order.get("avgPrice")),
            executed_qty=_maybe_float(order.get("executedQty")),
        )

    async def cancel_order(self, symbol: str, order_id: str) -> None:
        await self._request("DELETE", _ORDER_PATH, {"symbol": symbol, "orderId": order_id})

    async def get_order_status(self, symbol: str, order_id: str) -> OrderResult:
        data = await self._request("GET", _ORDER_PATH, {"symbol": symbol, "orderId": order_id})
        order = data.get("order", data)
        return OrderResult(
            order_id=str(order.get("orderId", order_id)),
            status=str(order.get("status", "")),
            avg_price=_maybe_float(order.get("avgPrice")),
            executed_qty=_maybe_float(order.get("executedQty")),
        )

    async def get_open_position(self, symbol: str) -> PositionInfo | None:
        data = await self._request("GET", _POSITIONS_PATH, {"symbol": symbol})
        positions = data if isinstance(data, list) else data.get("positions", [])
        for position in positions:
            quantity = float(position.get("positionAmt", 0.0))
            if quantity != 0.0:
                return PositionInfo(
                    symbol=symbol,
                    quantity=quantity,
                    entry_price=_maybe_float(position.get("avgPrice")),
                )
        return None


def _maybe_float(value: object) -> float | None:
    return float(value) if value not in (None, "") else None
