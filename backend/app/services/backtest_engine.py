"""Backtest orchestration: MTF pivot detection, trend classification,
entry/SL/TP resolution (spec sections 3-4), and position sizing (spec 5).

The actual trading *decisions* (pivot/trend classification, retracement zone,
1m reversal-entry detection, SL/TP/position-size resolution, TP1 partial
close) live in `app/services/trading_logic.py` and are only re-exported here
for backwards-compatible access (`backtest_engine._build_pending_setup`,
etc.) and so `simulate()` can call them directly. `app/bot/engine.py` imports
the exact same functions for live trading, which is what guarantees the bot
trades identically to what's validated here -- see trading_logic.py's module
docstring for the full rationale and the strategy description.

Simplifications (documented, not covered by the spec text):
- HTF/LTF pivots are detected once over the full fetched history and treated
  as "known" starting `HTF_PIVOT_LOOKBACK`/`LTF_PIVOT_LOOKBACK` candles after
  they occur. The live bot (app/bot/engine.py) instead re-runs the same
  batch `detect_pivots` call over a rolling recent-candle window on every
  poll, which is simpler than an incremental tracker and produces the same
  pivots once a candle is old enough to be confirmed either way.
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
from collections import defaultdict
from collections.abc import Awaitable, Callable
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
from app.services.pivot import detect_pivots

# Trading *decisions* live in trading_logic.py (shared with app/bot/engine.py);
# re-exported here under their old private names so this module can call them
# directly and existing tests (backend/tests/test_backtest_engine.py) that
# reach into `backtest_engine._foo` keep working unchanged.
from app.services.trading_logic import (  # noqa: F401
    HTF_LOOKBACK_BUFFER_DAYS,
    HTF_PIVOT_LOOKBACK,
    LTF_PIVOT_LOOKBACK,
    RETRACEMENT_RATIO,
    RSI_PERIOD,
    SL_BUFFER_PCT,
    TP1_CLOSE_FRACTION,
    TREND_TP_HYBRID_MODE,
    TREND_TP_RISK_REWARD_RATIO,
    OpenTrade as _OpenTrade,
    PendingSetup as _PendingSetup,
    advance_box_trade as _advance_box_trade,
    advance_hybrid_trend_trade as _advance_hybrid_trend_trade,
    advance_open_trade as _advance_open_trade,
    advance_trend_trade as _advance_trend_trade,
    build_htf_trend_windows as _build_htf_trend_windows,
    build_pending_setup as _build_pending_setup,
    close_position as _close_position,
    find_1m_reversal_entry as _find_1m_reversal_entry,
    forced_take_profit as _forced_take_profit,
    pending_setup_invalidated as _pending_setup_invalidated,
    retracement_zone_price as _retracement_zone_price,
    setup_matches_trend as _setup_matches_trend,
    signed_pnl as _signed_pnl,
    trend_window_at as _trend_window_at,
    try_open_box_trade as _try_open_box_trade,
    try_open_box_trade_on_rsi_signal as _try_open_box_trade_on_rsi_signal,
    try_open_hybrid_trend_trade as _try_open_hybrid_trend_trade,
    try_open_trend_trade as _try_open_trend_trade,
    update_pending_setup_extreme as _update_pending_setup_extreme,
)

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
                            PositionSide.LONG,
                            pivot.price,
                            extreme_price=candle.high,
                            tp2_price=window.recent_sh_price,
                        )
                        break
            elif window.trend == TrendState.DOWNTREND:
                for pivot in confirmed_at.get(i, []):
                    if pivot.type == PivotType.SWING_HIGH:
                        pending_setup = _build_pending_setup(
                            PositionSide.SHORT,
                            pivot.price,
                            extreme_price=candle.low,
                            tp2_price=window.recent_sl_price,
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
    tp1_price = _forced_take_profit(setup.side, entry_price, setup.stop_loss)
    if TREND_TP_HYBRID_MODE:
        assert setup.tp2_price is not None
        return _try_open_hybrid_trend_trade(
            side=setup.side,
            entry_price=entry_price,
            pivot_price=setup.pivot_price,
            tp1_price=tp1_price,
            tp2_price=setup.tp2_price,
            entry_time=entry_time,
            equity=equity,
            settings=settings,
        )
    return _try_open_trend_trade(
        side=setup.side,
        entry_price=entry_price,
        pivot_price=setup.pivot_price,
        tp_price=tp1_price,
        entry_time=entry_time,
        equity=equity,
        settings=settings,
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
