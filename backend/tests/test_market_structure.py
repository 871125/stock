from datetime import datetime, timedelta

import pytest

from app.schemas.backtest import PivotPoint, PivotType, TrendState
from app.services.market_structure import classify_trend

BASE_TIME = datetime(2024, 1, 1)


def make_pivot(seq: int, pivot_type: PivotType, price: float) -> PivotPoint:
    return PivotPoint(
        index=seq,
        timestamp=BASE_TIME + timedelta(hours=seq),
        price=price,
        type=pivot_type,
        sequence_no=seq,
    )


def pivots(*specs: tuple[PivotType, float]) -> list[PivotPoint]:
    return [make_pivot(i, t, p) for i, (t, p) in enumerate(specs)]


def test_uptrend_when_higher_lows_and_higher_highs() -> None:
    result = classify_trend(
        pivots(
            (PivotType.SWING_LOW, 10),
            (PivotType.SWING_HIGH, 20),
            (PivotType.SWING_LOW, 15),
            (PivotType.SWING_HIGH, 25),
        )
    )
    assert result == TrendState.UPTREND


def test_uptrend_also_detected_starting_with_swing_high() -> None:
    result = classify_trend(
        pivots(
            (PivotType.SWING_HIGH, 20),
            (PivotType.SWING_LOW, 10),
            (PivotType.SWING_HIGH, 25),
            (PivotType.SWING_LOW, 15),
        )
    )
    assert result == TrendState.UPTREND


def test_downtrend_when_lower_lows_and_lower_highs() -> None:
    result = classify_trend(
        pivots(
            (PivotType.SWING_LOW, 15),
            (PivotType.SWING_HIGH, 25),
            (PivotType.SWING_LOW, 10),
            (PivotType.SWING_HIGH, 20),
        )
    )
    assert result == TrendState.DOWNTREND


def test_consolidation_when_converging_higher_low_lower_high() -> None:
    result = classify_trend(
        pivots(
            (PivotType.SWING_LOW, 10),
            (PivotType.SWING_HIGH, 25),
            (PivotType.SWING_LOW, 15),
            (PivotType.SWING_HIGH, 20),
        )
    )
    assert result == TrendState.CONSOLIDATION


def test_consolidation_when_expanding_lower_low_higher_high() -> None:
    result = classify_trend(
        pivots(
            (PivotType.SWING_LOW, 15),
            (PivotType.SWING_HIGH, 20),
            (PivotType.SWING_LOW, 10),
            (PivotType.SWING_HIGH, 25),
        )
    )
    assert result == TrendState.CONSOLIDATION


def test_consolidation_on_tie() -> None:
    result = classify_trend(
        pivots(
            (PivotType.SWING_LOW, 10),
            (PivotType.SWING_HIGH, 20),
            (PivotType.SWING_LOW, 10),
            (PivotType.SWING_HIGH, 25),
        )
    )
    assert result == TrendState.CONSOLIDATION


def test_raises_when_not_exactly_four_pivots() -> None:
    with pytest.raises(ValueError):
        classify_trend(
            pivots(
                (PivotType.SWING_LOW, 10),
                (PivotType.SWING_HIGH, 20),
            )
        )


def test_raises_when_not_alternating() -> None:
    with pytest.raises(ValueError):
        classify_trend(
            pivots(
                (PivotType.SWING_LOW, 10),
                (PivotType.SWING_LOW, 12),
                (PivotType.SWING_HIGH, 20),
                (PivotType.SWING_HIGH, 22),
            )
        )
