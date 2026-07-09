from datetime import datetime, timedelta

from app.schemas.backtest import Candle, PivotType
from app.services.pivot import detect_pivots

BASE_TIME = datetime(2024, 1, 1)


def make_candles(ohlc: list[tuple[float, float, float, float]]) -> list[Candle]:
    return [
        Candle(
            timestamp=BASE_TIME + timedelta(hours=i),
            open=o,
            high=h,
            low=lo,
            close=c,
            volume=1.0,
        )
        for i, (o, h, lo, c) in enumerate(ohlc)
    ]


def test_basic_alternating_pivots() -> None:
    candles = make_candles(
        [
            (10, 10, 9, 9.5),  # 0 edge
            (10, 11, 9, 10.5),  # 1
            (11, 14, 10, 13.5),  # 2 SH
            (13, 12, 8, 8.5),  # 3 SL
            (9, 13, 9, 12.5),  # 4
            (12, 17, 10, 16.5),  # 5 SH
            (16, 15, 9, 9.5),  # 6 SL
            (9, 11, 11, 10.5),  # 7
            (10, 10, 9, 9.5),  # 8 edge
        ]
    )

    pivots = detect_pivots(candles, lookback=1)

    assert [(p.type, p.index, p.price) for p in pivots] == [
        (PivotType.SWING_HIGH, 2, 14),
        (PivotType.SWING_LOW, 3, 8),
        (PivotType.SWING_HIGH, 5, 17),
        (PivotType.SWING_LOW, 6, 9),
    ]
    assert [p.sequence_no for p in pivots] == [1, 2, 3, 4]


def test_confirmed_timestamp_is_lookback_candles_after_the_pivot() -> None:
    """A pivot at index i can't be identified until `lookback` candles after
    it are seen -- confirmed_timestamp must reflect that, not the pivot
    candle's own (earlier) timestamp, otherwise anything gating a decision on
    "this pivot exists" would be using data from before it existed."""
    candles = make_candles(
        [
            (10, 10, 9, 9.5),  # 0 edge
            (10, 11, 9, 10.5),  # 1
            (11, 14, 10, 13.5),  # 2 SH
            (13, 12, 8, 8.5),  # 3 SL
            (9, 13, 9, 12.5),  # 4
            (12, 17, 10, 16.5),  # 5 SH
            (16, 15, 9, 9.5),  # 6 SL
            (9, 11, 11, 10.5),  # 7
            (10, 10, 9, 9.5),  # 8 edge
        ]
    )

    pivots = detect_pivots(candles, lookback=1)

    assert [p.timestamp for p in pivots] == [
        BASE_TIME + timedelta(hours=2),
        BASE_TIME + timedelta(hours=3),
        BASE_TIME + timedelta(hours=5),
        BASE_TIME + timedelta(hours=6),
    ]
    # confirmed_timestamp is exactly `lookback` (1) hour after each pivot's own candle.
    assert [p.confirmed_timestamp for p in pivots] == [
        BASE_TIME + timedelta(hours=3),
        BASE_TIME + timedelta(hours=4),
        BASE_TIME + timedelta(hours=6),
        BASE_TIME + timedelta(hours=7),
    ]


def test_confirmed_timestamp_recomputed_when_pivot_updates_to_more_extreme() -> None:
    candles = make_candles(
        [
            (9, 8, 7, 7.5),  # 0 edge
            (9, 10, 8, 9.5),  # 1
            (9, 14, 9, 13.5),  # 2 SH candidate (14), lower than pivot at 4
            (11, 12, 9.5, 11.5),  # 3 neutral
            (12, 18, 9.5, 17.5),  # 4 SH candidate (18), replaces pivot 2
            (11, 11, 8, 8.5),  # 5 edge
        ]
    )

    pivots = detect_pivots(candles, lookback=1)

    assert len(pivots) == 1
    assert pivots[0].index == 4
    # Recomputed against the *new* (index 4) pivot, not left over from index 2.
    assert pivots[0].confirmed_timestamp == BASE_TIME + timedelta(hours=5)


def test_consecutive_same_side_updates_to_more_extreme() -> None:
    candles = make_candles(
        [
            (9, 8, 7, 7.5),  # 0 edge
            (9, 10, 8, 9.5),  # 1
            (9, 18, 9, 17.5),  # 2 SH candidate (18)
            (11, 12, 9.5, 11.5),  # 3 neutral
            (12, 14, 9.5, 13.5),  # 4 SH candidate (14), same side as pivot 2
            (11, 11, 8, 8.5),  # 5 edge
        ]
    )

    pivots = detect_pivots(candles, lookback=1)

    assert len(pivots) == 1
    assert pivots[0].type == PivotType.SWING_HIGH
    assert pivots[0].price == 18
    assert pivots[0].index == 2


def test_consecutive_same_side_ignores_less_extreme() -> None:
    candles = make_candles(
        [
            (9, 8, 7, 7.5),  # 0 edge
            (9, 10, 8, 9.5),  # 1
            (9, 14, 9, 13.5),  # 2 SH candidate (14), lower than pivot at 4
            (11, 12, 9.5, 11.5),  # 3 neutral
            (12, 18, 9.5, 17.5),  # 4 SH candidate (18), replaces pivot 2
            (11, 11, 8, 8.5),  # 5 edge
        ]
    )

    pivots = detect_pivots(candles, lookback=1)

    assert len(pivots) == 1
    assert pivots[0].price == 18
    assert pivots[0].index == 4


def test_outside_bar_bullish_orders_sl_then_sh() -> None:
    candles = make_candles(
        [
            (10, 10, 9, 9.5),  # 0 edge
            (10, 11, 8, 10.5),  # 1
            (9, 20, 5, 13),  # 2 outside bar, bullish (close > open)
            (10, 12, 9, 9.5),  # 3
            (9, 10, 8, 8.5),  # 4 edge
        ]
    )

    pivots = detect_pivots(candles, lookback=1)

    assert [(p.type, p.price, p.index) for p in pivots] == [
        (PivotType.SWING_LOW, 5, 2),
        (PivotType.SWING_HIGH, 20, 2),
    ]
    assert [p.sequence_no for p in pivots] == [1, 2]


def test_outside_bar_bearish_orders_sh_then_sl() -> None:
    candles = make_candles(
        [
            (10, 10, 9, 9.5),  # 0 edge
            (10, 11, 8, 10.5),  # 1
            (13, 20, 5, 9),  # 2 outside bar, bearish (close < open)
            (10, 12, 9, 9.5),  # 3
            (9, 10, 8, 8.5),  # 4 edge
        ]
    )

    pivots = detect_pivots(candles, lookback=1)

    assert [(p.type, p.price, p.index) for p in pivots] == [
        (PivotType.SWING_HIGH, 20, 2),
        (PivotType.SWING_LOW, 5, 2),
    ]
    assert [p.sequence_no for p in pivots] == [1, 2]
