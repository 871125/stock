from datetime import UTC, datetime, timedelta

import pytest

from app.core.config import Settings
from app.schemas.backtest import Candle, PivotPoint, PivotType, PositionSide, TrendState
from app.services import backtest_engine as be
from app.services.position_sizing import calculate_position_size

BASE_TIME = datetime(2024, 1, 1, tzinfo=UTC)
SETTINGS = Settings(risk_per_trade_pct=0.01, leverage=10, liquidation_buffer_pct=0.09)


def make_candle(
    hours: int, o: float, h: float, low: float, c: float, volume: float = 1.0
) -> Candle:
    return Candle(
        timestamp=BASE_TIME + timedelta(hours=hours),
        open=o,
        high=h,
        low=low,
        close=c,
        volume=volume,
    )


def make_pivot(seq: int, hours: int, pivot_type: PivotType, price: float) -> PivotPoint:
    return PivotPoint(
        index=hours,
        timestamp=BASE_TIME + timedelta(hours=hours),
        price=price,
        type=pivot_type,
        sequence_no=seq,
    )


# ---- _build_htf_trend_windows / _trend_window_at ----------------------------------------


def test_build_trend_windows_and_lookup_uptrend() -> None:
    pivots = [
        make_pivot(1, 0, PivotType.SWING_LOW, 100),
        make_pivot(2, 4, PivotType.SWING_HIGH, 120),
        make_pivot(3, 8, PivotType.SWING_LOW, 110),
        make_pivot(4, 12, PivotType.SWING_HIGH, 130),
    ]

    windows = be._build_htf_trend_windows(pivots)
    assert len(windows) == 1
    assert windows[0].trend == TrendState.UPTREND
    assert windows[0].recent_sl_price == 110
    assert windows[0].recent_sh_price == 130
    assert windows[0].effective_from == pivots[3].timestamp

    timestamps = [w.effective_from for w in windows]

    before = be._trend_window_at(windows, timestamps, BASE_TIME)
    assert before is None

    at_or_after = be._trend_window_at(windows, timestamps, pivots[3].timestamp + timedelta(hours=1))
    assert at_or_after is windows[0]


def test_trend_window_lookup_with_no_windows_returns_none() -> None:
    assert be._trend_window_at([], [], BASE_TIME) is None


# ---- _try_open_trend_trade ---------------------------------------------------------------


def test_open_long_trend_trade_succeeds() -> None:
    trade = be._try_open_trend_trade(
        side=PositionSide.LONG,
        entry_price=100,
        pivot_price=95,
        tp_price=120,
        entry_time=BASE_TIME,
        equity=10_000,
        settings=SETTINGS,
    )
    assert trade is not None
    assert trade.side == PositionSide.LONG
    assert trade.stop_loss == pytest.approx(95 * 0.999)
    assert trade.take_profit == 120
    assert trade.quantity > 0


def test_open_trend_trade_rejects_invalid_tp_below_entry() -> None:
    trade = be._try_open_trend_trade(
        side=PositionSide.LONG,
        entry_price=100,
        pivot_price=95,
        tp_price=98,  # TP below entry is nonsensical for a long
        entry_time=BASE_TIME,
        equity=10_000,
        settings=SETTINGS,
    )
    assert trade is None


def test_open_trend_trade_skipped_on_liquidation_risk() -> None:
    # SL distance ~15% > 9% liquidation buffer at 10x leverage.
    trade = be._try_open_trend_trade(
        side=PositionSide.LONG,
        entry_price=100,
        pivot_price=85,
        tp_price=130,
        entry_time=BASE_TIME,
        equity=10_000,
        settings=SETTINGS,
    )
    assert trade is None


# ---- _advance_trend_trade -----------------------------------------------------------------


def test_advance_long_trend_trade_hits_take_profit() -> None:
    trade = be._OpenTrade(
        sequence_no=1,
        side=PositionSide.LONG,
        entry_price=100,
        entry_time=BASE_TIME,
        quantity=10,
        is_box_trade=False,
        stop_loss=90,
        take_profit=120,
    )
    candle = make_candle(1, 110, 125, 108, 122)

    updated, closed = be._advance_trend_trade(trade, candle)

    assert updated is None
    assert closed is not None
    assert closed.pnl == pytest.approx((120 - 100) * 10)
    assert closed.is_win is True
    assert closed.exit_time == candle.timestamp


def test_advance_long_trend_trade_hits_stop_loss() -> None:
    trade = be._OpenTrade(
        sequence_no=1,
        side=PositionSide.LONG,
        entry_price=100,
        entry_time=BASE_TIME,
        quantity=10,
        is_box_trade=False,
        stop_loss=90,
        take_profit=120,
    )
    candle = make_candle(1, 95, 96, 85, 90)

    updated, closed = be._advance_trend_trade(trade, candle)

    assert updated is None
    assert closed is not None
    assert closed.pnl == pytest.approx((90 - 100) * 10)
    assert closed.is_win is False


def test_advance_long_trend_trade_prefers_stop_loss_when_both_touched_same_bar() -> None:
    trade = be._OpenTrade(
        sequence_no=1,
        side=PositionSide.LONG,
        entry_price=100,
        entry_time=BASE_TIME,
        quantity=10,
        is_box_trade=False,
        stop_loss=90,
        take_profit=120,
    )
    candle = make_candle(1, 100, 125, 85, 100)  # wick touches both SL and TP

    _, closed = be._advance_trend_trade(trade, candle)

    assert closed is not None
    assert closed.pnl == pytest.approx((90 - 100) * 10)


def test_advance_short_trend_trade_hits_take_profit() -> None:
    trade = be._OpenTrade(
        sequence_no=1,
        side=PositionSide.SHORT,
        entry_price=100,
        entry_time=BASE_TIME,
        quantity=10,
        is_box_trade=False,
        stop_loss=110,
        take_profit=80,
    )
    candle = make_candle(1, 90, 92, 78, 80)

    _, closed = be._advance_trend_trade(trade, candle)

    assert closed is not None
    assert closed.pnl == pytest.approx((100 - 80) * 10)
    assert closed.is_win is True


def test_advance_trend_trade_keeps_position_open_when_neither_hit() -> None:
    trade = be._OpenTrade(
        sequence_no=1,
        side=PositionSide.LONG,
        entry_price=100,
        entry_time=BASE_TIME,
        quantity=10,
        is_box_trade=False,
        stop_loss=90,
        take_profit=120,
    )
    candle = make_candle(1, 101, 105, 99, 102)

    updated, closed = be._advance_trend_trade(trade, candle)

    assert closed is None
    assert updated is trade


# ---- _try_open_box_trade / _advance_box_trade ----------------------------------------------


def test_open_long_box_trade_computes_levels() -> None:
    trade = be._try_open_box_trade(
        side=PositionSide.LONG,
        entry_price=101,
        box_bottom=100,
        box_top=120,
        entry_time=BASE_TIME,
        equity=10_000,
        settings=SETTINGS,
    )
    assert trade is not None
    assert trade.stop_loss == 100
    assert trade.take_profit_1 == 110
    assert trade.take_profit_2 == 120


def test_box_trade_tp1_then_tp2_full_cycle() -> None:
    trade = be._try_open_box_trade(
        side=PositionSide.LONG,
        entry_price=101,
        box_bottom=100,
        box_top=120,
        entry_time=BASE_TIME,
        equity=10_000,
        settings=SETTINGS,
    )
    assert trade is not None
    quantity = trade.quantity

    # Bar 1: reaches midline (110) only -> partial close, breakeven SL.
    bar1 = make_candle(1, 105, 111, 104, 109)
    updated, closed = be._advance_box_trade(trade, bar1, rsi_value=55.0)
    assert closed is None
    assert updated is not None
    assert updated.tp1_hit is True
    assert updated.stop_loss == 101
    assert updated.remaining_fraction == pytest.approx(0.5)

    # Bar 2: reaches box top (120) -> full close of the remainder.
    bar2 = make_candle(2, 115, 121, 114, 119)
    updated2, closed2 = be._advance_box_trade(updated, bar2, rsi_value=75.0)
    assert updated2 is None
    assert closed2 is not None

    expected_pnl = (110 - 101) * quantity * 0.5 + (120 - 101) * quantity * 0.5
    assert closed2.pnl == pytest.approx(expected_pnl)
    assert closed2.is_win is True


def test_box_trade_stop_loss_triggers_on_body_close_not_wick() -> None:
    trade = be._try_open_box_trade(
        side=PositionSide.LONG,
        entry_price=101,
        box_bottom=100,
        box_top=120,
        entry_time=BASE_TIME,
        equity=10_000,
        settings=SETTINGS,
    )
    assert trade is not None

    # Wick dips below the box bottom but closes back inside -> no SL trigger.
    wick_only = make_candle(1, 102, 103, 97, 101.5)
    updated, closed = be._advance_box_trade(trade, wick_only, rsi_value=45.0)
    assert closed is None
    assert updated is trade

    # Candle body closes below the box bottom -> SL triggers.
    body_close = make_candle(2, 101, 102, 95, 96)
    updated2, closed2 = be._advance_box_trade(updated, body_close, rsi_value=40.0)
    assert updated2 is None
    assert closed2 is not None
    assert closed2.is_win is False


def test_box_trade_tp2_triggers_via_rsi_extreme_without_reaching_price_boundary() -> None:
    trade = be._try_open_box_trade(
        side=PositionSide.LONG,
        entry_price=101,
        box_bottom=100,
        box_top=120,
        entry_time=BASE_TIME,
        equity=10_000,
        settings=SETTINGS,
    )
    assert trade is not None

    # Price stays well below the box top, but RSI reaches overbought -> full close.
    bar = make_candle(1, 105, 108, 104, 107)
    updated, closed = be._advance_box_trade(trade, bar, rsi_value=71.0)

    assert updated is None
    assert closed is not None
    assert closed.pnl == pytest.approx((107 - 101) * trade.quantity)


def test_open_box_trade_skipped_on_liquidation_risk() -> None:
    trade = be._try_open_box_trade(
        side=PositionSide.LONG,
        entry_price=100,
        box_bottom=85,  # ~15% away, exceeds the 9% liquidation buffer
        box_top=130,
        entry_time=BASE_TIME,
        equity=10_000,
        settings=SETTINGS,
    )
    assert trade is None


# ---- _max_drawdown_pct ---------------------------------------------------------------------


def test_max_drawdown_pct_tracks_peak_to_trough() -> None:
    curve = [1000, 1200, 900, 1100, 800, 1500]
    dd = be._max_drawdown_pct(curve)
    assert dd == pytest.approx((1200 - 800) / 1200)


def test_max_drawdown_pct_zero_when_always_rising() -> None:
    assert be._max_drawdown_pct([1000, 1100, 1200]) == 0.0


# ---- simulate() end-to-end ------------------------------------------------------------------


def _uptrend_htf_candles() -> list[Candle]:
    # 4h levels forming a clean HL/HH zigzag: SL=100, SH=125, SL=105 (HL), SH=133 (HH).
    # Verified by hand: strict local extrema over a +/-2 candle window (HTF_PIVOT_LOOKBACK).
    levels = [
        110, 108, 105, 102, 100, 103, 107, 112, 118, 125, 120,
        115, 111, 108, 105, 108, 112, 118, 125, 133, 128, 124,
    ]
    return [
        Candle(
            timestamp=BASE_TIME + timedelta(hours=4 * i),
            open=level,
            high=level + 1,
            low=level - 1,
            close=level,
            volume=1.0,
        )
        for i, level in enumerate(levels)
    ]


def _uptrend_ltf_candles() -> list[Candle]:
    # Starts after the HTF uptrend is confirmed (hour 76). Forms one LTF swing low at
    # hour 79 (confirmed at hour 81 with LTF_PIVOT_LOOKBACK=2), then rallies to the HTF
    # take-profit target (133) by hour 85.
    rows = [
        (77, 109, 111, 109, 110),
        (78, 106, 108, 106, 107),
        (79, 103, 105, 103, 104),  # swing low candidate, low=103
        (80, 105, 107, 105, 106),
        (81, 108, 110, 108, 109.5),  # confirmation bar -> entry at close
        (82, 109, 115, 108, 114),
        (83, 114, 122, 113, 121),
        (84, 122, 130, 120, 129),
        (85, 130, 135, 128, 134),  # high >= 133 -> take profit
    ]
    return [
        Candle(
            timestamp=BASE_TIME + timedelta(hours=h),
            open=o,
            high=hi,
            low=lo,
            close=c,
            volume=1.0,
        )
        for h, o, hi, lo, c in rows
    ]


def test_simulate_full_uptrend_long_trade_hits_take_profit() -> None:
    htf_candles = _uptrend_htf_candles()
    ltf_candles = _uptrend_ltf_candles()
    initial_equity = 10_000.0

    result = be.simulate("BTC-USDT", htf_candles, ltf_candles, initial_equity)

    # Pivot prices are the candle's actual high/low, one above/below the zigzag "level".
    assert [(p.type, p.price) for p in result.htf_pivots] == [
        (PivotType.SWING_LOW, 99),
        (PivotType.SWING_HIGH, 126),
        (PivotType.SWING_LOW, 104),
        (PivotType.SWING_HIGH, 134),
    ]
    assert [(p.type, p.price) for p in result.ltf_pivots] == [(PivotType.SWING_LOW, 103)]

    assert len(result.positions) == 1
    position = result.positions[0]

    entry_price = 109.5
    stop_loss = 103 * 0.999
    take_profit = 134  # most recent confirmed HTF swing high
    sizing = calculate_position_size(
        equity=initial_equity,
        entry_price=entry_price,
        stop_loss_price=stop_loss,
        risk_per_trade_pct=SETTINGS.risk_per_trade_pct,
        leverage=SETTINGS.leverage,
        liquidation_buffer_pct=SETTINGS.liquidation_buffer_pct,
    )
    expected_pnl = (take_profit - entry_price) * sizing.quantity

    assert position.side == PositionSide.LONG
    assert position.sequence_no == 1
    assert position.entry_price == entry_price
    assert position.stop_loss == pytest.approx(stop_loss)
    assert position.take_profit == take_profit
    assert position.quantity == pytest.approx(sizing.quantity)
    assert position.pnl == pytest.approx(expected_pnl)
    assert position.is_win is True
    assert position.exit_time == BASE_TIME + timedelta(hours=85)

    assert result.summary.total_trades == 1
    assert result.summary.win_count == 1
    assert result.summary.final_equity == pytest.approx(initial_equity + expected_pnl)


# ---- run_backtest wiring -------------------------------------------------------------------


class _FakeBingXClient:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def get_ohlcv(self, symbol, timeframe, start, end) -> list[Candle]:
        self.calls.append((symbol, timeframe, start, end))
        return []


async def test_run_backtest_fetches_htf_with_buffer_and_ltf_with_exact_range() -> None:
    from app.schemas.backtest import BacktestRequest, Timeframe

    request = BacktestRequest(
        symbol="BTC-USDT",
        start_date=BASE_TIME,
        end_date=BASE_TIME + timedelta(days=30),
        initial_equity=5_000,
    )
    fake_client = _FakeBingXClient()

    result = await be.run_backtest(request, client=fake_client)

    assert len(fake_client.calls) == 2
    htf_call, ltf_call = fake_client.calls
    assert htf_call == (
        "BTC-USDT",
        Timeframe.HTF_4H,
        BASE_TIME - timedelta(days=be.HTF_LOOKBACK_BUFFER_DAYS),
        request.end_date,
    )
    assert ltf_call == ("BTC-USDT", Timeframe.LTF_1H, request.start_date, request.end_date)

    assert result.symbol == "BTC-USDT"
    assert result.positions == []
    assert result.summary.total_trades == 0
    assert result.summary.final_equity == 5_000
