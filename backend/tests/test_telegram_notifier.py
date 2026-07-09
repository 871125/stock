import httpx
import pytest

from app.services.telegram_notifier import TelegramNotifier


async def test_send_is_noop_when_not_configured() -> None:
    called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json={"ok": True})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        notifier = TelegramNotifier(bot_token="", chat_id="", http_client=http_client)
        assert notifier.is_configured is False
        await notifier.send("should not be sent")

    assert called is False


async def test_send_posts_to_bot_api_when_configured() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = request.content
        return httpx.Response(200, json={"ok": True})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="https://api.telegram.org") as hc:
        notifier = TelegramNotifier(bot_token="TESTTOKEN", chat_id="12345", http_client=hc)
        assert notifier.is_configured is True
        await notifier.send("hello from the bot")

    assert "botTESTTOKEN" in seen["url"]
    assert b"hello from the bot" in seen["body"]
    assert b"12345" in seen["body"]


async def test_send_raises_on_non_2xx_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"ok": False, "description": "bad request"})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="https://api.telegram.org") as hc:
        notifier = TelegramNotifier(bot_token="TESTTOKEN", chat_id="12345", http_client=hc)
        with pytest.raises(httpx.HTTPStatusError):
            await notifier.send("hello")
