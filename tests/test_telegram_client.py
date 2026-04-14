from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote

import pytest
from telethon import errors

from app.member_export import export_members
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


def test_concrete_gateway_adapter_is_wireable_to_export_members(tmp_path: Path) -> None:
    async def run() -> None:
        store = StateStore(tmp_path / ".runtime")
        gateway = TelegramGateway(store)
        client = gateway.build_client("session-one", 123456, "hash")

        current_user = SimpleNamespace(id=1, username="current")
        participant = SimpleNamespace(
            id=2,
            first_name="Alice",
            last_name="Example",
            username="alice",
            photo=object(),
        )
        full_user = SimpleNamespace(
            about="About Alice",
            birthday=SimpleNamespace(day=1, month=2, year=2000),
            personal_channel_id=77,
        )
        linked_channel = SimpleNamespace(username="alice-channel")

        async def get_me() -> object:
            return current_user

        def iter_participants(chat: object):
            async def generator():
                yield current_user
                yield participant

            return generator()

        async def get_full_user(user: object) -> object:
            return full_user

        async def get_entity(peer: object) -> object:
            return linked_channel

        async def download_profile_photo(entity: object, file: Path) -> str:
            file.write_bytes(b"avatar-bytes")
            return str(file)

        setattr(client, "get_me", get_me)
        setattr(client, "iter_participants", iter_participants)
        setattr(client, "get_full_user", get_full_user)
        setattr(client, "get_entity", get_entity)
        setattr(client, "download_profile_photo", download_profile_photo)

        adapter = gateway.bind_client(client)

        summary = await export_members(
            adapter,
            chat=object(),
            runtime_dir=tmp_path / ".runtime",
            run_id="run-001",
            sleep=lambda _: asyncio.sleep(0),
            jitter=lambda _: 0.0,
            chat_label="Concrete Group",
        )

        assert summary.exported_count == 1
        assert summary.skipped_current_account == 1
        row = summary.rows[0]
        assert row.user_id == 2
        assert row.about.status == "value"
        assert row.birthday.status == "value"
        assert row.linked_channel_url.value == "https://t.me/alice-channel"
        assert row.photo_path.status == "value"
        assert summary.csv_path.exists()

    asyncio.run(run())
