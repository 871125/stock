import hashlib
import hmac

import httpx
import pytest

from app.services.bingx_client import BingXAPIError
from app.services.bingx_trade_client import BingXTradeClient, sign_params

_BASE_URL = "https://open-api-vst.bingx.com"


def test_sign_params_matches_manual_hmac_sha256() -> None:
    params = {"symbol": "BTC-USDT", "timestamp": "1700000000000", "side": "BUY"}
    expected_query = "side=BUY&symbol=BTC-USDT&timestamp=1700000000000"  # ascending key order
    expected = hmac.new(b"my-secret", expected_query.encode(), hashlib.sha256).hexdigest()

    assert sign_params("my-secret", params) == expected


def test_sign_params_is_order_independent_in_input_dict() -> None:
    a = sign_params("secret", {"b": "2", "a": "1"})
    b = sign_params("secret", {"a": "1", "b": "2"})
    assert a == b


async def test_get_available_balance_signs_request_and_parses_response() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["headers"] = request.headers
        seen["query"] = dict(httpx.QueryParams(request.url.query))
        return httpx.Response(
            200,
            json={"code": 0, "msg": "", "data": {"balance": {"availableMargin": "1234.56"}}},
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url=_BASE_URL) as http_client:
        client = BingXTradeClient(http_client=http_client)
        balance = await client.get_available_balance()

    assert balance == pytest.approx(1234.56)
    assert "X-BX-APIKEY" in seen["headers"]
    assert "timestamp" in seen["query"]
    assert "signature" in seen["query"]


async def test_place_market_order_parses_order_result() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        query = dict(httpx.QueryParams(request.url.query))
        assert query["symbol"] == "BTC-USDT"
        assert query["side"] == "BUY"
        assert query["type"] == "MARKET"
        assert query["positionSide"] == "BOTH"
        assert query["reduceOnly"] == "false"
        return httpx.Response(
            200,
            json={
                "code": 0,
                "msg": "",
                "data": {"order": {"orderId": "42", "status": "FILLED", "avgPrice": "100.5"}},
            },
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url=_BASE_URL) as http_client:
        client = BingXTradeClient(http_client=http_client)
        result = await client.place_market_order("BTC-USDT", "BUY", quantity=0.01)

    assert result.order_id == "42"
    assert result.status == "FILLED"
    assert result.avg_price == pytest.approx(100.5)


async def test_place_stop_market_order_includes_stop_price_and_reduce_only() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        query = dict(httpx.QueryParams(request.url.query))
        assert query["type"] == "STOP_MARKET"
        assert query["stopPrice"] == "95.0"
        assert query["reduceOnly"] == "true"
        return httpx.Response(
            200,
            json={"code": 0, "msg": "", "data": {"order": {"orderId": "43", "status": "NEW"}}},
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url=_BASE_URL) as http_client:
        client = BingXTradeClient(http_client=http_client)
        result = await client.place_stop_market_order(
            "BTC-USDT", "SELL", stop_price=95.0, quantity=0.01
        )

    assert result.order_id == "43"


async def test_error_response_raises_bingx_api_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 100202, "msg": "Insufficient margin"})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url=_BASE_URL) as http_client:
        client = BingXTradeClient(http_client=http_client)
        with pytest.raises(BingXAPIError) as exc_info:
            await client.get_available_balance()

    assert exc_info.value.code == 100202


async def test_get_open_position_returns_none_when_flat() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 0, "msg": "", "data": []})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url=_BASE_URL) as http_client:
        client = BingXTradeClient(http_client=http_client)
        position = await client.get_open_position("BTC-USDT")

    assert position is None


async def test_get_open_position_returns_info_when_open() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "code": 0,
                "msg": "",
                "data": [{"positionAmt": "0.05", "avgPrice": "101.2"}],
            },
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url=_BASE_URL) as http_client:
        client = BingXTradeClient(http_client=http_client)
        position = await client.get_open_position("BTC-USDT")

    assert position is not None
    assert position.quantity == pytest.approx(0.05)
    assert position.entry_price == pytest.approx(101.2)
