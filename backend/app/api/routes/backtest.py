from fastapi import APIRouter

from app.schemas.backtest import BacktestRequest, BacktestResult
from app.services.backtest_engine import run_backtest

router = APIRouter(prefix="/backtest", tags=["backtest"])


@router.post("", response_model=BacktestResult)
async def create_backtest(request: BacktestRequest) -> BacktestResult:
    return await run_backtest(request)
