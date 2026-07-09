"""Live trading loop for a single symbol.

Polls BingX on a fixed interval and makes the exact same decisions the
backtester would (see app/services/trading_logic.py for why), translating
each decision into real orders instead of simulated bookkeeping:

- No open trade, no pending setup: refetch HTF/LTF candles, rebuild trend
  windows the same way `simulate()` does, and look for a newly-confirmed LTF
  pivot in the trend direction (arms a pending setup) or an RSI box signal
  during consolidation.
- Pending setup armed: track the post-pivot extreme against the *current*
  (possibly still-forming) 1h candle so zone-touch is detected in real time,
  then poll 1-minute candles for the reversal-close entry trigger exactly
  like the backtester's 1m timing layer.
- Open trade: BingX's own STOP_MARKET/TAKE_PROFIT_MARKET orders do the actual
  SL/TP execution, so this loop only polls order status to notice fills and
  mirror the same bookkeeping transitions `advance_box_trade` /
  `advance_hybrid_trend_trade` describe (TP1 partial -> breakeven SL). The
  one thing the exchange can't do as a conditional order is the box trade's
  RSI-based TP2 exit, so that's evaluated here every poll and closed with a
  market order when triggered.

Pivot confirmation only ever looks at the most recently *closed* LTF candle
(tracked via `last_processed_pivot_timestamp` so it's examined exactly once)
-- this mirrors the backtester's strictly sequential, no-backlog candle walk:
a pivot that confirms while the bot is offline or busy with another setup is
not retroactively picked up later, same as the backtester would never look
backward either.
"""

import asyncio
from datetime import UTC, datetime, timedelta

from app.bot.state import BotState, LiveOpenTrade, load, save
from app.core.config import Settings, get_settings
from app.schemas.backtest import Candle, PivotPoint, PivotType, PositionSide, Timeframe, TrendState
from app.services.bingx_client import BingXClient
from app.services.bingx_trade_client import BingXTradeClient
from app.services.indicators import rsi
from app.services.pivot import detect_pivots
from app.services.telegram_notifier import TelegramNotifier
from app.services.trading_logic import (
    HTF_LOOKBACK_BUFFER_DAYS,
    HTF_PIVOT_LOOKBACK,
    LTF_PIVOT_LOOKBACK,
    RSI_OVERBOUGHT,
    RSI_OVERSOLD,
    RSI_PERIOD,
    TP1_CLOSE_FRACTION,
    TREND_TP_HYBRID_MODE,
    PendingSetup,
    TrendWindow,
    build_htf_trend_windows,
    build_pending_setup,
    find_1m_reversal_entry,
    forced_take_profit,
    pending_setup_invalidated,
    retracement_zone_price,
    setup_matches_trend,
    signed_pnl,
    trend_stop_loss,
    trend_window_at,
    try_open_box_trade_on_rsi_signal,
    try_open_hybrid_trend_trade,
    try_open_trend_trade,
    update_pending_setup_extreme,
)

_LTF_LIVE_FETCH_DAYS = 90
_FILLED_STATUS = "FILLED"


async def run(symbol: str) -> None:
    settings = get_settings()
    market_client = BingXClient()
    trade_client = BingXTradeClient()
    notifier = TelegramNotifier()

    state = load(settings.bot_state_dir, symbol) or BotState(symbol=symbol)
    await notifier.send(f"\U0001f916 봇 시작: {symbol} (VST={settings.bingx_use_vst})")

    await trade_client.set_leverage(symbol, settings.leverage)
    await _reconcile_on_startup(state, symbol, trade_client, notifier)
    save(state, settings.bot_state_dir)

    while True:
        try:
            await _poll_once(state, symbol, settings, market_client, trade_client, notifier)
        except Exception as exc:  # noqa: BLE001 -- never let one bad poll kill the bot
            await notifier.send(f"⚠️ 봇 오류: {exc!r}")

        interval = (
            settings.bot_armed_poll_interval_seconds
            if state.pending_setup is not None
            else settings.bot_poll_interval_seconds
        )
        await asyncio.sleep(interval)


async def _reconcile_on_startup(
    state: BotState, symbol: str, trade_client: BingXTradeClient, notifier: TelegramNotifier
) -> None:
    position = await trade_client.get_open_position(symbol)
    has_exchange_position = position is not None
    has_state_position = state.open_trade is not None
    if has_exchange_position != has_state_position:
        await notifier.send(
            "⚠️ 상태 불일치: "
            f"거래소 포지션={has_exchange_position}, "
            f"저장된 상태={has_state_position}. 수동 확인 필요."
        )


async def _poll_once(
    state: BotState,
    symbol: str,
    settings: Settings,
    market_client: BingXClient,
    trade_client: BingXTradeClient,
    notifier: TelegramNotifier,
) -> None:
    if state.open_trade is not None:
        await _manage_open_trade(state, symbol, settings, market_client, trade_client, notifier)
        return

    now = datetime.now(UTC)
    htf_candles = await market_client.get_ohlcv(
        symbol, Timeframe.HTF_4H, now - timedelta(days=HTF_LOOKBACK_BUFFER_DAYS), now
    )
    ltf_candles = await market_client.get_ohlcv(
        symbol, Timeframe.LTF_1H, now - timedelta(days=_LTF_LIVE_FETCH_DAYS), now
    )
    if not htf_candles or not ltf_candles:
        return

    closed_ltf_candles = [c for c in ltf_candles if _is_closed(c, now)]
    current_candle = ltf_candles[-1]

    htf_pivots = detect_pivots(
        [c for c in htf_candles if _is_closed(c, now, hours=4)], HTF_PIVOT_LOOKBACK
    )
    ltf_pivots = detect_pivots(closed_ltf_candles, LTF_PIVOT_LOOKBACK)
    trend_windows = build_htf_trend_windows(htf_pivots)
    trend_timestamps = [w.effective_from for w in trend_windows]

    window = trend_window_at(trend_windows, trend_timestamps, current_candle.timestamp)
    if window is None:
        return

    if state.pending_setup is not None:
        if not setup_matches_trend(state.pending_setup, window.trend):
            state.pending_setup = None
            save(state, settings.bot_state_dir)
            return
        await _advance_pending_setup(
            state, symbol, settings, current_candle, now, market_client, trade_client, notifier
        )
        return

    if closed_ltf_candles:
        await _try_arm_pending_setup(
            state, settings, closed_ltf_candles, ltf_pivots, window, notifier
        )
        if state.pending_setup is not None:
            return

    if window.trend == TrendState.CONSOLIDATION and len(closed_ltf_candles) >= RSI_PERIOD + 2:
        await _check_box_trade_signal(
            state, symbol, settings, closed_ltf_candles, window, trade_client, notifier
        )


def _is_closed(candle: Candle, now: datetime, hours: int = 1) -> bool:
    return candle.timestamp + timedelta(hours=hours) <= now


async def _try_arm_pending_setup(
    state: BotState,
    settings: Settings,
    closed_ltf_candles: list[Candle],
    ltf_pivots: list[PivotPoint],
    window: TrendWindow,
    notifier: TelegramNotifier,
) -> None:
    newest_closed = closed_ltf_candles[-1]
    newest_ts = newest_closed.timestamp.isoformat()
    if newest_ts == state.last_processed_pivot_timestamp:
        return  # already examined this candle's confirmed-pivot list

    state.last_processed_pivot_timestamp = newest_ts
    confirm_index = len(closed_ltf_candles) - 1

    for pivot in ltf_pivots:
        if pivot.index + LTF_PIVOT_LOOKBACK != confirm_index:
            continue
        if window.trend == TrendState.UPTREND and pivot.type == PivotType.SWING_LOW:
            state.pending_setup = build_pending_setup(
                PositionSide.LONG,
                pivot.price,
                extreme_price=newest_closed.high,
                tp2_price=window.recent_sh_price,
            )
            await notifier.send(f"\U0001f4cd 대기 셋업: LONG {state.symbol} pivot={pivot.price}")
        elif window.trend == TrendState.DOWNTREND and pivot.type == PivotType.SWING_HIGH:
            state.pending_setup = build_pending_setup(
                PositionSide.SHORT,
                pivot.price,
                extreme_price=newest_closed.low,
                tp2_price=window.recent_sl_price,
            )
            await notifier.send(f"\U0001f4cd 대기 셋업: SHORT {state.symbol} pivot={pivot.price}")
        break

    save(state, settings.bot_state_dir)


async def _advance_pending_setup(
    state: BotState,
    symbol: str,
    settings: Settings,
    current_candle: Candle,
    now: datetime,
    market_client: BingXClient,
    trade_client: BingXTradeClient,
    notifier: TelegramNotifier,
) -> None:
    setup = state.pending_setup
    assert setup is not None
    update_pending_setup_extreme(setup, current_candle)

    zone_price = retracement_zone_price(setup)
    touched = (
        current_candle.low <= zone_price
        if setup.side == PositionSide.LONG
        else current_candle.high >= zone_price
    )

    if touched:
        hour_start = current_candle.timestamp
        one_minute_candles = await market_client.get_ohlcv(
            symbol, Timeframe.LTF_1M, hour_start, hour_start + timedelta(hours=1)
        )
        entry = find_1m_reversal_entry(setup.side, zone_price, one_minute_candles)
        if entry is not None:
            entry_price_hint, _entry_time = entry
            filled = await _execute_trend_entry(
                state, symbol, settings, setup, entry_price_hint, trade_client, notifier
            )
            if filled:
                state.pending_setup = None
                save(state, settings.bot_state_dir)
                return

    if _is_closed(current_candle, now) and pending_setup_invalidated(setup, current_candle):
        state.pending_setup = None

    save(state, settings.bot_state_dir)


async def _execute_trend_entry(
    state: BotState,
    symbol: str,
    settings: Settings,
    setup: PendingSetup,
    entry_price_hint: float,
    trade_client: BingXTradeClient,
    notifier: TelegramNotifier,
) -> bool:
    """Validates + sizes the trade with the exact same decision function the
    backtester uses (fed the 1m reversal-close price as an estimate), places
    the real market order, then re-derives SL/TP against the actual fill
    price. Returns False (without touching state) if the decision function
    rejects the setup -- caller keeps the pending setup armed and retries on
    a later poll, same as the backtester tries again on the next hour."""
    equity = await trade_client.get_available_balance()
    stop_loss = trend_stop_loss(setup.side, setup.pivot_price)
    tp1_estimate = forced_take_profit(setup.side, entry_price_hint, stop_loss)

    if TREND_TP_HYBRID_MODE:
        assert setup.tp2_price is not None
        decision = try_open_hybrid_trend_trade(
            side=setup.side,
            entry_price=entry_price_hint,
            pivot_price=setup.pivot_price,
            tp1_price=tp1_estimate,
            tp2_price=setup.tp2_price,
            entry_time=datetime.now(UTC),
            equity=equity,
            settings=settings,
        )
    else:
        decision = try_open_trend_trade(
            side=setup.side,
            entry_price=entry_price_hint,
            pivot_price=setup.pivot_price,
            tp_price=tp1_estimate,
            entry_time=datetime.now(UTC),
            equity=equity,
            settings=settings,
        )
    if decision is None:
        return False

    side_str = "BUY" if setup.side == PositionSide.LONG else "SELL"
    close_side_str = "SELL" if setup.side == PositionSide.LONG else "BUY"
    entry_order = await trade_client.place_market_order(symbol, side_str, decision.quantity)
    fill_price = entry_order.avg_price or entry_price_hint

    # Re-derive SL/TP against the real fill price -- SL is pivot-based (unaffected),
    # TP is fill_price +/- RR*risk so it shifts with whatever slippage occurred.
    decision.entry_price = fill_price
    decision.stop_loss = stop_loss
    live_trade = LiveOpenTrade(trade=decision, entry_order_id=entry_order.order_id)

    if decision.take_profit_1 is not None:
        assert decision.take_profit_2 is not None
        tp1_final = forced_take_profit(setup.side, fill_price, stop_loss)
        decision.take_profit_1 = tp1_final
        tp1_qty = decision.quantity * TP1_CLOSE_FRACTION
        tp2_qty = decision.quantity - tp1_qty
        sl_order = await trade_client.place_stop_market_order(
            symbol, close_side_str, stop_loss, decision.quantity
        )
        tp1_order = await trade_client.place_take_profit_market_order(
            symbol, close_side_str, tp1_final, tp1_qty
        )
        tp2_order = await trade_client.place_take_profit_market_order(
            symbol, close_side_str, decision.take_profit_2, tp2_qty
        )
        live_trade.sl_order_id = sl_order.order_id
        live_trade.tp1_order_id = tp1_order.order_id
        live_trade.tp2_order_id = tp2_order.order_id
    else:
        tp_final = forced_take_profit(setup.side, fill_price, stop_loss)
        decision.take_profit = tp_final
        sl_order = await trade_client.place_stop_market_order(
            symbol, close_side_str, stop_loss, decision.quantity
        )
        tp_order = await trade_client.place_take_profit_market_order(
            symbol, close_side_str, tp_final, decision.quantity
        )
        live_trade.sl_order_id = sl_order.order_id
        live_trade.tp_order_id = tp_order.order_id

    state.open_trade = live_trade
    await notifier.send(
        f"✅ 진입: {decision.side.value} {symbol} @ {fill_price:.6g} "
        f"qty={decision.quantity:.6g} SL={decision.stop_loss:.6g}"
    )
    return True


async def _check_box_trade_signal(
    state: BotState,
    symbol: str,
    settings: Settings,
    closed_ltf_candles: list[Candle],
    window: TrendWindow,
    trade_client: BingXTradeClient,
    notifier: TelegramNotifier,
) -> None:
    closes = [c.close for c in closed_ltf_candles]
    rsi_values = rsi(closes, RSI_PERIOD)
    prev_rsi, cur_rsi = rsi_values[-2], rsi_values[-1]
    candle = closed_ltf_candles[-1]

    equity = await trade_client.get_available_balance()
    decision = try_open_box_trade_on_rsi_signal(candle, prev_rsi, cur_rsi, window, equity, settings)
    if decision is None:
        return

    side_str = "BUY" if decision.side == PositionSide.LONG else "SELL"
    close_side_str = "SELL" if decision.side == PositionSide.LONG else "BUY"
    entry_order = await trade_client.place_market_order(symbol, side_str, decision.quantity)
    fill_price = entry_order.avg_price or decision.entry_price
    decision.entry_price = fill_price
    # Box SL/TP1/TP2 are box-boundary-derived, not entry-price-derived -- no re-derivation needed.

    assert decision.take_profit_1 is not None and decision.take_profit_2 is not None
    tp1_qty = decision.quantity * TP1_CLOSE_FRACTION
    tp2_qty = decision.quantity - tp1_qty
    sl_order = await trade_client.place_stop_market_order(
        symbol, close_side_str, decision.stop_loss, decision.quantity
    )
    tp1_order = await trade_client.place_take_profit_market_order(
        symbol, close_side_str, decision.take_profit_1, tp1_qty
    )
    tp2_order = await trade_client.place_take_profit_market_order(
        symbol, close_side_str, decision.take_profit_2, tp2_qty
    )

    state.open_trade = LiveOpenTrade(
        trade=decision,
        entry_order_id=entry_order.order_id,
        sl_order_id=sl_order.order_id,
        tp1_order_id=tp1_order.order_id,
        tp2_order_id=tp2_order.order_id,
    )
    state.pending_setup = None
    save(state, settings.bot_state_dir)
    await notifier.send(
        f"✅ 박스권 진입: {decision.side.value} {symbol} @ {fill_price:.6g} "
        f"qty={decision.quantity:.6g}"
    )


async def _manage_open_trade(
    state: BotState,
    symbol: str,
    settings: Settings,
    market_client: BingXClient,
    trade_client: BingXTradeClient,
    notifier: TelegramNotifier,
) -> None:
    live = state.open_trade
    assert live is not None
    trade = live.trade
    has_two_stage_tp = trade.is_box_trade or trade.take_profit_1 is not None

    if has_two_stage_tp:
        if not trade.tp1_hit and live.tp1_order_id:
            status = await trade_client.get_order_status(symbol, live.tp1_order_id)
            if status.status == _FILLED_STATUS:
                await _handle_tp1_fill(state, symbol, settings, trade_client, notifier)
                return

        if live.sl_order_id:
            sl_status = await trade_client.get_order_status(symbol, live.sl_order_id)
            if sl_status.status == _FILLED_STATUS:
                await _handle_full_close(
                    state,
                    symbol,
                    settings,
                    trade_client,
                    notifier,
                    exit_price=trade.stop_loss,
                    cancel_order_ids=_present(live.tp1_order_id, live.tp2_order_id),
                )
                return

        if live.tp2_order_id:
            tp2_status = await trade_client.get_order_status(symbol, live.tp2_order_id)
            if tp2_status.status == _FILLED_STATUS:
                assert trade.take_profit_2 is not None
                await _handle_full_close(
                    state,
                    symbol,
                    settings,
                    trade_client,
                    notifier,
                    exit_price=trade.take_profit_2,
                    cancel_order_ids=_present(live.sl_order_id),
                )
                return

        if trade.is_box_trade:
            await _check_box_rsi_exit(
                state, symbol, settings, market_client, trade_client, notifier
            )
        return

    if live.sl_order_id:
        sl_status = await trade_client.get_order_status(symbol, live.sl_order_id)
        if sl_status.status == _FILLED_STATUS:
            await _handle_full_close(
                state,
                symbol,
                settings,
                trade_client,
                notifier,
                exit_price=trade.stop_loss,
                cancel_order_ids=_present(live.tp_order_id),
            )
            return

    if live.tp_order_id:
        tp_status = await trade_client.get_order_status(symbol, live.tp_order_id)
        if tp_status.status == _FILLED_STATUS:
            assert trade.take_profit is not None
            await _handle_full_close(
                state,
                symbol,
                settings,
                trade_client,
                notifier,
                exit_price=trade.take_profit,
                cancel_order_ids=_present(live.sl_order_id),
            )
            return


def _present(*order_ids: str | None) -> list[str]:
    return [oid for oid in order_ids if oid]


async def _check_box_rsi_exit(
    state: BotState,
    symbol: str,
    settings: Settings,
    market_client: BingXClient,
    trade_client: BingXTradeClient,
    notifier: TelegramNotifier,
) -> None:
    live = state.open_trade
    assert live is not None
    trade = live.trade

    now = datetime.now(UTC)
    ltf_candles = await market_client.get_ohlcv(
        symbol, Timeframe.LTF_1H, now - timedelta(days=5), now
    )
    closed = [c for c in ltf_candles if _is_closed(c, now)]
    if len(closed) < RSI_PERIOD + 1:
        return

    cur_rsi = rsi([c.close for c in closed], RSI_PERIOD)[-1]
    if cur_rsi is None:
        return

    tp2_rsi_hit = (trade.side == PositionSide.LONG and cur_rsi >= RSI_OVERBOUGHT) or (
        trade.side == PositionSide.SHORT and cur_rsi <= RSI_OVERSOLD
    )
    if not tp2_rsi_hit:
        return

    close_side_str = "SELL" if trade.side == PositionSide.LONG else "BUY"
    remaining_qty = trade.quantity * trade.remaining_fraction
    close_order = await trade_client.place_market_order(
        symbol, close_side_str, remaining_qty, reduce_only=True
    )
    exit_price = close_order.avg_price or closed[-1].close
    await _handle_full_close(
        state,
        symbol,
        settings,
        trade_client,
        notifier,
        exit_price=exit_price,
        cancel_order_ids=_present(live.sl_order_id, live.tp2_order_id),
    )


async def _handle_tp1_fill(
    state: BotState,
    symbol: str,
    settings: Settings,
    trade_client: BingXTradeClient,
    notifier: TelegramNotifier,
) -> None:
    live = state.open_trade
    assert live is not None
    trade = live.trade
    assert trade.take_profit_1 is not None

    partial_qty = trade.quantity * TP1_CLOSE_FRACTION
    trade.realized_pnl += signed_pnl(
        trade.side, trade.entry_price, trade.take_profit_1, partial_qty
    )
    trade.remaining_fraction -= TP1_CLOSE_FRACTION
    trade.tp1_hit = True

    if live.sl_order_id:
        await _cancel_ignoring_errors(trade_client, symbol, live.sl_order_id)

    close_side_str = "SELL" if trade.side == PositionSide.LONG else "BUY"
    remaining_qty = trade.quantity * trade.remaining_fraction
    new_sl_order = await trade_client.place_stop_market_order(
        symbol, close_side_str, trade.entry_price, remaining_qty
    )
    trade.stop_loss = trade.entry_price  # breakeven
    live.sl_order_id = new_sl_order.order_id

    save(state, settings.bot_state_dir)
    await notifier.send(
        f"\U0001f7e2 TP1 부분청산: {symbol} {partial_qty:.6g} @ "
        f"{trade.take_profit_1:.6g} (SL→본절가 이동)"
    )


async def _handle_full_close(
    state: BotState,
    symbol: str,
    settings: Settings,
    trade_client: BingXTradeClient,
    notifier: TelegramNotifier,
    exit_price: float,
    cancel_order_ids: list[str],
) -> None:
    live = state.open_trade
    assert live is not None
    trade = live.trade
    remaining_qty = trade.quantity * trade.remaining_fraction
    pnl = signed_pnl(trade.side, trade.entry_price, exit_price, remaining_qty)
    total_pnl = trade.realized_pnl + pnl

    for order_id in cancel_order_ids:
        await _cancel_ignoring_errors(trade_client, symbol, order_id)

    state.open_trade = None
    save(state, settings.bot_state_dir)

    result = "승" if total_pnl > 0 else "패"
    await notifier.send(
        f"\U0001f3c1 포지션 종료 ({result}): {symbol} pnl={total_pnl:.2f} " f"@ {exit_price:.6g}"
    )


async def _cancel_ignoring_errors(
    trade_client: BingXTradeClient, symbol: str, order_id: str
) -> None:
    try:
        await trade_client.cancel_order(symbol, order_id)
    except Exception:  # noqa: BLE001 -- order may already be filled/expired/canceled
        pass
