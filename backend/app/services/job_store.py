"""In-memory job store for tracking long-running backtest runs.

A single process, in-memory dict is enough for this tool (one user, one
backend instance, no need for a persistent broker like Redis/Celery). Jobs
are never evicted, but that's fine for a local dev tool -- restart the
backend to clear them.
"""

import asyncio
import uuid
from dataclasses import dataclass, field

from app.schemas.backtest import BacktestResult, JobStatus


@dataclass
class Job:
    id: str
    status: JobStatus = JobStatus.PENDING
    progress: float = 0.0
    message: str = ""
    result: BacktestResult | None = None
    error: str | None = None
    task: asyncio.Task | None = field(default=None, repr=False, compare=False)


class JobStore:
    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}

    def create(self) -> Job:
        job = Job(id=str(uuid.uuid4()))
        self._jobs[job.id] = job
        return job

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)


job_store = JobStore()
