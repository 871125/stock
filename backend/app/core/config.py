from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "coin-autotrade-backend"
    # Matches the Vite dev server on localhost, 127.0.0.1, or any private LAN IP,
    # so the frontend also works when opened from another device on the network.
    cors_origin_regex: str = r"^https?://(localhost|127\.0\.0\.1|(\d{1,3}\.){3}\d{1,3}):5173$"

    bingx_api_key: str = ""
    bingx_api_secret: str = ""
    bingx_base_url: str = "https://open-api.bingx.com"

    risk_per_trade_pct: float = 0.01
    leverage: int = 10
    # Liquidation buffer is derived from leverage (see
    # position_sizing.derive_liquidation_buffer_pct), not set independently.

    # --- Live bot (app/bot) -----------------------------------------------------------
    # BingX VST (demo/paper trading) uses the same signing scheme and endpoint shapes as
    # live trading but a separate base URL and a separately-issued demo API key/secret
    # (put the demo key in bingx_api_key/bingx_api_secret while bingx_use_vst is true).
    # Always verify VST behavior before ever flipping this to false.
    bingx_use_vst: bool = True
    bingx_vst_trade_base_url: str = "https://open-api-vst.bingx.com"
    bingx_live_trade_base_url: str = "https://open-api.bingx.com"

    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    bot_symbol: str = "BTC-USDT"
    bot_state_dir: str = "state"
    bot_poll_interval_seconds: float = 45.0
    bot_armed_poll_interval_seconds: float = 8.0

    @property
    def bingx_trade_base_url(self) -> str:
        return (
            self.bingx_vst_trade_base_url if self.bingx_use_vst else self.bingx_live_trade_base_url
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
