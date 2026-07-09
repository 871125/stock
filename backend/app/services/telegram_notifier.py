"""Telegram alerts for the live bot (app/bot).

A thin wrapper over the Bot API's sendMessage -- no extra dependency, same
httpx-based style as the rest of the codebase. Silently does nothing if
telegram_bot_token/telegram_chat_id aren't configured, so the bot can still
run (with a startup warning logged) before Telegram is set up.
"""

import httpx

from app.core.config import get_settings

_API_BASE = "https://api.telegram.org"


class TelegramNotifier:
    def __init__(
        self,
        bot_token: str | None = None,
        chat_id: str | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        settings = get_settings()
        self._bot_token = bot_token if bot_token is not None else settings.telegram_bot_token
        self._chat_id = chat_id if chat_id is not None else settings.telegram_chat_id
        self._http_client = http_client

    @property
    def is_configured(self) -> bool:
        return bool(self._bot_token and self._chat_id)

    async def send(self, message: str) -> None:
        if not self.is_configured:
            return

        client = self._http_client or httpx.AsyncClient(base_url=_API_BASE, timeout=10.0)
        owns_client = self._http_client is None
        try:
            response = await client.post(
                f"/bot{self._bot_token}/sendMessage",
                json={"chat_id": self._chat_id, "text": message},
            )
            response.raise_for_status()
        finally:
            if owns_client:
                await client.aclose()
