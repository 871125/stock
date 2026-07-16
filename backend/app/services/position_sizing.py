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

`liquidation_buffer_pct` should be derived from leverage (see
`derive_liquidation_buffer_pct`), not set independently -- otherwise raising
leverage no longer tightens the liquidation check the way spec 5.3 intends.

The fixed-fractional formula sizes off risk_amount (equity * risk_pct) and
sl_distance_pct alone -- a tight stop against a large equity can imply
margin_required beyond `equity` itself, `equity` is never fed back in as a
ceiling. In live trading `equity` is the account's *available* balance, so
this isn't hypothetical: it's exactly what produced BingX error 110424
("order size must be less than the available amount") when a pivot-based
stop landed close to entry. `is_margin_insufficient` flags that case the same
way `is_liquidation_risk` does, for callers to skip the trade instead of
letting the exchange reject the order.
"""

from dataclasses import dataclass

LIQUIDATION_SAFETY_FACTOR = 0.9
# Leaves headroom below the live-queried available balance for fees/slippage/
# price drift between sizing and order placement, so we skip trades that
# would land right on the exchange's rejection boundary instead of bouncing
# off it and retrying blind on the next poll.
MARGIN_SAFETY_FACTOR = 0.95


def derive_liquidation_buffer_pct(leverage: int) -> float:
    """Approximate liquidation move at this leverage is 1/leverage; leave a
    safety margin below it (spec 5.3's 9% at 10x is exactly 0.9 * 1/10)."""
    if leverage <= 0:
        raise ValueError("leverage must be > 0")
    return (1 / leverage) * LIQUIDATION_SAFETY_FACTOR


@dataclass
class PositionSizeResult:
    risk_amount: float
    sl_distance_pct: float
    position_value: float
    margin_required: float
    quantity: float
    is_liquidation_risk: bool
    is_margin_insufficient: bool


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
        is_margin_insufficient=margin_required > equity * MARGIN_SAFETY_FACTOR,
    )
