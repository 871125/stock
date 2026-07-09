"""Live bot entrypoint: `python -m app.bot.run` from the backend/ directory.

Runs a single symbol (settings.bot_symbol) forever until interrupted
(Ctrl+C). See docs/backtest_results.md and README.md's "매매 로직 상세"
section for the strategy this executes, and app/services/trading_logic.py
for why the bot's decisions are guaranteed to match the backtest.
"""

import asyncio
import logging

from app.bot import engine
from app.core.config import get_settings
from app.services.telegram_notifier import TelegramNotifier

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("bot")


async def _main() -> None:
    settings = get_settings()
    mode = "VST(모의투자)" if settings.bingx_use_vst else "실전"
    logger.info("starting bot: symbol=%s mode=%s", settings.bot_symbol, mode)

    notifier = TelegramNotifier()
    try:
        await engine.run(settings.bot_symbol)
    except (KeyboardInterrupt, asyncio.CancelledError):
        logger.info("shutdown requested")
    finally:
        await notifier.send(f"\U0001f6d1 봇 종료: {settings.bot_symbol}")


def main() -> None:
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        logger.info("interrupted")


if __name__ == "__main__":
    main()
