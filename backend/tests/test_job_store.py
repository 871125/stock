from app.schemas.backtest import JobStatus
from app.services.job_store import JobStore


def test_create_assigns_unique_ids() -> None:
    store = JobStore()
    job1 = store.create()
    job2 = store.create()

    assert job1.id != job2.id
    assert job1.status == JobStatus.PENDING
    assert job1.progress == 0.0


def test_get_returns_created_job() -> None:
    store = JobStore()
    job = store.create()

    assert store.get(job.id) is job


def test_get_returns_none_for_unknown_id() -> None:
    store = JobStore()
    assert store.get("does-not-exist") is None
