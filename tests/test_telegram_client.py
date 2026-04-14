from __future__ import annotations

import asyncio
from pathlib import Path
from urllib.parse import quote

import pytest
from telethon import errors

from app.models import RetryPolicy
from app.state_store import StateStore
from app.telegram_client import TelegramGateway


def test_gateway_session_path_uses_named_runtime_sessions(tmp_path: Path) -> None:
    store = StateStore(tmp_path / ".runtime")
    gateway = TelegramGateway(store)

    session_name = "Сессия 👋"
    assert gateway.session_path(session_name) == (
        tmp_path / ".runtime" / "sessions" / f"{quote(session_name, safe='')}.session"
    )


def test_run_with_retry_caps_waits_at_four(tmp_path: Path) -> None:
    async def run() -> None:
        store = StateStore(tmp_path / ".runtime")
        sleeps: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            sleeps.append(seconds)

        gateway = TelegramGateway(
            store,
            retry_policy=RetryPolicy(max_waits=4, initial_wait_seconds=1.0, backoff_factor=2.0, max_wait_seconds=4.0),
            sleep=fake_sleep,
        )

        attempts = {"count": 0}

        async def operation() -> str:
            attempts["count"] += 1
            if attempts["count"] <= 5:
                exc = errors.FloodWaitError(None)
                exc.seconds = 0
                raise exc
            return "ok"

        with pytest.raises(errors.FloodWaitError):
            await gateway.run_with_retry(operation)

        assert attempts["count"] == 5
        assert sleeps == [1.0, 2.0, 4.0, 4.0]

    asyncio.run(run())
