import httpx
import pytest
from httpx import ASGITransport

import app.api.routes.backtest as backtest_routes
from app.main import app
from app.schemas.backtest import BacktestResult, BacktestSummary
from app.services.job_store import job_store

_REQUEST_BODY = {"symbol": "BTC-USDT", "start_date": "2024-06-01", "end_date": "2024-06-04"}


async def _fake_run_backtest(request, client=None, on_progress=None) -> BacktestResult:
    if on_progress is not None:
        await on_progress(50.0, "halfway")
    return BacktestResult(
        symbol=request.symbol,
        htf_candles=[],
        ltf_candles=[],
        htf_pivots=[],
        ltf_pivots=[],
        positions=[],
        summary=BacktestSummary(
            total_trades=0,
            win_count=0,
            loss_count=0,
            win_rate=0.0,
            total_pnl=0.0,
            max_drawdown_pct=0.0,
            final_equity=request.initial_equity,
        ),
    )


async def test_backtest_job_completes_and_returns_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(backtest_routes, "run_backtest", _fake_run_backtest)

    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        create_resp = await client.post("/backtest/jobs", json=_REQUEST_BODY)
        assert create_resp.status_code == 200
        job_id = create_resp.json()["job_id"]

        job = job_store.get(job_id)
        assert job is not None
        await job.task  # deterministically wait for the background run to finish

        status_resp = await client.get(f"/backtest/jobs/{job_id}")

    assert status_resp.status_code == 200
    body = status_resp.json()
    assert body["status"] == "completed"
    assert body["progress"] == 100.0
    assert body["result"]["symbol"] == "BTC-USDT"


async def test_backtest_job_not_found_returns_404() -> None:
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/backtest/jobs/does-not-exist")

    assert resp.status_code == 404


async def test_backtest_job_reports_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    async def failing_run_backtest(request, client=None, on_progress=None):
        raise RuntimeError("boom")

    monkeypatch.setattr(backtest_routes, "run_backtest", failing_run_backtest)

    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        create_resp = await client.post("/backtest/jobs", json=_REQUEST_BODY)
        job_id = create_resp.json()["job_id"]

        job = job_store.get(job_id)
        assert job is not None
        await job.task

        status_resp = await client.get(f"/backtest/jobs/{job_id}")

    body = status_resp.json()
    assert body["status"] == "failed"
    assert body["error"] == "boom"
