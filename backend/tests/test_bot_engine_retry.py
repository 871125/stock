"""_poll_once_with_retry should absorb transient BingX errors (e.g. code
109500 "quote service unavailable") and network-level hiccups (httpx
TransportError) with a few quick retries instead of immediately bubbling up
to the run() loop's Telegram notification, while still surfacing
non-transient errors (and exhausted transient ones) as-is.
"""

from datetime import datetime

import httpx
import pytest

from app.bot import engine
from app.services.bingx_client import BingXAPIError


class _NullNotifier:
    async def send(self, message: str) -> None:
        pass


async def _poll_once_stub_factory(monkeypatch, outcomes):
    """Patches engine._poll_once to pop from `outcomes` each call: either an
    exception instance to raise, or None to succeed."""
    calls = {"count": 0}

    async def fake_poll_once(state, symbol, settings, market_client, trade_client, notifier, cache):
        calls["count"] += 1
        outcome = outcomes.pop(0)
        if outcome is not None:
            raise outcome

    monkeypatch.setattr(engine, "_poll_once", fake_poll_once)
    return calls


async def _no_sleep(monkeypatch):
    sleeps: list[float] = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(engine.asyncio, "sleep", fake_sleep)
    return sleeps


async def test_transient_error_recovers_after_retries(monkeypatch):
    outcomes = [BingXAPIError(109500, "quote service unavailable"), None]
    calls = await _poll_once_stub_factory(monkeypatch, outcomes)
    sleeps = await _no_sleep(monkeypatch)

    await engine._poll_once_with_retry(
        None, "BTC-USDT", None, None, None, _NullNotifier(), engine._MarketDataCache()
    )

    assert calls["count"] == 2
    assert sleeps == [2.0]  # one retry backoff, no notification needed


async def test_transient_error_raises_once_retries_exhausted(monkeypatch):
    outcomes = [BingXAPIError(109500, "quote service unavailable")] * (
        engine._TRANSIENT_RETRY_ATTEMPTS + 1
    )
    calls = await _poll_once_stub_factory(monkeypatch, outcomes)
    await _no_sleep(monkeypatch)

    with pytest.raises(BingXAPIError):
        await engine._poll_once_with_retry(
            None, "BTC-USDT", None, None, None, _NullNotifier(), engine._MarketDataCache()
        )

    assert calls["count"] == engine._TRANSIENT_RETRY_ATTEMPTS + 1


async def test_rate_limit_error_recovers_after_retries(monkeypatch):
    outcomes = [BingXAPIError(100410, "rate limited"), None]
    calls = await _poll_once_stub_factory(monkeypatch, outcomes)
    sleeps = await _no_sleep(monkeypatch)

    await engine._poll_once_with_retry(
        None, "BTC-USDT", None, None, None, _NullNotifier(), engine._MarketDataCache()
    )

    assert calls["count"] == 2
    assert sleeps == [2.0]


async def test_non_transient_error_raises_immediately(monkeypatch):
    outcomes = [BingXAPIError(80001, "signature verification failed")]
    calls = await _poll_once_stub_factory(monkeypatch, outcomes)
    sleeps = await _no_sleep(monkeypatch)

    with pytest.raises(BingXAPIError):
        await engine._poll_once_with_retry(
            None, "BTC-USDT", None, None, None, _NullNotifier(), engine._MarketDataCache()
        )

    assert calls["count"] == 1
    assert sleeps == []


async def test_network_read_error_recovers_after_retry(monkeypatch):
    outcomes = [httpx.ReadError(""), None]
    calls = await _poll_once_stub_factory(monkeypatch, outcomes)
    sleeps = await _no_sleep(monkeypatch)

    await engine._poll_once_with_retry(
        None, "BTC-USDT", None, None, None, _NullNotifier(), engine._MarketDataCache()
    )

    assert calls["count"] == 2
    assert sleeps == [2.0]


async def test_network_error_raises_once_retries_exhausted(monkeypatch):
    outcomes = [httpx.ConnectError("")] * (engine._TRANSIENT_RETRY_ATTEMPTS + 1)
    calls = await _poll_once_stub_factory(monkeypatch, outcomes)
    await _no_sleep(monkeypatch)

    with pytest.raises(httpx.ConnectError):
        await engine._poll_once_with_retry(
            None, "BTC-USDT", None, None, None, _NullNotifier(), engine._MarketDataCache()
        )

    assert calls["count"] == engine._TRANSIENT_RETRY_ATTEMPTS + 1


def test_next_heartbeat_time_before_first_slot_of_day():
    now = datetime(2026, 7, 16, 8, 0, tzinfo=engine._HEARTBEAT_TZ)
    assert engine._next_heartbeat_time(now) == datetime(
        2026, 7, 16, 9, 0, tzinfo=engine._HEARTBEAT_TZ
    )


def test_next_heartbeat_time_mid_window():
    now = datetime(2026, 7, 16, 10, 30, tzinfo=engine._HEARTBEAT_TZ)
    assert engine._next_heartbeat_time(now) == datetime(
        2026, 7, 16, 13, 0, tzinfo=engine._HEARTBEAT_TZ
    )


def test_next_heartbeat_time_wraps_past_midnight():
    now = datetime(2026, 7, 16, 23, 0, tzinfo=engine._HEARTBEAT_TZ)
    assert engine._next_heartbeat_time(now) == datetime(
        2026, 7, 17, 1, 0, tzinfo=engine._HEARTBEAT_TZ
    )


def test_next_heartbeat_time_early_morning_before_five():
    now = datetime(2026, 7, 17, 2, 0, tzinfo=engine._HEARTBEAT_TZ)
    assert engine._next_heartbeat_time(now) == datetime(
        2026, 7, 17, 5, 0, tzinfo=engine._HEARTBEAT_TZ
    )


def test_next_heartbeat_time_exactly_on_boundary_goes_to_next_slot():
    now = datetime(2026, 7, 16, 13, 0, tzinfo=engine._HEARTBEAT_TZ)
    assert engine._next_heartbeat_time(now) == datetime(
        2026, 7, 16, 17, 0, tzinfo=engine._HEARTBEAT_TZ
    )


class _StopLoop(Exception):
    pass


async def test_heartbeat_loop_survives_notifier_failure(monkeypatch):
    sent: list[str] = []

    class _FlakyNotifier:
        async def send(self, message: str) -> None:
            sent.append(message)
            raise RuntimeError("telegram down")

    monkeypatch.setattr(engine, "_next_heartbeat_time", lambda now: now)

    sleeps: list[float] = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) >= 2:
            raise _StopLoop

    monkeypatch.setattr(engine.asyncio, "sleep", fake_sleep)

    with pytest.raises(_StopLoop):
        await engine._heartbeat_loop("BTC-USDT", _FlakyNotifier())

    assert sent == ["✅ 시스템 정상 작동 중: BTC-USDT"]
    assert sleeps == [0.0, 0.0]
