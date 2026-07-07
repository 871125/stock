import asyncio

from fastapi import APIRouter, HTTPException

from app.schemas.backtest import (
    BacktestJobCreated,
    BacktestJobStatusResponse,
    BacktestRequest,
    BacktestResult,
    JobStatus,
)
from app.services.backtest_engine import run_backtest
from app.services.job_store import Job, job_store

router = APIRouter(prefix="/backtest", tags=["backtest"])


@router.post("", response_model=BacktestResult)
async def create_backtest(request: BacktestRequest) -> BacktestResult:
    return await run_backtest(request)


@router.post("/jobs", response_model=BacktestJobCreated)
async def create_backtest_job(request: BacktestRequest) -> BacktestJobCreated:
    job = job_store.create()
    job.task = asyncio.create_task(_run_job(job, request))
    return BacktestJobCreated(job_id=job.id)


@router.get("/jobs/{job_id}", response_model=BacktestJobStatusResponse)
async def get_backtest_job(job_id: str) -> BacktestJobStatusResponse:
    job = job_store.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")

    return BacktestJobStatusResponse(
        job_id=job.id,
        status=job.status,
        progress=job.progress,
        message=job.message,
        result=job.result,
        error=job.error,
    )


async def _run_job(job: Job, request: BacktestRequest) -> None:
    job.status = JobStatus.RUNNING

    async def on_progress(percent: float, message: str) -> None:
        job.progress = percent
        job.message = message

    try:
        job.result = await run_backtest(request, on_progress=on_progress)
        job.progress = 100.0
        job.status = JobStatus.COMPLETED
    except Exception as exc:  # surface any failure to the polling client instead of crashing
        job.status = JobStatus.FAILED
        job.error = str(exc)
