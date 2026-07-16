"""Entry places three orders in sequence (market entry, SL, TP) against a real
exchange -- if SL or TP placement fails after the entry already filled, the
position must never end up both untracked (risking a duplicate re-entry next
poll) and unprotected (no exit orders at all). These tests pin two things:

1. _execute_trend_entry / _check_box_trade_signal record state.open_trade (and
   clear pending_setup) the moment the entry fills, before attempting SL/TP --
   so a later failure still leaves the position tracked.
2. _ensure_protective_orders (called every _manage_open_trade poll) fills in
   whichever SL/TP order didn't make it out, and is a no-op once everything's
   present.
"""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.bot import engine
from app.bot.state import BotState, LiveOpenTrade
from app.core.config import Settings
from app.schemas.backtest import PositionSide
from app.services.trading_logic import OpenTrade, build_pending_setup, trend_stop_loss


class _NotifierSpy:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def send(self, message: str) -> None:
        self.messages.append(message)


class _OrderResult:
    def __init__(self, order_id: str) -> None:
        self.order_id = order_id
        self.status = "NEW"
        self.avg_price: float | None = None
        self.executed_qty: float | None = None


class _FakeTradeClient:
    """Records every order call; `fail_on` names a method that raises instead
    of returning, simulating the exchange rejecting that specific call."""

    def __init__(self, fail_on: str | None = None) -> None:
        self.fail_on = fail_on
        self.calls: list[tuple] = []
        self._next_id = 1

    def _order_id(self) -> str:
        order_id = f"order-{self._next_id}"
        self._next_id += 1
        return order_id

    async def get_available_balance(self, asset: str = "USDT") -> float:
        return 10_000.0

    async def place_market_order(self, symbol, side, quantity, **kwargs):
        if self.fail_on == "entry":
            raise RuntimeError("entry rejected")
        self.calls.append(("entry", side, quantity))
        return _OrderResult(self._order_id())

    async def place_stop_market_order(self, symbol, side, stop_price, quantity, **kwargs):
        if self.fail_on == "sl":
            raise RuntimeError("sl rejected")
        self.calls.append(("sl", side, stop_price, quantity))
        return _OrderResult(self._order_id())

    async def place_take_profit_market_order(self, symbol, side, stop_price, quantity, **kwargs):
        if self.fail_on == "tp":
            raise RuntimeError("tp rejected")
        self.calls.append(("tp", side, stop_price, quantity))
        return _OrderResult(self._order_id())


def _plain_trade(sl_order_id=None, tp_order_id=None) -> LiveOpenTrade:
    trade = OpenTrade(
        sequence_no=0,
        side=PositionSide.LONG,
        entry_price=100.0,
        entry_time=datetime.now(UTC),
        quantity=1.0,
        is_box_trade=False,
        stop_loss=95.0,
        take_profit=110.0,
    )
    return LiveOpenTrade(
        trade=trade, entry_order_id="entry-1", sl_order_id=sl_order_id, tp_order_id=tp_order_id
    )


def _two_stage_trade(tp1_hit: bool, sl_order_id="sl-1", tp1_order_id=None, tp2_order_id=None):
    trade = OpenTrade(
        sequence_no=0,
        side=PositionSide.LONG,
        entry_price=100.0,
        entry_time=datetime.now(UTC),
        quantity=1.0,
        is_box_trade=True,
        stop_loss=95.0,
        take_profit_1=105.0,
        take_profit_2=115.0,
        tp1_hit=tp1_hit,
        remaining_fraction=0.5 if tp1_hit else 1.0,
    )
    return LiveOpenTrade(
        trade=trade,
        entry_order_id="entry-1",
        sl_order_id=sl_order_id,
        tp1_order_id=tp1_order_id,
        tp2_order_id=tp2_order_id,
    )


async def test_ensure_protective_orders_is_noop_when_all_present(tmp_path: Path) -> None:
    live_trade = _plain_trade(sl_order_id="sl-1", tp_order_id="tp-1")
    state = BotState(symbol="ETH-USDT", open_trade=live_trade)
    settings = Settings(bot_state_dir=str(tmp_path))
    trade_client = _FakeTradeClient()
    notifier = _NotifierSpy()

    await engine._ensure_protective_orders(state, "ETH-USDT", settings, trade_client, notifier)

    assert trade_client.calls == []
    assert notifier.messages == []


async def test_ensure_protective_orders_fills_missing_sl_and_tp(tmp_path: Path) -> None:
    state = BotState(symbol="ETH-USDT", open_trade=_plain_trade())
    settings = Settings(bot_state_dir=str(tmp_path))
    trade_client = _FakeTradeClient()
    notifier = _NotifierSpy()

    await engine._ensure_protective_orders(state, "ETH-USDT", settings, trade_client, notifier)

    live = state.open_trade
    assert live is not None
    assert live.sl_order_id is not None
    assert live.tp_order_id is not None
    assert len(notifier.messages) == 1
    assert "보호 주문 복구" in notifier.messages[0]


async def test_ensure_protective_orders_respects_tp1_hit_for_tp2_quantity(tmp_path: Path) -> None:
    state = BotState(symbol="ETH-USDT", open_trade=_two_stage_trade(tp1_hit=True))
    settings = Settings(bot_state_dir=str(tmp_path))
    trade_client = _FakeTradeClient()
    notifier = _NotifierSpy()

    await engine._ensure_protective_orders(state, "ETH-USDT", settings, trade_client, notifier)

    live = state.open_trade
    assert live is not None
    assert live.tp1_order_id is None  # TP1 already hit -- must not re-place it
    assert live.tp2_order_id is not None
    tp2_call = next(c for c in trade_client.calls if c[0] == "tp")
    assert tp2_call[3] == pytest.approx(0.5)  # remaining_fraction after TP1


async def test_execute_trend_entry_tracks_position_before_sl_placement_can_fail(
    tmp_path: Path,
) -> None:
    settings = Settings(bot_state_dir=str(tmp_path), risk_per_trade_pct=0.01, leverage=10)
    setup = build_pending_setup(PositionSide.LONG, pivot_price=95.0, extreme_price=110.0)
    state = BotState(symbol="ETH-USDT", pending_setup=setup)
    trade_client = _FakeTradeClient(fail_on="sl")
    notifier = _NotifierSpy()

    with pytest.raises(RuntimeError, match="sl rejected"):
        await engine._execute_trend_entry(
            state, "ETH-USDT", settings, setup, 100.0, trade_client, notifier
        )

    # Entry filled before the failing SL call -- must already be tracked so
    # the bot doesn't re-arm and double-enter on the next poll.
    assert state.open_trade is not None
    assert state.open_trade.entry_order_id == "order-1"
    assert state.open_trade.sl_order_id is None
    assert state.pending_setup is None
    expected_sl = trend_stop_loss(PositionSide.LONG, 95.0)
    assert expected_sl == pytest.approx(state.open_trade.trade.stop_loss)
