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

`_MarketDataCache` (see `_refresh_ltf_cache`/`_refresh_htf_cache`) avoids
re-fetching the full HTF/LTF history on every poll: a closed candle's pivot
status never changes once computed, so that expensive re-fetch only happens
when a new hour/4h candle has actually closed, while every poll still cheaply
re-fetches just enough of the recent window to track the current (forming)
candle in real time.
"""

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx

from app.bot.state import BotState, LiveOpenTrade, load, save
from app.core.config import Settings, get_settings
from app.schemas.backtest import Candle, PivotPoint, PivotType, PositionSide, Timeframe, TrendState
from app.services.bingx_client import BingXAPIError, BingXClient
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
    OpenTrade,
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

# How far back _refresh_ltf_cache/_refresh_htf_cache look on every poll just to
# notice the current candle and detect a new close -- generous margin over one
# candle's worth of time so a slow poll or minor clock skew still sees the
# latest closed bar, while staying a single unpaginated request (see
# _MarketDataCache).
_LTF_RECENT_WINDOW_HOURS = 12
_HTF_RECENT_WINDOW_HOURS = 48

# BingX-side error codes that are transient (temporary outages on their end,
# e.g. 109500 "quote service unavailable", or 100410 "rate limited" -- our own
# request volume tripping their per-IP/per-key limiter) rather than a problem
# with our request -- worth a few quick retries before waking anyone up over
# Telegram. httpx.TransportError (connection reset mid-read, timeout, DNS
# blip, etc.) is retried the same way: it's a network-level hiccup, not a
# code problem, and usually resolves within a few seconds.
_TRANSIENT_BINGX_CODES = {109500, 100410}
_TRANSIENT_RETRY_ATTEMPTS = 3
_TRANSIENT_RETRY_BASE_SECONDS = 2.0

_HEARTBEAT_TZ = ZoneInfo("Asia/Seoul")
_HEARTBEAT_INTERVAL_HOURS = 4
# Any fixed 09:00 KST instant works as the schedule anchor -- boundaries are
# every _HEARTBEAT_INTERVAL_HOURS from here in both directions, giving
# 01:00/05:00/09:00/13:00/17:00/21:00 KST every day.
_HEARTBEAT_ANCHOR = datetime(2020, 1, 1, 9, 0, tzinfo=_HEARTBEAT_TZ)


async def run(symbol: str) -> None:
    settings = get_settings()
    market_client = BingXClient()
    trade_client = BingXTradeClient()
    notifier = TelegramNotifier()

    state = load(settings.bot_state_dir, symbol) or BotState(symbol=symbol)
    await notifier.send(f"\U0001f916 봇 시작: {symbol} (VST={settings.bingx_use_vst})")

    await _ensure_one_way_position_mode(trade_client, notifier)
    await trade_client.set_leverage(symbol, settings.leverage)
    await _reconcile_on_startup(state, symbol, trade_client, notifier)
    save(state, settings.bot_state_dir)

    await asyncio.gather(
        _poll_loop(state, symbol, settings, market_client, trade_client, notifier),
        _heartbeat_loop(symbol, notifier),
    )


async def _poll_loop(
    state: BotState,
    symbol: str,
    settings: Settings,
    market_client: BingXClient,
    trade_client: BingXTradeClient,
    notifier: TelegramNotifier,
) -> None:
    cache = _MarketDataCache()
    while True:
        try:
            # Re-checked every poll, not just at startup -- the account can
            # drift back to hedge mode mid-session (e.g. someone touches it
            # in the BingX app), and every order call after that fails with
            # 109400 until it's corrected.
            await _ensure_one_way_position_mode(trade_client, notifier)
            await _poll_once_with_retry(
                state, symbol, settings, market_client, trade_client, notifier, cache
            )
        except Exception as exc:  # noqa: BLE001 -- never let one bad poll kill the bot
            await notifier.send(f"⚠️ 봇 오류: {exc!r}")

        interval = (
            settings.bot_armed_poll_interval_seconds
            if state.pending_setup is not None
            else settings.bot_poll_interval_seconds
        )
        await asyncio.sleep(interval)


async def _heartbeat_loop(symbol: str, notifier: TelegramNotifier) -> None:
    """Sends a "still alive" ping on a fixed schedule (09/13/17/21/01/05 KST),
    independent of the poll loop and regardless of trading activity -- so a
    wedged-but-not-crashed process (e.g. hung on a request that never times
    out) is still noticeable within a few hours even during a quiet market."""
    while True:
        now = datetime.now(_HEARTBEAT_TZ)
        next_run = _next_heartbeat_time(now)
        await asyncio.sleep((next_run - now).total_seconds())
        try:
            await notifier.send(f"✅ 시스템 정상 작동 중: {symbol}")
        except Exception:  # noqa: BLE001 -- a failed heartbeat must not kill the bot
            pass


def _next_heartbeat_time(now: datetime) -> datetime:
    interval = timedelta(hours=_HEARTBEAT_INTERVAL_HOURS)
    slots_passed = (now - _HEARTBEAT_ANCHOR) // interval
    return _HEARTBEAT_ANCHOR + (slots_passed + 1) * interval


async def _ensure_one_way_position_mode(
    trade_client: BingXTradeClient, notifier: TelegramNotifier
) -> None:
    """The bot always sends positionSide="BOTH" (see bingx_trade_client.py's
    module docstring), which BingX rejects with error 109400 if the account
    is in hedge mode -- switch it to one-way before anything else runs so
    that mismatch can't surface mid-trade."""
    if await trade_client.get_position_mode_is_hedged():
        await trade_client.set_position_mode_hedged(False)
        await notifier.send("⚙️ BingX 포지션 모드를 원웨이(One-way)로 전환했습니다.")


async def _poll_once_with_retry(
    state: BotState,
    symbol: str,
    settings: Settings,
    market_client: BingXClient,
    trade_client: BingXTradeClient,
    notifier: TelegramNotifier,
    cache: "_MarketDataCache",
) -> None:
    """Retry `_poll_once` a few times on known-transient BingX errors and on
    network-level hiccups (dropped connection, read timeout, etc.).

    A single blip (e.g. code 109500, or an httpx.ReadError from a connection
    reset) should resolve within a couple of seconds and not page anyone;
    only exhausting the retries bubbles up to the caller's Telegram
    notification.
    """
    for attempt in range(_TRANSIENT_RETRY_ATTEMPTS + 1):
        try:
            await _poll_once(state, symbol, settings, market_client, trade_client, notifier, cache)
            return
        except BingXAPIError as exc:
            if exc.code not in _TRANSIENT_BINGX_CODES or attempt == _TRANSIENT_RETRY_ATTEMPTS:
                raise
            await asyncio.sleep(_TRANSIENT_RETRY_BASE_SECONDS * (2**attempt))
        except httpx.TransportError:
            if attempt == _TRANSIENT_RETRY_ATTEMPTS:
                raise
            await asyncio.sleep(_TRANSIENT_RETRY_BASE_SECONDS * (2**attempt))


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


@dataclass
class _MarketDataCache:
    """Per-run cache of HTF/LTF pivots and trend windows, keyed on the most
    recently *closed* candle seen for each timeframe. Never persisted --
    correctness only depends on data that's already closed (and therefore
    immutable), so an empty cache after a restart just forces one full
    recompute on the first poll, same as before this cache existed."""

    ltf_primed: bool = False
    ltf_last_closed_ts: datetime | None = None
    closed_ltf_candles: list[Candle] = field(default_factory=list)
    ltf_pivots: list[PivotPoint] = field(default_factory=list)

    htf_primed: bool = False
    htf_last_closed_ts: datetime | None = None
    trend_windows: list[TrendWindow] = field(default_factory=list)
    trend_timestamps: list[datetime] = field(default_factory=list)


async def _refresh_ltf_cache(
    cache: _MarketDataCache, market_client: BingXClient, symbol: str, now: datetime
) -> Candle | None:
    """Returns the current (possibly still-forming) 1h candle, off a cheap
    single-page fetch made every poll. `detect_pivots` only ever looks at
    already-*closed* candles and is a purely local sliding-window computation
    (see pivot.py), so a closed bar's pivot status can never change once
    computed -- the expensive `_LTF_LIVE_FETCH_DAYS`-day refetch + pivot
    recompute only needs to happen when a new hour has actually closed since
    the last one (about once an hour) instead of on every poll (every 8s
    while a setup is armed). Returns None if no data is available at all,
    mirroring the original unconditional-fetch behavior."""
    recent = await market_client.get_ohlcv(
        symbol, Timeframe.LTF_1H, now - timedelta(hours=_LTF_RECENT_WINDOW_HOURS), now
    )
    if not recent:
        return None

    closed_recent = [c for c in recent if _is_closed(c, now)]
    latest_closed_ts = closed_recent[-1].timestamp if closed_recent else None

    if not cache.ltf_primed or latest_closed_ts != cache.ltf_last_closed_ts:
        full = await market_client.get_ohlcv(
            symbol, Timeframe.LTF_1H, now - timedelta(days=_LTF_LIVE_FETCH_DAYS), now
        )
        if not full:
            return None
        cache.closed_ltf_candles = [c for c in full if _is_closed(c, now)]
        cache.ltf_pivots = detect_pivots(cache.closed_ltf_candles, LTF_PIVOT_LOOKBACK)
        cache.ltf_last_closed_ts = latest_closed_ts
        cache.ltf_primed = True

    return recent[-1]


async def _refresh_htf_cache(
    cache: _MarketDataCache, market_client: BingXClient, symbol: str, now: datetime
) -> bool:
    """Same reasoning as _refresh_ltf_cache, on the 4h timeframe -- a new
    close (and therefore a possible trend change) only happens every 4h.
    Returns False if no data is available at all."""
    recent = await market_client.get_ohlcv(
        symbol, Timeframe.HTF_4H, now - timedelta(hours=_HTF_RECENT_WINDOW_HOURS), now
    )
    if not recent:
        return False

    closed_recent = [c for c in recent if _is_closed(c, now, hours=4)]
    latest_closed_ts = closed_recent[-1].timestamp if closed_recent else None

    if not cache.htf_primed or latest_closed_ts != cache.htf_last_closed_ts:
        full = await market_client.get_ohlcv(
            symbol, Timeframe.HTF_4H, now - timedelta(days=HTF_LOOKBACK_BUFFER_DAYS), now
        )
        if not full:
            return False
        htf_pivots = detect_pivots(
            [c for c in full if _is_closed(c, now, hours=4)], HTF_PIVOT_LOOKBACK
        )
        cache.trend_windows = build_htf_trend_windows(htf_pivots)
        cache.trend_timestamps = [w.effective_from for w in cache.trend_windows]
        cache.htf_last_closed_ts = latest_closed_ts
        cache.htf_primed = True

    return True


async def _poll_once(
    state: BotState,
    symbol: str,
    settings: Settings,
    market_client: BingXClient,
    trade_client: BingXTradeClient,
    notifier: TelegramNotifier,
    cache: _MarketDataCache,
) -> None:
    if state.open_trade is not None:
        await _manage_open_trade(state, symbol, settings, market_client, trade_client, notifier)
        return

    now = datetime.now(UTC)
    if not await _refresh_htf_cache(cache, market_client, symbol, now):
        return
    current_candle = await _refresh_ltf_cache(cache, market_client, symbol, now)
    if current_candle is None:
        return

    closed_ltf_candles = cache.closed_ltf_candles
    ltf_pivots = cache.ltf_pivots

    window = trend_window_at(cache.trend_windows, cache.trend_timestamps, current_candle.timestamp)
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


async def _recover_untracked_fill(
    state: BotState,
    settings: Settings,
    symbol: str,
    decision: OpenTrade,
    notifier: TelegramNotifier,
) -> None:
    """Adopts a position that's already open on the exchange as `decision`'s
    fill, instead of placing another entry order on top of it. Used when a
    previous entry attempt's confirmation may have been lost (e.g. a dropped
    connection while reading place_market_order's response) -- without this,
    a retry or the next poll would call place_market_order again for the
    same setup, doubling real position size. entry_order_id is left blank
    since we never got one; _ensure_protective_orders fills in SL/TP on the
    next _manage_open_trade poll exactly like any other partially-completed
    entry (see the matching comment in _execute_trend_entry)."""
    state.open_trade = LiveOpenTrade(trade=decision, entry_order_id="")
    state.pending_setup = None
    save(state, settings.bot_state_dir)
    await notifier.send(
        f"⚠️ {symbol} 진입 주문 응답 유실 감지: 거래소에 이미 포지션이 있어 기존 체결을 "
        f"복구했습니다 (entry={decision.entry_price:.6g}). SL/TP는 다음 폴링에서 자동 복구됩니다."
    )


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

    # A previous call here for this same setup may have already placed the
    # entry order without us ever seeing confirmation (e.g. the connection
    # dropped while reading place_market_order's response) -- retrying
    # (_poll_once_with_retry) or the next poll would otherwise call
    # place_market_order again on top of a position that's already open.
    existing_position = await trade_client.get_open_position(symbol)
    if existing_position is not None:
        fill_price = existing_position.entry_price or entry_price_hint
        decision.entry_price = fill_price
        decision.stop_loss = stop_loss
        if decision.take_profit_1 is not None:
            decision.take_profit_1 = forced_take_profit(setup.side, fill_price, stop_loss)
        else:
            decision.take_profit = forced_take_profit(setup.side, fill_price, stop_loss)
        await _recover_untracked_fill(state, settings, symbol, decision, notifier)
        return True

    try:
        entry_order = await trade_client.place_market_order(symbol, side_str, decision.quantity)
    except BingXAPIError as exc:
        # Our own margin/liquidation checks (position_sizing.py) passed, so the
        # exchange's own rejection here means something in its live risk
        # calculation diverges from ours (e.g. an enforced leverage bracket
        # lower than settings.leverage) -- surface the numbers we used so a
        # recurrence is diagnosable instead of another guessing game.
        sl_distance_pct = abs(entry_price_hint - stop_loss) / entry_price_hint
        raise BingXAPIError(
            exc.code,
            f"{exc.message} [qty={decision.quantity:.6g} equity={equity:.2f} "
            f"entry_hint={entry_price_hint:.6g} sl={stop_loss:.6g} "
            f"sl_dist%={sl_distance_pct:.4%} leverage={settings.leverage}]",
        ) from exc
    fill_price = entry_order.avg_price or entry_price_hint

    # Re-derive SL/TP against the real fill price -- SL is pivot-based (unaffected),
    # TP is fill_price +/- RR*risk so it shifts with whatever slippage occurred.
    decision.entry_price = fill_price
    decision.stop_loss = stop_loss
    live_trade = LiveOpenTrade(trade=decision, entry_order_id=entry_order.order_id)

    # Record the fill immediately, before attempting SL/TP -- if any of those
    # calls below fails, the position must never end up both untracked (which
    # would let the bot re-arm and double-enter next poll) and unprotected.
    # _manage_open_trade's _ensure_protective_orders retries whichever order
    # didn't make it out here on the very next poll.
    state.open_trade = live_trade
    state.pending_setup = None
    save(state, settings.bot_state_dir)

    if decision.take_profit_1 is not None:
        assert decision.take_profit_2 is not None
        tp1_final = forced_take_profit(setup.side, fill_price, stop_loss)
        decision.take_profit_1 = tp1_final
        tp1_qty = decision.quantity * TP1_CLOSE_FRACTION
        tp2_qty = decision.quantity - tp1_qty

        sl_order = await trade_client.place_stop_market_order(
            symbol, close_side_str, stop_loss, decision.quantity
        )
        live_trade.sl_order_id = sl_order.order_id
        save(state, settings.bot_state_dir)

        tp1_order = await trade_client.place_take_profit_market_order(
            symbol, close_side_str, tp1_final, tp1_qty
        )
        live_trade.tp1_order_id = tp1_order.order_id
        save(state, settings.bot_state_dir)

        tp2_order = await trade_client.place_take_profit_market_order(
            symbol, close_side_str, decision.take_profit_2, tp2_qty
        )
        live_trade.tp2_order_id = tp2_order.order_id
        save(state, settings.bot_state_dir)
    else:
        tp_final = forced_take_profit(setup.side, fill_price, stop_loss)
        decision.take_profit = tp_final

        sl_order = await trade_client.place_stop_market_order(
            symbol, close_side_str, stop_loss, decision.quantity
        )
        live_trade.sl_order_id = sl_order.order_id
        save(state, settings.bot_state_dir)

        tp_order = await trade_client.place_take_profit_market_order(
            symbol, close_side_str, tp_final, decision.quantity
        )
        live_trade.tp_order_id = tp_order.order_id
        save(state, settings.bot_state_dir)

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

    # See the matching check in _execute_trend_entry -- a previous call here
    # may have already placed the entry order without us seeing confirmation.
    existing_position = await trade_client.get_open_position(symbol)
    if existing_position is not None:
        decision.entry_price = existing_position.entry_price or decision.entry_price
        # Box SL/TP1/TP2 are box-boundary-derived, not entry-price-derived --
        # no re-derivation needed here either.
        await _recover_untracked_fill(state, settings, symbol, decision, notifier)
        return

    entry_order = await trade_client.place_market_order(symbol, side_str, decision.quantity)
    fill_price = entry_order.avg_price or decision.entry_price
    decision.entry_price = fill_price
    # Box SL/TP1/TP2 are box-boundary-derived, not entry-price-derived -- no re-derivation needed.

    # Record the fill immediately, before attempting SL/TP -- see the matching
    # comment in _execute_trend_entry for why (untracked + unprotected is the
    # worst outcome if a later order placement call fails).
    live_trade = LiveOpenTrade(trade=decision, entry_order_id=entry_order.order_id)
    state.open_trade = live_trade
    state.pending_setup = None
    save(state, settings.bot_state_dir)

    assert decision.take_profit_1 is not None and decision.take_profit_2 is not None
    tp1_qty = decision.quantity * TP1_CLOSE_FRACTION
    tp2_qty = decision.quantity - tp1_qty

    sl_order = await trade_client.place_stop_market_order(
        symbol, close_side_str, decision.stop_loss, decision.quantity
    )
    live_trade.sl_order_id = sl_order.order_id
    save(state, settings.bot_state_dir)

    tp1_order = await trade_client.place_take_profit_market_order(
        symbol, close_side_str, decision.take_profit_1, tp1_qty
    )
    live_trade.tp1_order_id = tp1_order.order_id
    save(state, settings.bot_state_dir)

    tp2_order = await trade_client.place_take_profit_market_order(
        symbol, close_side_str, decision.take_profit_2, tp2_qty
    )
    live_trade.tp2_order_id = tp2_order.order_id
    save(state, settings.bot_state_dir)

    await notifier.send(
        f"✅ 박스권 진입: {decision.side.value} {symbol} @ {fill_price:.6g} "
        f"qty={decision.quantity:.6g}"
    )


async def _ensure_protective_orders(
    state: BotState,
    symbol: str,
    settings: Settings,
    trade_client: BingXTradeClient,
    notifier: TelegramNotifier,
) -> None:
    """Places whichever SL/TP order didn't make it out during entry (the
    exchange rejected/errored on it after the fill was already recorded --
    see _execute_trend_entry / _check_box_trade_signal). Runs at the top of
    every _manage_open_trade poll; a no-op (no network calls) once every
    expected order id is present."""
    live = state.open_trade
    assert live is not None
    trade = live.trade
    close_side_str = "SELL" if trade.side == PositionSide.LONG else "BUY"
    remaining_qty = trade.quantity * trade.remaining_fraction
    has_two_stage_tp = trade.is_box_trade or trade.take_profit_1 is not None
    placed_any = False

    if live.sl_order_id is None:
        sl_order = await trade_client.place_stop_market_order(
            symbol, close_side_str, trade.stop_loss, remaining_qty
        )
        live.sl_order_id = sl_order.order_id
        placed_any = True
        save(state, settings.bot_state_dir)

    if has_two_stage_tp:
        tp1_qty = trade.quantity * TP1_CLOSE_FRACTION
        tp2_qty = trade.quantity - tp1_qty

        if not trade.tp1_hit and live.tp1_order_id is None:
            assert trade.take_profit_1 is not None
            tp1_order = await trade_client.place_take_profit_market_order(
                symbol, close_side_str, trade.take_profit_1, tp1_qty
            )
            live.tp1_order_id = tp1_order.order_id
            placed_any = True
            save(state, settings.bot_state_dir)

        if live.tp2_order_id is None:
            assert trade.take_profit_2 is not None
            qty = remaining_qty if trade.tp1_hit else tp2_qty
            tp2_order = await trade_client.place_take_profit_market_order(
                symbol, close_side_str, trade.take_profit_2, qty
            )
            live.tp2_order_id = tp2_order.order_id
            placed_any = True
            save(state, settings.bot_state_dir)
    elif live.tp_order_id is None:
        assert trade.take_profit is not None
        tp_order = await trade_client.place_take_profit_market_order(
            symbol, close_side_str, trade.take_profit, remaining_qty
        )
        live.tp_order_id = tp_order.order_id
        placed_any = True
        save(state, settings.bot_state_dir)

    if placed_any:
        await notifier.send(f"\U0001f6e1️ 보호 주문 복구: {symbol} 누락된 SL/TP를 다시 걸었습니다.")


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
    await _ensure_protective_orders(state, symbol, settings, trade_client, notifier)
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
    trade.stop_loss = trade.entry_price  # breakeven

    # Clear sl_order_id (the old pivot-based SL is about to be canceled) and
    # persist tp1_hit/stop_loss *before* attempting the replacement SL below --
    # if that call fails, state/disk must already show "no SL order id" so
    # _ensure_protective_orders places a correct breakeven-priced one on the
    # next poll. Otherwise a retry would skip this whole tp1-fill branch
    # (tp1_hit is already True in memory) while live.sl_order_id still points
    # at the SL we're about to cancel below, leaving the position with no
    # stop loss at all until someone notices manually.
    old_sl_order_id = live.sl_order_id
    live.sl_order_id = None
    save(state, settings.bot_state_dir)

    if old_sl_order_id:
        await _cancel_ignoring_errors(trade_client, symbol, old_sl_order_id)

    close_side_str = "SELL" if trade.side == PositionSide.LONG else "BUY"
    remaining_qty = trade.quantity * trade.remaining_fraction
    new_sl_order = await trade_client.place_stop_market_order(
        symbol, close_side_str, trade.stop_loss, remaining_qty
    )
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

    # An SL/TP order reporting FILLED is a conditional-order trigger, not
    # proof the resulting close actually executed (e.g. the child market
    # order can itself fail) -- confirm against the real exchange position
    # before canceling the sibling order or clearing state, so a false
    # "filled" reading can't leave a real position both untracked and
    # unprotected (its other order canceled out from under it).
    position = await trade_client.get_open_position(symbol)
    if position is not None:
        await notifier.send(
            f"⚠️ 상태 불일치: {symbol} 주문은 체결(FILLED)로 보이는데 거래소에 "
            f"포지션이 남아있습니다 (qty={position.quantity:.6g}). 자동 종료 처리를 "
            "건너뛰었습니다 -- 수동 확인 필요."
        )
        return

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
