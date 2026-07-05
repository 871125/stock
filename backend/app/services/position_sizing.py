"""Fixed-fractional position sizing (spec section 5).

risk_amount = equity * risk_per_trade_pct
sl_distance_pct = |entry - stop_loss| / entry
position_value = risk_amount / sl_distance_pct
margin_required = position_value / leverage
quantity = position_value / entry

If sl_distance_pct exceeds liquidation_buffer_pct at the configured leverage,
the trade must be skipped or leverage reduced (spec 5.3). This function only
flags that risk via `is_liquidation_risk`; the skip/reduce-leverage decision
is the caller's (backtest_engine / bot).
"""

from dataclasses import dataclass


@dataclass
class PositionSizeResult:
    risk_amount: float
    sl_distance_pct: float
    position_value: float
    margin_required: float
    quantity: float
    is_liquidation_risk: bool


def calculate_position_size(
    equity: float,
    entry_price: float,
    stop_loss_price: float,
    risk_per_trade_pct: float,
    leverage: int,
    liquidation_buffer_pct: float,
) -> PositionSizeResult:
    if equity <= 0:
        raise ValueError("equity must be > 0")
    if entry_price <= 0:
        raise ValueError("entry_price must be > 0")
    if stop_loss_price == entry_price:
        raise ValueError("stop_loss_price must differ from entry_price")
    if leverage <= 0:
        raise ValueError("leverage must be > 0")
    if risk_per_trade_pct <= 0:
        raise ValueError("risk_per_trade_pct must be > 0")

    risk_amount = equity * risk_per_trade_pct
    sl_distance_pct = abs(entry_price - stop_loss_price) / entry_price
    position_value = risk_amount / sl_distance_pct
    margin_required = position_value / leverage
    quantity = position_value / entry_price

    return PositionSizeResult(
        risk_amount=risk_amount,
        sl_distance_pct=sl_distance_pct,
        position_value=position_value,
        margin_required=margin_required,
        quantity=quantity,
        is_liquidation_risk=sl_distance_pct > liquidation_buffer_pct,
    )
