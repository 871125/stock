"""Backtest orchestration: MTF pivot detection, trend classification,
entry/SL/TP resolution (spec sections 3-4), and position sizing (spec 5).

Simplifications (documented, not covered by the spec text):
- HTF pivots are detected once over the full fetched history and treated as
  "known" starting `HTF_PIVOT_LOOKBACK` candles after they occur (same for
  LTF). A live bot would track unconfirmed pivots incrementally (spec 6.2);
  this backtester recomputes over the full range instead.
- At most one position is open at a time.
- Trend-trade SL/TP are resolved against candle wicks (high/low); box-trade
  SL is resolved against the candle body close, per spec 4.3.
- On a bar where both SL and TP are touched, SL is assumed to trigger first
  (conservative).
- If a position is still open at the end of the requested range, it is
  force-closed at the last candle's close so it's reflected in the summary.
"""

import bisect
from collections import defaultdict
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
from app.services.position_sizing import calculate_position_size

HTF_PIVOT_LOOKBACK = 2
LTF_PIVOT_LOOKBACK = 2
HTF_LOOKBACK_BUFFER_DAYS = 180
SL_BUFFER_PCT = 0.001
RSI_PERIOD = 14
RSI_OVERSOLD = 30.0
RSI_OVERBOUGHT = 70.0
BOX_PROXIMITY_PCT = 0.005
TP1_CLOSE_FRACTION = 0.5


@dataclass
class _TrendWindow:
    effective_from: datetime
    trend: TrendState
    recent_sl_price: float
    recent_sh_price: float


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
    request: BacktestRequest, client: BingXClient | None = None
) -> BacktestResult:
    client = client or BingXClient()
    htf_start = request.start_date - timedelta(days=HTF_LOOKBACK_BUFFER_DAYS)

    htf_candles = await client.get_ohlcv(
        request.symbol, Timeframe.HTF_4H, htf_start, request.end_date
    )
    ltf_candles = await client.get_ohlcv(
        request.symbol, Timeframe.LTF_1H, request.start_date, request.end_date
    )

    return simulate(request.symbol, htf_candles, ltf_candles, request.initial_equity)


def simulate(
    symbol: str,
    htf_candles: list[Candle],
    ltf_candles: list[Candle],
    initial_equity: float,
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

    for i, candle in enumerate(ltf_candles):
        if open_trade is not None:
            open_trade, closed = _advance_open_trade(open_trade, candle, rsi_values[i])
            if closed is not None:
                positions.append(closed)
                equity += closed.pnl or 0.0
                equity_curve.append(equity)
            continue

        window = _trend_window_at(trend_windows, trend_timestamps, candle.timestamp)
        if window is None:
            continue

        opened: _OpenTrade | None = None

        if window.trend == TrendState.UPTREND:
            for pivot in confirmed_at.get(i, []):
                if pivot.type == PivotType.SWING_LOW:
                    opened = _try_open_trend_trade(
                        side=PositionSide.LONG,
                        entry_price=candle.close,
                        pivot_price=pivot.price,
                        tp_price=window.recent_sh_price,
                        entry_time=candle.timestamp,
                        equity=equity,
                        settings=settings,
                    )
                    break
        elif window.trend == TrendState.DOWNTREND:
            for pivot in confirmed_at.get(i, []):
                if pivot.type == PivotType.SWING_HIGH:
                    opened = _try_open_trend_trade(
                        side=PositionSide.SHORT,
                        entry_price=candle.close,
                        pivot_price=pivot.price,
                        tp_price=window.recent_sl_price,
                        entry_time=candle.timestamp,
                        equity=equity,
                        settings=settings,
                    )
                    break
        elif i > 0:
            opened = _try_open_box_trade_on_rsi_signal(
                candle, rsi_values[i - 1], rsi_values[i], window, equity, settings
            )

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


def _try_open_trend_trade(
    side: PositionSide,
    entry_price: float,
    pivot_price: float,
    tp_price: float,
    entry_time: datetime,
    equity: float,
    settings: Settings,
) -> _OpenTrade | None:
    if side == PositionSide.LONG:
        stop_loss = pivot_price * (1 - SL_BUFFER_PCT)
        if not (stop_loss < entry_price < tp_price):
            return None
    else:
        stop_loss = pivot_price * (1 + SL_BUFFER_PCT)
        if not (tp_price < entry_price < stop_loss):
            return None

    sizing = calculate_position_size(
        equity=equity,
        entry_price=entry_price,
        stop_loss_price=stop_loss,
        risk_per_trade_pct=settings.risk_per_trade_pct,
        leverage=settings.leverage,
        liquidation_buffer_pct=settings.liquidation_buffer_pct,
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
        liquidation_buffer_pct=settings.liquidation_buffer_pct,
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
