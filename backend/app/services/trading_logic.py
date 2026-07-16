"""Pure trading decision logic shared by the backtester and the live bot.

This module has no knowledge of *how* candles were obtained or how a trade
decision gets carried out (simulated bookkeeping vs. a real exchange order) --
every function here is deterministic given its inputs. `backtest_engine.py`
and `app/bot/engine.py` both import from here so that a decision (entry zone,
SL/TP, position size, TP1 partial-close, invalidation) is computed by the
exact same code in both places. This is the mechanism that guarantees the
live bot trades identically to what the backtest already validated -- there
is no separate "bot version" of this logic to drift out of sync.

Trend-trade entries are refined with a 1-minute timing layer: once the LTF
(1h) pivot ("OB") that would normally trigger an immediate entry is
confirmed, price is tracked for the most favorable price reached since (the
post-pivot extreme), and entry is armed once price retraces RETRACEMENT_RATIO
(50%, a simple half retracement) of the way back toward the pivot -- not a
full round-trip to the pivot itself -- then the first 1-minute candle inside
that hour with a reversal-close confirms the actual entry.

Trend-trade TP is a forced TREND_TP_RISK_REWARD_RATIO (2:1) multiple of the
SL distance from the 1m entry price, not the nearest opposite HTF swing --
the HTF swing is frequently too far away to realistically reach, so pinning
TP to a fixed multiple of risk raises the odds of actually hitting it. SL is
unaffected -- still derived from the 1h pivot exactly as before.

TREND_TP_HYBRID_MODE (default off, experimental) closes TP1_CLOSE_FRACTION of
the position at the forced RR target (as above) and lets the rest ride to the
opposite HTF swing that was active when the pending setup formed -- the TP
this logic used before RR forcing was introduced. SL moves to breakeven once
TP1 fills. A setup is skipped if the HTF swing doesn't sit beyond the RR
target in the trade direction (no room for a runner leg).

See docs/backtest_results.md for the live-data experiments behind every
constant below.
"""

import bisect
from dataclasses import dataclass
from datetime import datetime

from app.core.config import Settings
from app.schemas.backtest import Candle, PivotPoint, PivotType, Position, PositionSide, TrendState
from app.services.market_structure import classify_trend
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
TREND_TP_HYBRID_MODE = False  # experimental: partial TP1 at forced RR, runner to opposite HTF swing


@dataclass
class TrendWindow:
    effective_from: datetime
    trend: TrendState
    recent_sl_price: float
    recent_sh_price: float


@dataclass
class PendingSetup:
    """An LTF pivot ("OB") confirmed in the trend direction, awaiting a 1m
    reversal-close entry once price retraces RETRACEMENT_RATIO of the way back
    from the post-pivot extreme toward the pivot price, rather than requiring
    a full round-trip back to the pivot itself."""

    side: PositionSide
    pivot_price: float
    stop_loss: float
    extreme_price: float  # most favorable price (high/low) seen since the pivot confirmed
    tp2_price: float | None = None  # opposite HTF swing at setup time; only used in hybrid mode


@dataclass
class OpenTrade:
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


def build_htf_trend_windows(pivots: list[PivotPoint]) -> list[TrendWindow]:
    windows: list[TrendWindow] = []
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
            TrendWindow(
                # Use confirmed_timestamp, not timestamp -- the trend can't be
                # known until pivots[i] itself is confirmable (lookback candles
                # after its own candle), otherwise this window would claim to be
                # "effective" before the data that justifies it even exists yet.
                effective_from=pivots[i].confirmed_timestamp,
                trend=trend,
                recent_sl_price=recent_sl.price,
                recent_sh_price=recent_sh.price,
            )
        )
    return windows


def trend_window_at(
    windows: list[TrendWindow], timestamps: list[datetime], timestamp: datetime
) -> TrendWindow | None:
    idx = bisect.bisect_right(timestamps, timestamp) - 1
    if idx < 0:
        return None
    return windows[idx]


def trend_stop_loss(side: PositionSide, pivot_price: float) -> float:
    if side == PositionSide.LONG:
        return pivot_price * (1 - SL_BUFFER_PCT)
    return pivot_price * (1 + SL_BUFFER_PCT)


def setup_matches_trend(setup: PendingSetup, trend: TrendState) -> bool:
    if setup.side == PositionSide.LONG:
        return trend == TrendState.UPTREND
    return trend == TrendState.DOWNTREND


def build_pending_setup(
    side: PositionSide,
    pivot_price: float,
    extreme_price: float | None = None,
    tp2_price: float | None = None,
) -> PendingSetup:
    return PendingSetup(
        side=side,
        pivot_price=pivot_price,
        stop_loss=trend_stop_loss(side, pivot_price),
        extreme_price=extreme_price if extreme_price is not None else pivot_price,
        tp2_price=tp2_price,
    )


def forced_take_profit(side: PositionSide, entry_price: float, stop_loss: float) -> float:
    risk = abs(entry_price - stop_loss)
    if side == PositionSide.LONG:
        return entry_price + TREND_TP_RISK_REWARD_RATIO * risk
    return entry_price - TREND_TP_RISK_REWARD_RATIO * risk


def update_pending_setup_extreme(setup: PendingSetup, candle: Candle) -> None:
    if setup.side == PositionSide.LONG:
        setup.extreme_price = max(setup.extreme_price, candle.high)
    else:
        setup.extreme_price = min(setup.extreme_price, candle.low)


def retracement_zone_price(setup: PendingSetup) -> float:
    """Midpoint between the pivot and the most favorable price reached since --
    i.e. the price level RETRACEMENT_RATIO of the way back toward the pivot."""
    return setup.pivot_price + RETRACEMENT_RATIO * (setup.extreme_price - setup.pivot_price)


def pending_setup_invalidated(setup: PendingSetup, candle: Candle) -> bool:
    if setup.side == PositionSide.LONG:
        return candle.close < setup.stop_loss
    return candle.close > setup.stop_loss


def find_1m_reversal_entry(
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


def try_open_trend_trade(
    side: PositionSide,
    entry_price: float,
    pivot_price: float,
    tp_price: float,
    entry_time: datetime,
    equity: float,
    settings: Settings,
) -> OpenTrade | None:
    stop_loss = trend_stop_loss(side, pivot_price)
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
    if sizing.is_liquidation_risk or sizing.is_margin_insufficient:
        return None

    return OpenTrade(
        sequence_no=0,
        side=side,
        entry_price=entry_price,
        entry_time=entry_time,
        quantity=sizing.quantity,
        is_box_trade=False,
        stop_loss=stop_loss,
        take_profit=tp_price,
    )


def try_open_hybrid_trend_trade(
    side: PositionSide,
    entry_price: float,
    pivot_price: float,
    tp1_price: float,
    tp2_price: float,
    entry_time: datetime,
    equity: float,
    settings: Settings,
) -> OpenTrade | None:
    """Like try_open_trend_trade, but TP1_CLOSE_FRACTION exits at the forced RR
    target (tp1_price) and the remainder rides to the opposite HTF swing
    (tp2_price). Rejected if tp2 doesn't sit beyond tp1 -- no room for a runner."""
    stop_loss = trend_stop_loss(side, pivot_price)
    if side == PositionSide.LONG:
        if not (stop_loss < entry_price < tp1_price < tp2_price):
            return None
    else:
        if not (tp2_price < tp1_price < entry_price < stop_loss):
            return None

    sizing = calculate_position_size(
        equity=equity,
        entry_price=entry_price,
        stop_loss_price=stop_loss,
        risk_per_trade_pct=settings.risk_per_trade_pct,
        leverage=settings.leverage,
        liquidation_buffer_pct=derive_liquidation_buffer_pct(settings.leverage),
    )
    if sizing.is_liquidation_risk or sizing.is_margin_insufficient:
        return None

    return OpenTrade(
        sequence_no=0,
        side=side,
        entry_price=entry_price,
        entry_time=entry_time,
        quantity=sizing.quantity,
        is_box_trade=False,
        stop_loss=stop_loss,
        take_profit_1=tp1_price,
        take_profit_2=tp2_price,
    )


def try_open_box_trade_on_rsi_signal(
    candle: Candle,
    prev_rsi: float | None,
    cur_rsi: float | None,
    window: TrendWindow,
    equity: float,
    settings: Settings,
) -> OpenTrade | None:
    if prev_rsi is None or cur_rsi is None:
        return None

    box_bottom = window.recent_sl_price
    box_top = window.recent_sh_price

    cross_up = prev_rsi <= RSI_OVERSOLD < cur_rsi
    cross_down = prev_rsi >= RSI_OVERBOUGHT > cur_rsi
    near_bottom = candle.low <= box_bottom * (1 + BOX_PROXIMITY_PCT)
    near_top = candle.high >= box_top * (1 - BOX_PROXIMITY_PCT)

    if cross_up and near_bottom:
        return try_open_box_trade(
            PositionSide.LONG,
            candle.close,
            box_bottom,
            box_top,
            candle.timestamp,
            equity,
            settings,
        )
    if cross_down and near_top:
        return try_open_box_trade(
            PositionSide.SHORT,
            candle.close,
            box_bottom,
            box_top,
            candle.timestamp,
            equity,
            settings,
        )
    return None


def try_open_box_trade(
    side: PositionSide,
    entry_price: float,
    box_bottom: float,
    box_top: float,
    entry_time: datetime,
    equity: float,
    settings: Settings,
) -> OpenTrade | None:
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
    if sizing.is_liquidation_risk or sizing.is_margin_insufficient:
        return None

    return OpenTrade(
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


def advance_open_trade(
    trade: OpenTrade, candle: Candle, rsi_value: float | None
) -> tuple[OpenTrade | None, Position | None]:
    if trade.is_box_trade:
        return advance_box_trade(trade, candle, rsi_value)
    if trade.take_profit_1 is not None:
        return advance_hybrid_trend_trade(trade, candle)
    return advance_trend_trade(trade, candle)


def advance_trend_trade(
    trade: OpenTrade, candle: Candle
) -> tuple[OpenTrade | None, Position | None]:
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

    pnl = signed_pnl(trade.side, trade.entry_price, exit_price, trade.quantity)
    return None, close_position(trade, exit_price, candle.timestamp, pnl)


def advance_box_trade(
    trade: OpenTrade, candle: Candle, rsi_value: float | None
) -> tuple[OpenTrade | None, Position | None]:
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
        pnl = signed_pnl(trade.side, trade.entry_price, candle.close, remaining_qty)
        return None, close_position(trade, candle.close, candle.timestamp, pnl)

    if tp2_price_hit or tp2_rsi_hit:
        exit_price = trade.take_profit_2 if tp2_price_hit else candle.close
        pnl = signed_pnl(trade.side, trade.entry_price, exit_price, remaining_qty)
        return None, close_position(trade, exit_price, candle.timestamp, pnl)

    if not trade.tp1_hit and tp1_price_hit:
        partial_qty = trade.quantity * TP1_CLOSE_FRACTION
        trade.realized_pnl += signed_pnl(
            trade.side, trade.entry_price, trade.take_profit_1, partial_qty
        )
        trade.remaining_fraction -= TP1_CLOSE_FRACTION
        trade.tp1_hit = True
        trade.stop_loss = trade.entry_price  # move to breakeven

    return trade, None


def advance_hybrid_trend_trade(
    trade: OpenTrade, candle: Candle
) -> tuple[OpenTrade | None, Position | None]:
    assert trade.take_profit_1 is not None
    assert trade.take_profit_2 is not None

    if trade.side == PositionSide.LONG:
        sl_hit = candle.low <= trade.stop_loss
        tp2_hit = candle.high >= trade.take_profit_2
        tp1_hit_now = candle.high >= trade.take_profit_1
    else:
        sl_hit = candle.high >= trade.stop_loss
        tp2_hit = candle.low <= trade.take_profit_2
        tp1_hit_now = candle.low <= trade.take_profit_1

    remaining_qty = trade.quantity * trade.remaining_fraction

    if sl_hit:
        pnl = signed_pnl(trade.side, trade.entry_price, trade.stop_loss, remaining_qty)
        return None, close_position(trade, trade.stop_loss, candle.timestamp, pnl)

    if tp2_hit:
        pnl = signed_pnl(trade.side, trade.entry_price, trade.take_profit_2, remaining_qty)
        return None, close_position(trade, trade.take_profit_2, candle.timestamp, pnl)

    if not trade.tp1_hit and tp1_hit_now:
        partial_qty = trade.quantity * TP1_CLOSE_FRACTION
        trade.realized_pnl += signed_pnl(
            trade.side, trade.entry_price, trade.take_profit_1, partial_qty
        )
        trade.remaining_fraction -= TP1_CLOSE_FRACTION
        trade.tp1_hit = True
        trade.stop_loss = trade.entry_price  # move to breakeven after securing partial profit

    return trade, None


def signed_pnl(side: PositionSide, entry_price: float, exit_price: float, quantity: float) -> float:
    direction = 1 if side == PositionSide.LONG else -1
    return (exit_price - entry_price) * quantity * direction


def close_position(
    trade: OpenTrade, exit_price: float, exit_time: datetime, additional_pnl: float
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
