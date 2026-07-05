"""Trend classification from the 4 most recent pivots (spec section 2).

Uptrend:       HL & HH over the last two SL/SH pairs.
Downtrend:     LL & LH over the last two SL/SH pairs.
Consolidation: converging (LH & HL) or expanding (HH & LL) otherwise,
               including any tie between a pivot pair (neither higher nor lower).
"""

from app.schemas.backtest import PivotPoint, PivotType, TrendState

_ALTERNATING_ORDERS = (
    (PivotType.SWING_LOW, PivotType.SWING_HIGH, PivotType.SWING_LOW, PivotType.SWING_HIGH),
    (PivotType.SWING_HIGH, PivotType.SWING_LOW, PivotType.SWING_HIGH, PivotType.SWING_LOW),
)


def classify_trend(last_four_pivots: list[PivotPoint]) -> TrendState:
    if len(last_four_pivots) != 4:
        raise ValueError("classify_trend requires exactly 4 pivots")

    types = tuple(p.type for p in last_four_pivots)
    if types not in _ALTERNATING_ORDERS:
        raise ValueError("last_four_pivots must strictly alternate SL/SH, two of each")

    sl_first, sl_second = (p.price for p in last_four_pivots if p.type == PivotType.SWING_LOW)
    sh_first, sh_second = (p.price for p in last_four_pivots if p.type == PivotType.SWING_HIGH)

    sl_up = sl_second > sl_first
    sl_down = sl_second < sl_first
    sh_up = sh_second > sh_first
    sh_down = sh_second < sh_first

    if sl_up and sh_up:
        return TrendState.UPTREND
    if sl_down and sh_down:
        return TrendState.DOWNTREND
    return TrendState.CONSOLIDATION
