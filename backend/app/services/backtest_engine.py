"""Backtest orchestration: MTF pivot detection, trend classification,
entry/SL/TP resolution (spec sections 3-4), and position sizing (spec 5).

Trend-trade entries are further refined with a 1-minute timing layer: once
the LTF (1h) pivot ("OB") that would normally trigger an immediate entry is
confirmed, the engine instead tracks the most favorable price reached since
(the post-pivot extreme) and waits for price to retrace RETRACEMENT_RATIO
(50%, a simple half retracement) of the way back toward the pivot -- not
a full round-trip to the pivot itself -- then looks inside that hour's
1-minute candles for a reversal-close confirmation before entering.

Trend-trade TP is a forced TREND_TP_RISK_REWARD_RATIO (2:1) multiple of the
SL distance from the 1m entry price, not the nearest opposite HTF swing --
the HTF swing is frequently too far away to realistically reach, so pinning
TP to a fixed multiple of risk raises the odds of actually hitting it. SL is
unaffected -- still derived from the 1h pivot exactly as before.

Simplifications (documented, not covered by the spec text):
- HTF pivots are detected once over the full fetched history and treated as
  "known" starting `HTF_PIVOT_LOOKBACK` candles after they occur (same for
  LTF). A live bot would track unconfirmed pivots incrementally (spec 6.2);
  this backtester recomputes over the full range instead.
- At most one position (or one pending 1m-entry setup) is open at a time.
- Trend-trade SL/TP are resolved against candle wicks (high/low); box-trade
  SL is resolved against the candle body close, per spec 4.3.
- On a bar where both SL and TP are touched, SL is assumed to trigger first
  (conservative).
- If a position is still open at the end of the requested range, it is
  force-closed at the last candle's close so it's reflected in the summary.
- A pending setup that touches its zone but gets no 1m reversal-close within
  that hour keeps waiting on subsequent hours, until either it fills, the 1h
  candle closes past the (precomputed) stop-loss, or the HTF trend context
  no longer supports it.
- BingX only retains 1-minute klines for roughly the past year (verified
  live: 365 days back returns data, 730 days back returns none). For
  backtest ranges older than that, `fetch_1m` returns an empty list for
  every zone touch, so trend-trade setups never fill -- only the RSI box
  strategy (unaffected by this feature) can still produce trades that far
  back.
"""

import asyncio
import bisect
from collections import defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from app.core.config import Settings, get_settings
from app.schemas.backtest import (
    BacktestRequest,
    BacktestResult,
    BacktestSummary,
    Candle,
    PivotPoint,
    PivotType,
    Position,
    PositionSide,
    Timeframe,
    TrendState,
)
from app.services.bingx_client import BingXClient
from app.services.indicators import rsi
from app.services.market_structure import classify_trend
from app.services.pivot import detect_pivots
from app.services.position_sizing import calculate_position_size, derive_liquidation_buffer_pct

HTF_PIVOT_LOOKBACK = 2
LTF_PIVOT_LOOKBACK = 2
HTF_LOOKBACK_BUFFER_DAYS = 180
SL_BUFFER_PCT = 0.001
RSI_PERIOD = 14
RSI_OVERSOLD = 30.0
RSI_OVERBOUGHT = 70.0
BOX_PROXIMITY_PCT = 0.005
TP1_CLOSE_FRACTION = 0.5
RETRACEMENT_RATIO = 0.5  # fraction of the post-pivot move retraced before entry is armed
TREND_TP_RISK_REWARD_RATIO = 2.0  # trend-trade TP = entry +/- this multiple of the SL distance

HTF_FETCH_PROGRESS_PCT = 0.0
LTF_FETCH_PROGRESS_PCT = 15.0
SIMULATION_PROGRESS_START_PCT = 30.0
SIMULATION_PROGRESS_SPAN_PCT = 70.0
DONE_PROGRESS_PCT = 100.0

Fetch1mCandles = Callable[[datetime, datetime], Awaitable[list[Candle]]]
ProgressCallback = Callable[[float, str], Awaitable[None]]


async def _report_progress(
    on_progress: ProgressCallback | None, percent: float, message: str
) -> None:
    if on_progress is not None:
        await on_progress(percent, message)


@dataclass
class _TrendWindow:
    effective_from: datetime
    trend: TrendState
    recent_sl_price: float
    recent_sh_price: float


@dataclass
class _PendingSetup:
    """An LTF pivot ("OB") confirmed in the trend direction, awaiting a 1m
    reversal-close entry once price retraces RETRACEMENT_RATIO of the way back
    from the post-pivot extreme toward the pivot price, rather than requiring
    a full round-trip back to the pivot itself."""

    side: PositionSide
    pivot_price: float
    stop_loss: float
    extreme_price: float  # most favorable price (high/low) seen since the pivot confirmed


@dataclass
class _OpenTrade:
    sequence_no: int
    side: PositionSide
    entry_price: float
    entry_time: datetime
    quantity: float
    is_box_trade: bool
    stop_loss: float
    take_profit: float | None = None
    take_profit_1: float | None = None
    take_profit_2: float | None = None
    tp1_hit: bool = False
    remaining_fraction: float = 1.0
    realized_pnl: float = 0.0


async def run_backtest(
    request: BacktestRequest,
    client: BingXClient | None = None,
    on_progress: ProgressCallback | None = None,
) -> BacktestResult:
    client = client or BingXClient()
    htf_start = request.start_date - timedelta(days=HTF_LOOKBACK_BUFFER_DAYS)

    await _report_progress(on_progress, HTF_FETCH_PROGRESS_PCT, "Fetching HTF candles...")
    htf_candles = await client.get_ohlcv(
        request.symbol, Timeframe.HTF_4H, htf_start, request.end_date
    )

    await _report_progress(on_progress, LTF_FETCH_PROGRESS_PCT, "Fetching LTF candles...")
    ltf_candles = await client.get_ohlcv(
        request.symbol, Timeframe.LTF_1H, request.start_date, request.end_date
    )

    async def fetch_1m(start: datetime, end: datetime) -> list[Candle]:
        await asyncio.sleep(1.1)  # separate request; stay under the 1 req/s rate limit
        return await client.get_ohlcv(request.symbol, Timeframe.LTF_1M, start, end)

    result = await simulate(
        request.symbol,
        htf_candles,
        ltf_candles,
        request.initial_equity,
        fetch_1m,
        on_progress=on_progress,
    )
    await _report_progress(on_progress, DONE_PROGRESS_PCT, "Done")
    return result


async def simulate(
    symbol: str,
    htf_candles: list[Candle],
    ltf_candles: list[Candle],
    initial_equity: float,
    fetch_1m: Fetch1mCandles,
    on_progress: ProgressCallback | None = None,
) -> BacktestResult:
    settings = get_settings()

    htf_pivots = detect_pivots(htf_candles, HTF_PIVOT_LOOKBACK)
    ltf_pivots = detect_pivots(ltf_candles, LTF_PIVOT_LOOKBACK)

    trend_windows = _build_htf_trend_windows(htf_pivots)
    trend_timestamps = [w.effective_from for w in trend_windows]

    rsi_values = rsi([c.close for c in ltf_candles], RSI_PERIOD)

    confirmed_at: dict[int, list[PivotPoint]] = defaultdict(list)
    for pivot in ltf_pivots:
        confirm_index = pivot.index + LTF_PIVOT_LOOKBACK
        if confirm_index < len(ltf_candles):
            confirmed_at[confirm_index].append(pivot)

    equity = initial_equity
    equity_curve = [initial_equity]
    positions: list[Position] = []
    next_sequence_no = 1
    open_trade: _OpenTrade | None = None
    pending_setup: _PendingSetup | None = None

    total_candles = len(ltf_candles)
    report_every = max(1, total_candles // 100)

    for i, candle in enumerate(ltf_candles):
        if i % report_every == 0 or i == total_candles - 1:
            percent = SIMULATION_PROGRESS_START_PCT + (i / total_candles) * (
                SIMULATION_PROGRESS_SPAN_PCT
            )
            await _report_progress(
                on_progress, percent, f"Simulating candle {i + 1}/{total_candles}"
            )

        if open_trade is not None:
            open_trade, closed = _advance_open_trade(open_trade, candle, rsi_values[i])
            if closed is not None:
                positions.append(closed)
                equity += closed.pnl or 0.0
                equity_curve.append(equity)
            continue

        window = _trend_window_at(trend_windows, trend_timestamps, candle.timestamp)
        if window is None:
            pending_setup = None
            continue

        if pending_setup is not None and not _setup_matches_trend(pending_setup, window.trend):
            pending_setup = None

        opened: _OpenTrade | None = None

        if pending_setup is None:
            if window.trend == TrendState.UPTREND:
                for pivot in confirmed_at.get(i, []):
                    if pivot.type == PivotType.SWING_LOW:
                        pending_setup = _build_pending_setup(
                            PositionSide.LONG, pivot.price, extreme_price=candle.high
                        )
                        break
            elif window.trend == TrendState.DOWNTREND:
                for pivot in confirmed_at.get(i, []):
                    if pivot.type == PivotType.SWING_HIGH:
                        pending_setup = _build_pending_setup(
                            PositionSide.SHORT, pivot.price, extreme_price=candle.low
                        )
                        break
            elif i > 0:
                opened = _try_open_box_trade_on_rsi_signal(
                    candle, rsi_values[i - 1], rsi_values[i], window, equity, settings
                )

        if pending_setup is not None:
            _update_pending_setup_extreme(pending_setup, candle)
            opened = await _try_fill_pending_setup(
                pending_setup, candle, fetch_1m, equity, settings
            )
            if opened is not None:
                pending_setup = None
            elif _pending_setup_invalidated(pending_setup, candle):
                pending_setup = None

        if opened is not None:
            opened.sequence_no = next_sequence_no
            next_sequence_no += 1
            open_trade = opened

    if open_trade is not None and ltf_candles:
        last_candle = ltf_candles[-1]
        qty = open_trade.quantity * open_trade.remaining_fraction
        pnl = _signed_pnl(open_trade.side, open_trade.entry_price, last_candle.close, qty)
        closed = _close_position(open_trade, last_candle.close, last_candle.timestamp, pnl)
        positions.append(closed)
        equity += closed.pnl or 0.0
        equity_curve.append(equity)

    summary = _build_summary(positions, equity_curve, equity)

    return BacktestResult(
        symbol=symbol,
        htf_candles=htf_candles,
        ltf_candles=ltf_candles,
        htf_pivots=htf_pivots,
        ltf_pivots=ltf_pivots,
        positions=positions,
        summary=summary,
    )


def _build_htf_trend_windows(pivots: list[PivotPoint]) -> list[_TrendWindow]:
    windows: list[_TrendWindow] = []
    for i in range(3, len(pivots)):
        window = pivots[i - 3 : i + 1]
        trend = classify_trend(window)
        recent_sl = max(
            (p for p in window if p.type == PivotType.SWING_LOW), key=lambda p: p.sequence_no
        )
        recent_sh = max(
            (p for p in window if p.type == PivotType.SWING_HIGH), key=lambda p: p.sequence_no
        )
        windows.append(
            _TrendWindow(
                effective_from=pivots[i].timestamp,
                trend=trend,
                recent_sl_price=recent_sl.price,
                recent_sh_price=recent_sh.price,
            )
        )
    return windows


def _trend_window_at(
    windows: list[_TrendWindow], timestamps: list[datetime], timestamp: datetime
) -> _TrendWindow | None:
    idx = bisect.bisect_right(timestamps, timestamp) - 1
    if idx < 0:
        return None
    return windows[idx]


def _trend_stop_loss(side: PositionSide, pivot_price: float) -> float:
    if side == PositionSide.LONG:
        return pivot_price * (1 - SL_BUFFER_PCT)
    return pivot_price * (1 + SL_BUFFER_PCT)


def _setup_matches_trend(setup: _PendingSetup, trend: TrendState) -> bool:
    if setup.side == PositionSide.LONG:
        return trend == TrendState.UPTREND
    return trend == TrendState.DOWNTREND


def _build_pending_setup(
    side: PositionSide,
    pivot_price: float,
    extreme_price: float | None = None,
) -> _PendingSetup:
    return _PendingSetup(
        side=side,
        pivot_price=pivot_price,
        stop_loss=_trend_stop_loss(side, pivot_price),
        extreme_price=extreme_price if extreme_price is not None else pivot_price,
    )


def _forced_take_profit(side: PositionSide, entry_price: float, stop_loss: float) -> float:
    risk = abs(entry_price - stop_loss)
    if side == PositionSide.LONG:
        return entry_price + TREND_TP_RISK_REWARD_RATIO * risk
    return entry_price - TREND_TP_RISK_REWARD_RATIO * risk


def _update_pending_setup_extreme(setup: _PendingSetup, candle: Candle) -> None:
    if setup.side == PositionSide.LONG:
        setup.extreme_price = max(setup.extreme_price, candle.high)
    else:
        setup.extreme_price = min(setup.extreme_price, candle.low)


def _retracement_zone_price(setup: _PendingSetup) -> float:
    """Midpoint between the pivot and the most favorable price reached since --
    i.e. the price level RETRACEMENT_RATIO of the way back toward the pivot."""
    return setup.pivot_price + RETRACEMENT_RATIO * (setup.extreme_price - setup.pivot_price)


def _pending_setup_invalidated(setup: _PendingSetup, candle: Candle) -> bool:
    if setup.side == PositionSide.LONG:
        return candle.close < setup.stop_loss
    return candle.close > setup.stop_loss


async def _try_fill_pending_setup(
    setup: _PendingSetup,
    candle: Candle,
    fetch_1m: Fetch1mCandles,
    equity: float,
    settings: Settings,
) -> _OpenTrade | None:
    zone_price = _retracement_zone_price(setup)
    touched_zone = (
        candle.low <= zone_price if setup.side == PositionSide.LONG else candle.high >= zone_price
    )
    if not touched_zone:
        return None

    one_minute_candles = await fetch_1m(candle.timestamp, candle.timestamp + timedelta(hours=1))
    entry = _find_1m_reversal_entry(setup.side, zone_price, one_minute_candles)
    if entry is None:
        return None

    entry_price, entry_time = entry
    tp_price = _forced_take_profit(setup.side, entry_price, setup.stop_loss)
    return _try_open_trend_trade(
        side=setup.side,
        entry_price=entry_price,
        pivot_price=setup.pivot_price,
        tp_price=tp_price,
        entry_time=entry_time,
        equity=equity,
        settings=settings,
    )


def _find_1m_reversal_entry(
    side: PositionSide, zone_price: float, one_minute_candles: list[Candle]
) -> tuple[float, datetime] | None:
    """First 1m candle that touches the OB zone and closes back in the trade direction."""
    for candle in one_minute_candles:
        if side == PositionSide.LONG:
            touched_zone = candle.low <= zone_price
            reversal_close = candle.close > candle.open
        else:
            touched_zone = candle.high >= zone_price
            reversal_close = candle.close < candle.open
        if touched_zone and reversal_close:
            return candle.close, candle.timestamp
    return None


def _try_open_trend_trade(
    side: PositionSide,
    entry_price: float,
    pivot_price: float,
    tp_price: float,
    entry_time: datetime,
    equity: float,
    settings: Settings,
) -> _OpenTrade | None:
    stop_loss = _trend_stop_loss(side, pivot_price)
    if side == PositionSide.LONG:
        if not (stop_loss < entry_price < tp_price):
            return None
    else:
        if not (tp_price < entry_price < stop_loss):
            return None

    sizing = calculate_position_size(
        equity=equity,
        entry_price=entry_price,
        stop_loss_price=stop_loss,
        risk_per_trade_pct=settings.risk_per_trade_pct,
        leverage=settings.leverage,
        liquidation_buffer_pct=derive_liquidation_buffer_pct(settings.leverage),
    )
    if sizing.is_liquidation_risk:
        return None

    return _OpenTrade(
        sequence_no=0,
        side=side,
        entry_price=entry_price,
        entry_time=entry_time,
        quantity=sizing.quantity,
        is_box_trade=False,
        stop_loss=stop_loss,
        take_profit=tp_price,
    )


def _try_open_box_trade_on_rsi_signal(
    candle: Candle,
    prev_rsi: float | None,
    cur_rsi: float | None,
    window: _TrendWindow,
    equity: float,
    settings: Settings,
) -> _OpenTrade | None:
    if prev_rsi is None or cur_rsi is None:
        return None

    box_bottom = window.recent_sl_price
    box_top = window.recent_sh_price

    cross_up = prev_rsi <= RSI_OVERSOLD < cur_rsi
    cross_down = prev_rsi >= RSI_OVERBOUGHT > cur_rsi
    near_bottom = candle.low <= box_bottom * (1 + BOX_PROXIMITY_PCT)
    near_top = candle.high >= box_top * (1 - BOX_PROXIMITY_PCT)

    if cross_up and near_bottom:
        return _try_open_box_trade(
            PositionSide.LONG,
            candle.close,
            box_bottom,
            box_top,
            candle.timestamp,
            equity,
            settings,
        )
    if cross_down and near_top:
        return _try_open_box_trade(
            PositionSide.SHORT,
            candle.close,
            box_bottom,
            box_top,
            candle.timestamp,
            equity,
            settings,
        )
    return None


def _try_open_box_trade(
    side: PositionSide,
    entry_price: float,
    box_bottom: float,
    box_top: float,
    entry_time: datetime,
    equity: float,
    settings: Settings,
) -> _OpenTrade | None:
    midline = (box_bottom + box_top) / 2

    if side == PositionSide.LONG:
        stop_loss = box_bottom
        if not (stop_loss < entry_price < midline < box_top):
            return None
        take_profit_1, take_profit_2 = midline, box_top
    else:
        stop_loss = box_top
        if not (box_bottom < midline < entry_price < stop_loss):
            return None
        take_profit_1, take_profit_2 = midline, box_bottom

    sizing = calculate_position_size(
        equity=equity,
        entry_price=entry_price,
        stop_loss_price=stop_loss,
        risk_per_trade_pct=settings.risk_per_trade_pct,
        leverage=settings.leverage,
        liquidation_buffer_pct=derive_liquidation_buffer_pct(settings.leverage),
    )
    if sizing.is_liquidation_risk:
        return None

    return _OpenTrade(
        sequence_no=0,
        side=side,
        entry_price=entry_price,
        entry_time=entry_time,
        quantity=sizing.quantity,
        is_box_trade=True,
        stop_loss=stop_loss,
        take_profit_1=take_profit_1,
        take_profit_2=take_profit_2,
    )


def _advance_open_trade(
    trade: _OpenTrade, candle: Candle, rsi_value: float | None
) -> tuple[_OpenTrade | None, Position | None]:
    if trade.is_box_trade:
        return _advance_box_trade(trade, candle, rsi_value)
    return _advance_trend_trade(trade, candle)


def _advance_trend_trade(
    trade: _OpenTrade, candle: Candle
) -> tuple[_OpenTrade | None, Position | None]:
    assert trade.take_profit is not None

    if trade.side == PositionSide.LONG:
        hit_sl = candle.low <= trade.stop_loss
        hit_tp = candle.high >= trade.take_profit
    else:
        hit_sl = candle.high >= trade.stop_loss
        hit_tp = candle.low <= trade.take_profit

    if hit_sl:
        exit_price = trade.stop_loss
    elif hit_tp:
        exit_price = trade.take_profit
    else:
        return trade, None

    pnl = _signed_pnl(trade.side, trade.entry_price, exit_price, trade.quantity)
    return None, _close_position(trade, exit_price, candle.timestamp, pnl)


def _advance_box_trade(
    trade: _OpenTrade, candle: Candle, rsi_value: float | None
) -> tuple[_OpenTrade | None, Position | None]:
    assert trade.take_profit_1 is not None
    assert trade.take_profit_2 is not None

    if trade.side == PositionSide.LONG:
        sl_breach = candle.close < trade.stop_loss
        tp2_price_hit = candle.high >= trade.take_profit_2
        tp2_rsi_hit = rsi_value is not None and rsi_value >= RSI_OVERBOUGHT
        tp1_price_hit = candle.high >= trade.take_profit_1
    else:
        sl_breach = candle.close > trade.stop_loss
        tp2_price_hit = candle.low <= trade.take_profit_2
        tp2_rsi_hit = rsi_value is not None and rsi_value <= RSI_OVERSOLD
        tp1_price_hit = candle.low <= trade.take_profit_1

    remaining_qty = trade.quantity * trade.remaining_fraction

    if sl_breach:
        pnl = _signed_pnl(trade.side, trade.entry_price, candle.close, remaining_qty)
        return None, _close_position(trade, candle.close, candle.timestamp, pnl)

    if tp2_price_hit or tp2_rsi_hit:
        exit_price = trade.take_profit_2 if tp2_price_hit else candle.close
        pnl = _signed_pnl(trade.side, trade.entry_price, exit_price, remaining_qty)
        return None, _close_position(trade, exit_price, candle.timestamp, pnl)

    if not trade.tp1_hit and tp1_price_hit:
        partial_qty = trade.quantity * TP1_CLOSE_FRACTION
        trade.realized_pnl += _signed_pnl(
            trade.side, trade.entry_price, trade.take_profit_1, partial_qty
        )
        trade.remaining_fraction -= TP1_CLOSE_FRACTION
        trade.tp1_hit = True
        trade.stop_loss = trade.entry_price  # move to breakeven

    return trade, None


def _signed_pnl(
    side: PositionSide, entry_price: float, exit_price: float, quantity: float
) -> float:
    direction = 1 if side == PositionSide.LONG else -1
    return (exit_price - entry_price) * quantity * direction


def _close_position(
    trade: _OpenTrade, exit_price: float, exit_time: datetime, additional_pnl: float
) -> Position:
    total_pnl = trade.realized_pnl + additional_pnl
    take_profit = trade.take_profit if trade.take_profit is not None else trade.take_profit_2
    assert take_profit is not None

    return Position(
        sequence_no=trade.sequence_no,
        side=trade.side,
        entry_price=trade.entry_price,
        stop_loss=trade.stop_loss,
        take_profit=take_profit,
        take_profit_1=trade.take_profit_1,
        quantity=trade.quantity,
        position_value=trade.entry_price * trade.quantity,
        pnl=total_pnl,
        is_win=total_pnl > 0,
        entry_time=trade.entry_time,
        exit_time=exit_time,
    )


def _build_summary(
    positions: list[Position], equity_curve: list[float], final_equity: float
) -> BacktestSummary:
    total_trades = len(positions)
    win_count = sum(1 for p in positions if p.is_win)
    loss_count = total_trades - win_count
    win_rate = win_count / total_trades if total_trades else 0.0
    total_pnl = sum(p.pnl or 0.0 for p in positions)

    return BacktestSummary(
        total_trades=total_trades,
        win_count=win_count,
        loss_count=loss_count,
        win_rate=win_rate,
        total_pnl=total_pnl,
        max_drawdown_pct=_max_drawdown_pct(equity_curve),
        final_equity=final_equity,
    )


def _max_drawdown_pct(equity_curve: list[float]) -> float:
    peak = equity_curve[0]
    max_dd = 0.0
    for equity in equity_curve:
        peak = max(peak, equity)
        if peak > 0:
            max_dd = max(max_dd, (peak - equity) / peak)
    return max_dd
