from datetime import UTC, datetime, timedelta

import pytest

from app.core.config import Settings
from app.schemas.backtest import Candle, PivotPoint, PivotType, PositionSide, TrendState
from app.services import backtest_engine as be
from app.services.position_sizing import calculate_position_size, derive_liquidation_buffer_pct

BASE_TIME = datetime(2024, 1, 1, tzinfo=UTC)
SETTINGS = Settings(risk_per_trade_pct=0.01, leverage=10)


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
    assert trade.stop_loss == pytest.approx(95 * (1 - be.SL_BUFFER_PCT))
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


# ---- _try_open_hybrid_trend_trade / _advance_hybrid_trend_trade -----------------------------


def test_open_hybrid_trend_trade_succeeds_when_tp2_beyond_tp1() -> None:
    trade = be._try_open_hybrid_trend_trade(
        side=PositionSide.LONG,
        entry_price=100,
        pivot_price=95,
        tp1_price=110,
        tp2_price=130,
        entry_time=BASE_TIME,
        equity=10_000,
        settings=SETTINGS,
    )
    assert trade is not None
    assert trade.take_profit_1 == 110
    assert trade.take_profit_2 == 130
    assert trade.take_profit is None
    assert trade.remaining_fraction == 1.0
    assert trade.tp1_hit is False


def test_open_hybrid_trend_trade_rejects_tp2_not_beyond_tp1() -> None:
    # Opposite HTF swing (108) sits closer than the forced RR target (110) -- no room to run.
    trade = be._try_open_hybrid_trend_trade(
        side=PositionSide.LONG,
        entry_price=100,
        pivot_price=95,
        tp1_price=110,
        tp2_price=108,
        entry_time=BASE_TIME,
        equity=10_000,
        settings=SETTINGS,
    )
    assert trade is None


def test_advance_hybrid_trend_trade_partial_tp1_then_runs_to_tp2() -> None:
    trade = be._try_open_hybrid_trend_trade(
        side=PositionSide.LONG,
        entry_price=100,
        pivot_price=95,
        tp1_price=110,
        tp2_price=130,
        entry_time=BASE_TIME,
        equity=10_000,
        settings=SETTINGS,
    )
    assert trade is not None
    full_qty = trade.quantity

    # Candle touches TP1 but not TP2 -> half closes, SL moves to breakeven, trade stays open.
    trade, closed = be._advance_hybrid_trend_trade(trade, make_candle(1, 108, 112, 107, 111))
    assert closed is None
    assert trade is not None
    assert trade.tp1_hit is True
    assert trade.remaining_fraction == pytest.approx(0.5)
    assert trade.stop_loss == 100  # breakeven == entry price
    expected_tp1_pnl = (110 - 100) * (full_qty * 0.5)
    assert trade.realized_pnl == pytest.approx(expected_tp1_pnl)

    # Candle reaches TP2 -> remaining half closes at tp2.
    trade, closed = be._advance_hybrid_trend_trade(trade, make_candle(2, 125, 132, 124, 131))
    assert trade is None
    assert closed is not None
    assert closed.take_profit == 130
    expected_tp2_pnl = (130 - 100) * (full_qty * 0.5)
    assert closed.pnl == pytest.approx(expected_tp1_pnl + expected_tp2_pnl)
    assert closed.is_win is True


def test_advance_hybrid_trend_trade_sl_after_tp1_exits_at_breakeven() -> None:
    trade = be._try_open_hybrid_trend_trade(
        side=PositionSide.LONG,
        entry_price=100,
        pivot_price=95,
        tp1_price=110,
        tp2_price=130,
        entry_time=BASE_TIME,
        equity=10_000,
        settings=SETTINGS,
    )
    assert trade is not None
    full_qty = trade.quantity

    trade, _ = be._advance_hybrid_trend_trade(trade, make_candle(1, 108, 112, 107, 111))
    assert trade is not None

    # Price falls back to the (now breakeven) stop -- remaining half exits flat.
    trade, closed = be._advance_hybrid_trend_trade(trade, make_candle(2, 105, 106, 99, 100))
    assert trade is None
    assert closed is not None
    expected_tp1_pnl = (110 - 100) * (full_qty * 0.5)
    assert closed.pnl == pytest.approx(expected_tp1_pnl + 0.0)
    assert closed.is_win is True  # net still positive thanks to the secured TP1 leg


def test_advance_hybrid_trend_trade_sl_before_tp1_closes_full_at_loss() -> None:
    trade = be._try_open_hybrid_trend_trade(
        side=PositionSide.LONG,
        entry_price=100,
        pivot_price=95,
        tp1_price=110,
        tp2_price=130,
        entry_time=BASE_TIME,
        equity=10_000,
        settings=SETTINGS,
    )
    assert trade is not None

    trade, closed = be._advance_hybrid_trend_trade(trade, make_candle(1, 99, 100, 94.9, 95))
    assert trade is None
    assert closed is not None
    assert closed.is_win is False
    assert closed.exit_time == BASE_TIME + timedelta(hours=1)


# ---- pending 1m-entry setup ---------------------------------------------------------------


def test_setup_matches_trend() -> None:
    long_setup = be._build_pending_setup(PositionSide.LONG, 100)
    short_setup = be._build_pending_setup(PositionSide.SHORT, 120)

    assert be._setup_matches_trend(long_setup, TrendState.UPTREND) is True
    assert be._setup_matches_trend(long_setup, TrendState.DOWNTREND) is False
    assert be._setup_matches_trend(long_setup, TrendState.CONSOLIDATION) is False
    assert be._setup_matches_trend(short_setup, TrendState.DOWNTREND) is True
    assert be._setup_matches_trend(short_setup, TrendState.UPTREND) is False


def test_build_pending_setup_computes_stop_loss() -> None:
    long_setup = be._build_pending_setup(PositionSide.LONG, 100)
    assert long_setup.stop_loss == pytest.approx(100 * (1 - be.SL_BUFFER_PCT))

    short_setup = be._build_pending_setup(PositionSide.SHORT, 100)
    assert short_setup.stop_loss == pytest.approx(100 * (1 + be.SL_BUFFER_PCT))


def test_build_pending_setup_defaults_extreme_price_to_pivot() -> None:
    setup = be._build_pending_setup(PositionSide.LONG, 100)
    assert setup.extreme_price == 100


def test_forced_take_profit_uses_configured_risk_reward_ratio() -> None:
    long_tp = be._forced_take_profit(PositionSide.LONG, entry_price=110, stop_loss=100)
    assert long_tp == pytest.approx(110 + be.TREND_TP_RISK_REWARD_RATIO * 10)

    short_tp = be._forced_take_profit(PositionSide.SHORT, entry_price=90, stop_loss=100)
    assert short_tp == pytest.approx(90 - be.TREND_TP_RISK_REWARD_RATIO * 10)


def test_retracement_zone_price_uses_configured_ratio() -> None:
    long_setup = be._build_pending_setup(PositionSide.LONG, 100, extreme_price=110)
    assert be._retracement_zone_price(long_setup) == pytest.approx(
        100 + be.RETRACEMENT_RATIO * 10
    )

    short_setup = be._build_pending_setup(PositionSide.SHORT, 100, extreme_price=90)
    assert be._retracement_zone_price(short_setup) == pytest.approx(
        100 - be.RETRACEMENT_RATIO * 10
    )


def test_update_pending_setup_extreme_tracks_most_favorable_price_only() -> None:
    long_setup = be._build_pending_setup(PositionSide.LONG, 100, extreme_price=105)
    be._update_pending_setup_extreme(long_setup, make_candle(1, 106, 108, 104, 107))
    assert long_setup.extreme_price == 108  # new high extends it

    be._update_pending_setup_extreme(long_setup, make_candle(2, 107, 107.5, 103, 104))
    assert long_setup.extreme_price == 108  # a pullback bar doesn't shrink it

    short_setup = be._build_pending_setup(PositionSide.SHORT, 100, extreme_price=95)
    be._update_pending_setup_extreme(short_setup, make_candle(1, 94, 96, 92, 93))
    assert short_setup.extreme_price == 92  # new low extends it further down


async def test_try_fill_pending_setup_enters_on_golden_ratio_retracement() -> None:
    # pivot=100, extreme reached=110 -> 61.8% retracement zone = 106.18. Price only pulls back
    # to 104 (nowhere near the original pivot at 100) and the entry should still fill there.
    setup = be._build_pending_setup(PositionSide.LONG, pivot_price=100, extreme_price=110)
    candle = make_candle(1, 108, 109, 104, 106)

    reversal_time = candle.timestamp + timedelta(minutes=1)
    one_minute_candles = [
        Candle(
            timestamp=reversal_time, open=104.2, high=105.5, low=104.0, close=105.2, volume=1
        ),
    ]

    async def fetch_1m(start, end) -> list[Candle]:
        return one_minute_candles

    opened = await be._try_fill_pending_setup(setup, candle, fetch_1m, 10_000, SETTINGS)

    assert opened is not None
    assert opened.entry_price == 105.2
    assert opened.entry_time == reversal_time


async def test_try_fill_pending_setup_does_not_touch_zone_before_golden_ratio_retracement() -> None:
    # pivot=100, extreme=120 -> 61.8% retracement zone = 112.36. Price only pulls back to
    # 113 -- short of the zone.
    setup = be._build_pending_setup(PositionSide.LONG, pivot_price=100, extreme_price=120)
    candle = make_candle(1, 115, 116, 113, 114)

    calls: list[tuple] = []

    async def fetch_1m(start, end) -> list[Candle]:
        calls.append((start, end))
        return []

    opened = await be._try_fill_pending_setup(setup, candle, fetch_1m, 10_000, SETTINGS)

    assert opened is None
    assert calls == []


def test_pending_setup_invalidated_on_body_close_past_stop_loss() -> None:
    long_setup = be._build_pending_setup(PositionSide.LONG, 100)

    still_valid = make_candle(1, 101, 102, 99, 100.5)
    assert be._pending_setup_invalidated(long_setup, still_valid) is False

    invalidated = make_candle(2, 100, 101, 95, 98)
    assert be._pending_setup_invalidated(long_setup, invalidated) is True


def test_find_1m_reversal_entry_long_skips_non_reversal_candles() -> None:
    candles = [
        make_candle(0, 101.0, 101.2, 99.5, 100.5),  # touches zone (100) but bearish close
        make_candle(1, 102.0, 103.0, 101.5, 102.8),  # bullish close but never touches zone
        make_candle(2, 99.5, 100.9, 99.4, 100.8),  # touches zone (100) and closes bullish
    ]

    entry = be._find_1m_reversal_entry(PositionSide.LONG, 100, candles)

    assert entry == (100.8, candles[2].timestamp)


def test_find_1m_reversal_entry_short_requires_bearish_close_at_zone() -> None:
    candles = [
        make_candle(0, 99.5, 100.5, 99.0, 100.2),  # touches zone but bullish close
        make_candle(1, 100.3, 100.8, 100.1, 99.9),  # bearish close, touches zone (100)
    ]

    entry = be._find_1m_reversal_entry(PositionSide.SHORT, 100, candles)

    assert entry == (99.9, candles[1].timestamp)


def test_find_1m_reversal_entry_returns_none_when_no_candle_qualifies() -> None:
    candles = [make_candle(0, 105, 106, 104, 105.5)]  # never touches zone at 100
    assert be._find_1m_reversal_entry(PositionSide.LONG, 100, candles) is None


async def test_try_fill_pending_setup_skips_fetch_when_zone_not_touched() -> None:
    setup = be._build_pending_setup(PositionSide.LONG, 100)
    candle = make_candle(1, 105, 106, 103, 104)  # low=103, never reaches zone (100)

    calls: list[tuple] = []

    async def fetch_1m(start, end) -> list[Candle]:
        calls.append((start, end))
        return []

    opened = await be._try_fill_pending_setup(setup, candle, fetch_1m, 10_000, SETTINGS)

    assert opened is None
    assert calls == []


async def test_try_fill_pending_setup_enters_on_1m_reversal() -> None:
    setup = be._build_pending_setup(PositionSide.LONG, 100)
    candle = make_candle(1, 102, 103, 99, 101)  # wicks into the zone

    reversal_time = candle.timestamp + timedelta(minutes=1)
    one_minute_candles = [
        Candle(timestamp=candle.timestamp, open=100.2, high=100.3, low=99.6, close=99.8, volume=1),
        Candle(timestamp=reversal_time, open=99.8, high=100.9, low=99.7, close=100.7, volume=1),
    ]

    async def fetch_1m(start, end) -> list[Candle]:
        assert start == candle.timestamp
        assert end == candle.timestamp + timedelta(hours=1)
        return one_minute_candles

    opened = await be._try_fill_pending_setup(setup, candle, fetch_1m, 10_000, SETTINGS)

    expected_stop_loss = 100 * (1 - be.SL_BUFFER_PCT)
    expected_take_profit = 100.7 + be.TREND_TP_RISK_REWARD_RATIO * (100.7 - expected_stop_loss)

    assert opened is not None
    assert opened.entry_price == 100.7
    assert opened.entry_time == reversal_time
    assert opened.stop_loss == pytest.approx(expected_stop_loss)
    assert opened.take_profit == pytest.approx(expected_take_profit)


async def test_try_fill_pending_setup_returns_none_when_no_reversal_in_window() -> None:
    setup = be._build_pending_setup(PositionSide.LONG, 100)
    candle = make_candle(1, 102, 103, 99, 101)

    async def fetch_1m(start, end) -> list[Candle]:
        return [
            Candle(
                timestamp=candle.timestamp,
                open=100.5,
                high=100.6,
                low=99.5,
                close=99.9,
                volume=1,
            )
        ]

    opened = await be._try_fill_pending_setup(setup, candle, fetch_1m, 10_000, SETTINGS)

    assert opened is None


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
    # Starts after the HTF uptrend is confirmed (hour 76). Forms one LTF swing low ("OB")
    # at hour 79 (confirmed at hour 81 with LTF_PIVOT_LOOKBACK=2). The post-pivot extreme
    # climbs to 112 (hour 82's high), so the 50% retracement zone sits at
    # 103 + 0.5*(112-103) = 107.5 -- hour 82's low (107) already reaches it, which is
    # where the 1m reversal-close entry fires (see the fetch_1m stub in the test), well
    # before price would have fully round-tripped back to the pivot itself (103, touched at
    # hour 83). Price then rallies to the HTF take-profit target (134) by hour 86.
    rows = [
        (77, 109, 111, 109, 110),
        (78, 106, 108, 106, 107),
        (79, 103, 105, 103, 104),  # swing low candidate, low=103
        (80, 105, 107, 105, 106),
        (81, 108, 110, 108, 109.5),  # confirmation bar; extreme=110, zone=106.5, not touched
        (82, 109, 112, 107, 111),  # extreme updates to 112, zone=107.5 -> touched (low=107)
        (83, 110, 111, 103, 110.5),
        (84, 112, 120, 111, 119),  # high >= 118.506 -> take profit
        (85, 120, 128, 119, 127),
        (86, 128, 136, 127, 134),
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


async def test_simulate_full_uptrend_long_trade_hits_take_profit() -> None:
    htf_candles = _uptrend_htf_candles()
    ltf_candles = _uptrend_ltf_candles()
    initial_equity = 10_000.0

    zone_touch_candle = ltf_candles[5]  # hour 82, where the 50% zone (107.5) is reached
    assert zone_touch_candle.timestamp == BASE_TIME + timedelta(hours=82)
    reversal_time = zone_touch_candle.timestamp + timedelta(minutes=1)
    one_minute_candles = [
        # First minute: touches the zone but closes bearish -> not yet a reversal.
        Candle(
            timestamp=zone_touch_candle.timestamp,
            open=108.0,
            high=108.2,
            low=107.3,
            close=107.7,
            volume=1.0,
        ),
        # Second minute: touches the zone and closes bullish -> reversal entry.
        Candle(
            timestamp=reversal_time,
            open=107.7,
            high=108.5,
            low=107.4,
            close=108.1,
            volume=1.0,
        ),
    ]

    fetch_calls: list[tuple] = []

    async def fetch_1m(start, end) -> list[Candle]:
        fetch_calls.append((start, end))
        return one_minute_candles

    result = await be.simulate("BTC-USDT", htf_candles, ltf_candles, initial_equity, fetch_1m)

    # fetch_1m is only called once: when the 50% retracement zone is first reached (hour 82).
    assert fetch_calls == [
        (zone_touch_candle.timestamp, zone_touch_candle.timestamp + timedelta(hours=1))
    ]

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

    entry_price = 108.1
    stop_loss = 103 * (1 - be.SL_BUFFER_PCT)
    # TP is forced to TREND_TP_RISK_REWARD_RATIO (2:1) times the SL distance from entry,
    # not the HTF swing high (134) -- so it's reached sooner, at hour 84 instead of 86.
    take_profit = entry_price + be.TREND_TP_RISK_REWARD_RATIO * (entry_price - stop_loss)
    sizing = calculate_position_size(
        equity=initial_equity,
        entry_price=entry_price,
        stop_loss_price=stop_loss,
        risk_per_trade_pct=SETTINGS.risk_per_trade_pct,
        leverage=SETTINGS.leverage,
        liquidation_buffer_pct=derive_liquidation_buffer_pct(SETTINGS.leverage),
    )
    expected_pnl = (take_profit - entry_price) * sizing.quantity

    assert position.side == PositionSide.LONG
    assert position.sequence_no == 1
    assert position.entry_price == entry_price
    assert position.entry_time == reversal_time
    assert position.stop_loss == pytest.approx(stop_loss)
    assert position.take_profit == pytest.approx(take_profit)
    assert position.quantity == pytest.approx(sizing.quantity)
    assert position.pnl == pytest.approx(expected_pnl)
    assert position.is_win is True
    assert position.exit_time == BASE_TIME + timedelta(hours=84)

    assert result.summary.total_trades == 1
    assert result.summary.win_count == 1
    assert result.summary.final_equity == pytest.approx(initial_equity + expected_pnl)


async def test_simulate_hybrid_mode_partial_tp_then_runs_to_htf_swing(monkeypatch) -> None:
    monkeypatch.setattr(be, "TREND_TP_HYBRID_MODE", True)

    htf_candles = _uptrend_htf_candles()
    ltf_candles = _uptrend_ltf_candles()
    initial_equity = 10_000.0

    zone_touch_candle = ltf_candles[5]  # hour 82
    reversal_time = zone_touch_candle.timestamp + timedelta(minutes=1)
    one_minute_candles = [
        Candle(
            timestamp=zone_touch_candle.timestamp,
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

    async def fetch_1m(start, end) -> list[Candle]:
        return one_minute_candles

    result = await be.simulate("BTC-USDT", htf_candles, ltf_candles, initial_equity, fetch_1m)

    assert len(result.positions) == 1
    position = result.positions[0]

    entry_price = 108.1
    stop_loss = 103 * (1 - be.SL_BUFFER_PCT)
    take_profit_1 = entry_price + be.TREND_TP_RISK_REWARD_RATIO * (entry_price - stop_loss)
    take_profit_2 = 134.0  # the HTF window's recent_sh_price -- the pre-RR-forcing TP target

    sizing = calculate_position_size(
        equity=initial_equity,
        entry_price=entry_price,
        stop_loss_price=stop_loss,
        risk_per_trade_pct=SETTINGS.risk_per_trade_pct,
        leverage=SETTINGS.leverage,
        liquidation_buffer_pct=derive_liquidation_buffer_pct(SETTINGS.leverage),
    )
    leg1_pnl = (take_profit_1 - entry_price) * (sizing.quantity * be.TP1_CLOSE_FRACTION)
    leg2_pnl = (take_profit_2 - entry_price) * (sizing.quantity * (1 - be.TP1_CLOSE_FRACTION))

    assert position.entry_price == entry_price
    assert position.take_profit_1 == pytest.approx(take_profit_1)
    assert position.take_profit == pytest.approx(take_profit_2)
    assert position.pnl == pytest.approx(leg1_pnl + leg2_pnl)
    assert position.is_win is True
    # TP1 fires at hour 84 (same as the RR=2-only test); TP2 (134) isn't reached until hour 86.
    assert position.exit_time == BASE_TIME + timedelta(hours=86)


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


# ---- progress reporting ---------------------------------------------------------------------


async def test_run_backtest_reports_fetch_phases_and_completion() -> None:
    from app.schemas.backtest import BacktestRequest

    request = BacktestRequest(
        symbol="BTC-USDT",
        start_date=BASE_TIME,
        end_date=BASE_TIME + timedelta(days=30),
    )
    fake_client = _FakeBingXClient()

    reports: list[tuple] = []

    async def on_progress(percent, message):
        reports.append((percent, message))

    await be.run_backtest(request, client=fake_client, on_progress=on_progress)

    assert reports[0] == (be.HTF_FETCH_PROGRESS_PCT, "Fetching HTF candles...")
    assert reports[1] == (be.LTF_FETCH_PROGRESS_PCT, "Fetching LTF candles...")
    assert reports[-1] == (be.DONE_PROGRESS_PCT, "Done")
    # Percentages must never decrease.
    assert [p for p, _ in reports] == sorted(p for p, _ in reports)


async def test_simulate_reports_monotonically_increasing_progress_during_the_loop() -> None:
    htf_candles = _uptrend_htf_candles()
    ltf_candles = _uptrend_ltf_candles()

    async def fetch_1m(start, end) -> list[Candle]:
        return []

    reports: list[float] = []

    async def on_progress(percent, message):
        reports.append(percent)

    await be.simulate("BTC-USDT", htf_candles, ltf_candles, 10_000.0, fetch_1m, on_progress)

    assert len(reports) > 0
    assert reports == sorted(reports)
    assert all(be.SIMULATION_PROGRESS_START_PCT <= p <= 100 for p in reports)


async def test_simulate_without_on_progress_does_not_raise() -> None:
    htf_candles = _uptrend_htf_candles()
    ltf_candles = _uptrend_ltf_candles()

    async def fetch_1m(start, end) -> list[Candle]:
        return []

    result = await be.simulate("BTC-USDT", htf_candles, ltf_candles, 10_000.0, fetch_1m)

    assert result.symbol == "BTC-USDT"
