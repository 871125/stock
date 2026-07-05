from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "coin-autotrade-backend"
    cors_origins: list[str] = ["http://localhost:5173"]

    bingx_api_key: str = ""
    bingx_api_secret: str = ""
    bingx_base_url: str = "https://open-api.bingx.com"

    risk_per_trade_pct: float = 0.01
    leverage: int = 10
    liquidation_buffer_pct: float = 0.09


@lru_cache
def get_settings() -> Settings:
    return Settings()
