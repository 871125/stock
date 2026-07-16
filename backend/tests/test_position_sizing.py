import pytest

from app.services.position_sizing import calculate_position_size, derive_liquidation_buffer_pct

DEFAULT_KWARGS = dict(
    risk_per_trade_pct=0.01,
    leverage=10,
    liquidation_buffer_pct=0.09,
)


def test_long_position_sizing_matches_spec_formula() -> None:
    result = calculate_position_size(
        equity=10_000,
        entry_price=100,
        stop_loss_price=95,
        **DEFAULT_KWARGS,
    )

    assert result.risk_amount == pytest.approx(100)
    assert result.sl_distance_pct == pytest.approx(0.05)
    assert result.position_value == pytest.approx(2000)
    assert result.margin_required == pytest.approx(200)
    assert result.quantity == pytest.approx(20)
    assert result.is_liquidation_risk is False
    assert result.is_margin_insufficient is False


def test_flags_margin_insufficient_when_required_margin_exceeds_available_equity() -> None:
    # A very tight stop (0.05%) blows up position_value/margin_required even
    # though risk_amount itself stays a modest 1% of equity -- this is the
    # BingX 110424 "order size must be less than the available amount" case.
    result = calculate_position_size(
        equity=10_000,
        entry_price=100,
        stop_loss_price=99.95,
        **DEFAULT_KWARGS,
    )

    assert result.margin_required > 10_000
    assert result.is_margin_insufficient is True


def test_no_margin_insufficient_when_required_margin_fits_safety_buffer() -> None:
    result = calculate_position_size(
        equity=10_000,
        entry_price=100,
        stop_loss_price=95,
        **DEFAULT_KWARGS,
    )

    assert result.margin_required == pytest.approx(200)
    assert result.is_margin_insufficient is False


def test_short_position_uses_absolute_sl_distance() -> None:
    result = calculate_position_size(
        equity=10_000,
        entry_price=100,
        stop_loss_price=105,
        **DEFAULT_KWARGS,
    )

    assert result.sl_distance_pct == pytest.approx(0.05)
    assert result.position_value == pytest.approx(2000)
    assert result.quantity == pytest.approx(20)


def test_flags_liquidation_risk_when_sl_distance_exceeds_buffer() -> None:
    result = calculate_position_size(
        equity=10_000,
        entry_price=100,
        stop_loss_price=89,
        **DEFAULT_KWARGS,
    )

    assert result.sl_distance_pct == pytest.approx(0.11)
    assert result.is_liquidation_risk is True


def test_no_liquidation_risk_at_exact_buffer_boundary() -> None:
    result = calculate_position_size(
        equity=10_000,
        entry_price=100,
        stop_loss_price=91,
        **DEFAULT_KWARGS,
    )

    assert result.sl_distance_pct == pytest.approx(0.09)
    assert result.is_liquidation_risk is False


@pytest.mark.parametrize(
    "overrides",
    [
        {"equity": 0},
        {"equity": -1},
        {"entry_price": 0},
        {"entry_price": -100},
        {"stop_loss_price": 100},  # equals entry_price
        {"leverage": 0},
        {"risk_per_trade_pct": 0},
    ],
)
def test_raises_on_invalid_inputs(overrides: dict) -> None:
    kwargs = dict(
        equity=10_000,
        entry_price=100,
        stop_loss_price=95,
        **DEFAULT_KWARGS,
    )
    kwargs.update(overrides)

    with pytest.raises(ValueError):
        calculate_position_size(**kwargs)


def test_derive_liquidation_buffer_pct_matches_spec_default_at_10x() -> None:
    # Spec 5.3: 10x leverage -> ~10% liquidation move, 9% buffer used in practice.
    assert derive_liquidation_buffer_pct(10) == pytest.approx(0.09)


def test_derive_liquidation_buffer_pct_scales_with_leverage() -> None:
    assert derive_liquidation_buffer_pct(20) == pytest.approx(0.045)
    assert derive_liquidation_buffer_pct(5) == pytest.approx(0.18)


def test_derive_liquidation_buffer_pct_rejects_non_positive_leverage() -> None:
    with pytest.raises(ValueError):
        derive_liquidation_buffer_pct(0)
    with pytest.raises(ValueError):
        derive_liquidation_buffer_pct(-10)
