"""Swing High / Swing Low pivot detection (spec section 1).

Rules implemented:
- A candle at index i is a raw SH/SL candidate when its high/low is strictly
  more extreme than every other candle within `lookback` bars on both sides
  (1.1). Ties do not qualify, since neither side is strictly more extreme.
- Raw candidates are folded into a strictly alternating SL -> SH -> SL -> SH
  sequence. A same-side candidate appearing before the opposite type is
  confirmed either replaces the pending pivot (if more extreme) or is
  discarded (1.2).
- A single candle can raise both an SH and an SL candidate (outside bar).
  Intra-candle ordering follows the candle's direction: bullish -> SL then
  SH, bearish -> SH then SL (1.3).
"""

from app.schemas.backtest import Candle, PivotPoint, PivotType


def _is_bullish(candle: Candle) -> bool:
    return candle.close >= candle.open


def _raw_candidates(candles: list[Candle], lookback: int) -> list[list[PivotType]]:
    n = len(candles)
    candidates: list[list[PivotType]] = [[] for _ in range(n)]

    for i in range(lookback, n - lookback):
        candle = candles[i]
        neighbors = candles[i - lookback : i] + candles[i + 1 : i + lookback + 1]

        is_sh = all(candle.high > other.high for other in neighbors)
        is_sl = all(candle.low < other.low for other in neighbors)

        if is_sh and is_sl:
            candidates[i] = (
                [PivotType.SWING_LOW, PivotType.SWING_HIGH]
                if _is_bullish(candle)
                else [PivotType.SWING_HIGH, PivotType.SWING_LOW]
            )
        elif is_sh:
            candidates[i] = [PivotType.SWING_HIGH]
        elif is_sl:
            candidates[i] = [PivotType.SWING_LOW]

    return candidates


def detect_pivots(candles: list[Candle], lookback: int) -> list[PivotPoint]:
    if lookback < 1:
        raise ValueError("lookback must be >= 1")

    confirmed: list[PivotPoint] = []
    sequence_no = 0

    for index, types in enumerate(_raw_candidates(candles, lookback)):
        candle = candles[index]

        for pivot_type in types:
            price = candle.high if pivot_type == PivotType.SWING_HIGH else candle.low

            if confirmed and confirmed[-1].type == pivot_type:
                current = confirmed[-1]
                is_more_extreme = (
                    price > current.price
                    if pivot_type == PivotType.SWING_HIGH
                    else price < current.price
                )
                if is_more_extreme:
                    confirmed[-1] = PivotPoint(
                        index=index,
                        timestamp=candle.timestamp,
                        price=price,
                        type=pivot_type,
                        sequence_no=current.sequence_no,
                    )
                continue

            sequence_no += 1
            confirmed.append(
                PivotPoint(
                    index=index,
                    timestamp=candle.timestamp,
                    price=price,
                    type=pivot_type,
                    sequence_no=sequence_no,
                )
            )

    return confirmed
