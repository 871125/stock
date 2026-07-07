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


@lru_cache
def get_settings() -> Settings:
    return Settings()
