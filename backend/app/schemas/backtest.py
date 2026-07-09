from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class Timeframe(StrEnum):
    HTF_4H = "4h"
    LTF_1H = "1h"
    LTF_1M = "1m"


class PivotType(StrEnum):
    SWING_HIGH = "SH"
    SWING_LOW = "SL"


class TrendState(StrEnum):
    UPTREND = "uptrend"
    DOWNTREND = "downtrend"
    CONSOLIDATION = "consolidation"


class PositionSide(StrEnum):
    LONG = "long"
    SHORT = "short"


class Candle(BaseModel):
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


class PivotPoint(BaseModel):
    index: int
    timestamp: datetime  # the pivot candle's own timestamp
    # When this pivot becomes verifiable, i.e. the timestamp of the candle at
    # index + lookback -- a pivot can't be identified until that many candles
    # after it are seen, so anything gating a decision on "this pivot exists"
    # must wait until this time, not `timestamp` (see pivot.py / trading_logic.py).
    confirmed_timestamp: datetime
    price: float
    type: PivotType
    sequence_no: int


class Position(BaseModel):
    sequence_no: int
    side: PositionSide
    entry_price: float
    stop_loss: float
    take_profit: float
    take_profit_1: float | None = None
    quantity: float
    position_value: float
    pnl: float | None = None
    is_win: bool | None = None
    entry_time: datetime
    exit_time: datetime | None = None


class BacktestRequest(BaseModel):
    symbol: str
    start_date: datetime
    end_date: datetime
    initial_equity: float = Field(default=10_000.0, gt=0)


class BacktestSummary(BaseModel):
    total_trades: int
    win_count: int
    loss_count: int
    win_rate: float
    total_pnl: float
    max_drawdown_pct: float
    final_equity: float


class BacktestResult(BaseModel):
    symbol: str
    htf_candles: list[Candle]
    ltf_candles: list[Candle]
    htf_pivots: list[PivotPoint]
    ltf_pivots: list[PivotPoint]
    positions: list[Position]
    summary: BacktestSummary


class JobStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class BacktestJobCreated(BaseModel):
    job_id: str


class BacktestJobStatusResponse(BaseModel):
    job_id: str
    status: JobStatus
    progress: float
    message: str
    result: BacktestResult | None = None
    error: str | None = None
