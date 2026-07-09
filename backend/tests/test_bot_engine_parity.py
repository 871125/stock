"""Proves the live bot's own orchestration (arming a setup, executing an
entry) reaches the exact same decision -- entry price, SL, TP -- as the
backtester for the identical candle sequence. The underlying decision
functions are already the same imported functions (trading_logic.py), so
this specifically exercises app/bot/engine.py's wiring around them, which is
the part that could silently drift even though the shared logic can't.

Reuses the uptrend fixture from test_backtest_engine.py:
test_simulate_full_uptrend_long_trade_hits_take_profit already established
that the backtester enters LONG at 108.1 with stop_loss=102.897 and
take_profit=118.506 for this exact candle sequence -- this test drives the
same data through app/bot/engine.py's functions and asserts the same numbers.
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.bot import engine
from app.bot.state import BotState
from app.core.config import Settings
from app.schemas.backtest import Candle, PositionSide, TrendState
from app.services.bingx_trade_client import OrderResult
from app.services.pivot import detect_pivots
from app.services.trading_logic import (
    HTF_PIVOT_LOOKBACK,
    LTF_PIVOT_LOOKBACK,
    build_htf_trend_windows,
    trend_window_at,
)
from tests.test_backtest_engine import _uptrend_htf_candles, _uptrend_ltf_candles


class _NullNotifier:
    async def send(self, message: str) -> None:
        pass


class _FakeMarketClient:
    """Ignores the requested symbol/timeframe/date range and always returns
    the fixed 1-minute candles the test configures -- same trick the
    backtest tests use for their fetch_1m stub."""

    def __init__(self, one_minute_candles: list[Candle]) -> None:
        self._one_minute_candles = one_minute_candles

    async def get_ohlcv(self, symbol, timeframe, start, end) -> list[Candle]:
        return self._one_minute_candles


class _FakeTradeClient:
    def __init__(self, fill_price: float, balance: float = 10_000.0) -> None:
        self._fill_price = fill_price
        self._balance = balance
        self.placed_orders: list[tuple] = []

    async def get_available_balance(self, asset: str = "USDT") -> float:
        return self._balance

    async def place_market_order(
        self, symbol, side, quantity, reduce_only=False, position_side="BOTH"
    ):
        self.placed_orders.append(("market", side, quantity))
        return OrderResult(order_id="entry-1", status="FILLED", avg_price=self._fill_price)

    async def place_stop_market_order(
        self, symbol, side, stop_price, quantity, position_side="BOTH"
    ):
        self.placed_orders.append(("stop", side, stop_price, quantity))
        return OrderResult(order_id=f"sl-{len(self.placed_orders)}", status="NEW")

    async def place_take_profit_market_order(
        self, symbol, side, stop_price, quantity, position_side="BOTH"
    ):
        self.placed_orders.append(("tp", side, stop_price, quantity))
        return OrderResult(order_id=f"tp-{len(self.placed_orders)}", status="NEW")


async def test_bot_engine_matches_backtest_uptrend_entry(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(engine, "TREND_TP_HYBRID_MODE", False)
    # bot_state_dir must be isolated from the real `state/` dir -- engine._try_arm_pending_setup
    # / _advance_pending_setup call save() as a side effect, and the real dir is what the live
    # bot reads on startup (a leaked "BTC-USDT.state.json" here would look like a real position).
    settings = Settings(risk_per_trade_pct=0.01, leverage=10, bot_state_dir=str(tmp_path))

    htf_candles = _uptrend_htf_candles()
    ltf_candles = _uptrend_ltf_candles()

    htf_pivots = detect_pivots(htf_candles, HTF_PIVOT_LOOKBACK)
    trend_windows = build_htf_trend_windows(htf_pivots)
    trend_timestamps = [w.effective_from for w in trend_windows]

    # --- Step 1: arm the pending setup exactly like simulate() does at the hour-81
    # confirmation candle (backtest comment: "confirmation bar; extreme=110, not touched").
    closed_through_confirmation = ltf_candles[:5]  # hours 77-81
    window = trend_window_at(
        trend_windows, trend_timestamps, closed_through_confirmation[-1].timestamp
    )
    assert window is not None
    assert window.trend == TrendState.UPTREND
    assert window.recent_sh_price == 134  # opposite HTF swing at setup time

    ltf_pivots = detect_pivots(closed_through_confirmation, LTF_PIVOT_LOOKBACK)
    state = BotState(symbol="BTC-USDT")
    await engine._try_arm_pending_setup(
        state, settings, closed_through_confirmation, ltf_pivots, window, _NullNotifier()
    )

    assert state.pending_setup is not None
    assert state.pending_setup.side == PositionSide.LONG
    assert state.pending_setup.pivot_price == 103
    assert state.pending_setup.extreme_price == 110  # hour-81 candle's high
    assert state.pending_setup.tp2_price == 134

    # --- Step 2: advance to hour 82 -- zone touch + 1m reversal-close entry,
    # same 1-minute candles as test_simulate_full_uptrend_long_trade_hits_take_profit.
    current_candle = ltf_candles[5]  # hour 82
    reversal_time = current_candle.timestamp + timedelta(minutes=1)
    one_minute_candles = [
        Candle(
            timestamp=current_candle.timestamp,
            open=108.0,
            high=108.2,
            low=107.3,
            close=107.7,
            volume=1.0,
        ),
        Candle(
            timestamp=reversal_time,
            open=107.7,
            high=108.5,
            low=107.4,
            close=108.1,
            volume=1.0,
        ),
    ]

    fake_market = _FakeMarketClient(one_minute_candles)
    fake_trade = _FakeTradeClient(fill_price=108.1)
    now = datetime.now(UTC)

    await engine._advance_pending_setup(
        state, "BTC-USDT", settings, current_candle, now, fake_market, fake_trade, _NullNotifier()
    )

    assert state.pending_setup is None
    assert state.open_trade is not None
    trade = state.open_trade.trade

    # These are the exact numbers test_simulate_full_uptrend_long_trade_hits_take_profit
    # asserts for the backtester given this same candle sequence.
    expected_stop_loss = 103 * (1 - 0.001)
    expected_take_profit = 108.1 + 2.0 * (108.1 - expected_stop_loss)

    assert trade.side == PositionSide.LONG
    assert trade.entry_price == 108.1
    assert trade.stop_loss == pytest.approx(expected_stop_loss)
    assert trade.take_profit == pytest.approx(expected_take_profit)
    assert trade.quantity > 0

    # And the bot placed real orders sized/priced to match that decision.
    assert ("market", "BUY", pytest.approx(trade.quantity)) in [
        (o[0], o[1], o[2]) for o in fake_trade.placed_orders
    ]
    stop_orders = [o for o in fake_trade.placed_orders if o[0] == "stop"]
    tp_orders = [o for o in fake_trade.placed_orders if o[0] == "tp"]
    assert len(stop_orders) == 1
    assert stop_orders[0][2] == pytest.approx(expected_stop_loss)
    assert len(tp_orders) == 1
    assert tp_orders[0][2] == pytest.approx(expected_take_profit)

    # State was written to the isolated tmp dir, not the real state/ dir.
    assert (tmp_path / "BTC-USDT.state.json").exists()
