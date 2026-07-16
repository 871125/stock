"""_handle_full_close must not trust an SL/TP order's FILLED status blindly --
an ETH-USDT incident showed the bot declaring "포지션 종료" (and canceling the
sibling order) while BingX still had the position open, which both loses
state tracking of a real position AND strips its remaining protective order.
These tests pin the fix: cross-check trade_client.get_open_position() before
treating a fill as a genuine close.
"""

from datetime import UTC, datetime
from pathlib import Path

from app.bot import engine
from app.bot.state import BotState, LiveOpenTrade
from app.core.config import Settings
from app.schemas.backtest import PositionSide
from app.services.bingx_trade_client import PositionInfo
from app.services.trading_logic import OpenTrade


class _NotifierSpy:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def send(self, message: str) -> None:
        self.messages.append(message)


class _FakeTradeClient:
    def __init__(self, position: PositionInfo | None) -> None:
        self._position = position
        self.cancelled_order_ids: list[str] = []

    async def get_open_position(self, symbol: str) -> PositionInfo | None:
        return self._position

    async def cancel_order(self, symbol: str, order_id: str) -> None:
        self.cancelled_order_ids.append(order_id)


def _open_state() -> BotState:
    trade = OpenTrade(
        sequence_no=0,
        side=PositionSide.LONG,
        entry_price=1812.65,
        entry_time=datetime.now(UTC),
        quantity=0.0509537,
        is_box_trade=False,
        stop_loss=1784.45376,
        take_profit=1869.04248,
    )
    live = LiveOpenTrade(
        trade=trade, entry_order_id="entry-1", sl_order_id="sl-1", tp_order_id="tp-1"
    )
    return BotState(symbol="ETH-USDT", open_trade=live)


async def test_skips_close_when_position_still_open_on_exchange(tmp_path: Path) -> None:
    state = _open_state()
    settings = Settings(bot_state_dir=str(tmp_path))
    trade_client = _FakeTradeClient(position=PositionInfo(symbol="ETH-USDT", quantity=0.0509537))
    notifier = _NotifierSpy()

    await engine._handle_full_close(
        state,
        "ETH-USDT",
        settings,
        trade_client,
        notifier,
        exit_price=1784.45,
        cancel_order_ids=["tp-1"],
    )

    # State and the sibling order must survive -- only a mismatch warning goes out.
    assert state.open_trade is not None
    assert trade_client.cancelled_order_ids == []
    assert len(notifier.messages) == 1
    assert "상태 불일치" in notifier.messages[0]


async def test_closes_normally_when_position_confirmed_flat(tmp_path: Path) -> None:
    state = _open_state()
    settings = Settings(bot_state_dir=str(tmp_path))
    trade_client = _FakeTradeClient(position=None)
    notifier = _NotifierSpy()

    await engine._handle_full_close(
        state,
        "ETH-USDT",
        settings,
        trade_client,
        notifier,
        exit_price=1784.45,
        cancel_order_ids=["tp-1"],
    )

    assert state.open_trade is None
    assert trade_client.cancelled_order_ids == ["tp-1"]
    assert len(notifier.messages) == 1
    assert "포지션 종료" in notifier.messages[0]
