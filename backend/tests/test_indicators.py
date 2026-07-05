import pytest

from app.services.indicators import rsi


def test_returns_none_until_period_is_filled() -> None:
    values = rsi([10, 12, 11, 13], period=3)
    assert values[:3] == [None, None, None]
    assert values[3] is not None


def test_all_gains_yields_rsi_100() -> None:
    values = rsi([1, 2, 3, 4, 5, 6, 7], period=3)
    for v in values[3:]:
        assert v == pytest.approx(100.0)


def test_all_losses_yields_rsi_0() -> None:
    values = rsi([7, 6, 5, 4, 3, 2, 1], period=3)
    for v in values[3:]:
        assert v == pytest.approx(0.0)


def test_matches_hand_computed_wilder_rsi() -> None:
    # Closes: 10, 12, 11, 13, 14, 12, 15 with period=3 (hand-derived via Wilder smoothing).
    closes = [10, 12, 11, 13, 14, 12, 15]
    values = rsi(closes, period=3)

    assert values[0] is None
    assert values[1] is None
    assert values[2] is None
    assert values[3] == pytest.approx(80.0)
    assert values[4] == pytest.approx(84.61538461538461)
    assert values[5] == pytest.approx(50.0)
    assert values[6] == pytest.approx(73.96449704142012)


def test_short_input_returns_all_none() -> None:
    assert rsi([1, 2, 3], period=14) == [None, None, None]
