from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from telethon import errors

from app.models import SessionMeta
from app.session_manager import (
    NoActiveSessionError,
    SessionAuthorizationError,
    SessionManager,
)
from app.state_store import StateStore


class FakePrompts:
    def __init__(self) -> None:
        self.phone_requests: list[str] = []
        self.code_requests: list[tuple[str, str, int]] = []
        self.password_requests: list[tuple[str, int]] = []

    def request_phone(self, session_name: str) -> str:
        self.phone_requests.append(session_name)
        return "15551234567"

    def request_code(
        self,
        session_name: str,
        phone_number: str,
        attempt_number: int,
    ) -> str:
        self.code_requests.append((session_name, phone_number, attempt_number))
        return "12345"

    def request_password(self, session_name: str, attempt_number: int) -> str:
        self.password_requests.append((session_name, attempt_number))
        return "secret"


class RetryOncePrompts(FakePrompts):
    def request_code(
        self,
        session_name: str,
        phone_number: str,
        attempt_number: int,
    ) -> str:
        self.code_requests.append((session_name, phone_number, attempt_number))
        return "bad-code" if attempt_number == 1 else "good-code"


class FakeClient:
    def __init__(self, *, authorized: bool, user: object) -> None:
        self.authorized = authorized
        self.user = user
        self.connected = False
        self.calls: list[tuple[str, tuple[object, ...], dict[str, object]]] = []

    async def connect(self) -> None:
        self.calls.append(("connect", (), {}))
        self.connected = True

    async def disconnect(self) -> None:
        self.calls.append(("disconnect", (), {}))
        self.connected = False

    async def is_user_authorized(self) -> bool:
        self.calls.append(("is_user_authorized", (), {}))
        return self.authorized

    async def get_me(self) -> object:
        self.calls.append(("get_me", (), {}))
        return self.user

    async def send_code_request(
        self,
        phone: str,
        *,
        force_sms: bool = False,
    ) -> object:
        self.calls.append(("send_code_request", (phone,), {"force_sms": force_sms}))
        return SimpleNamespace(phone_code_hash="hash-1")

    async def sign_in(self, **kwargs: object) -> object:
        self.calls.append(("sign_in", (), kwargs))
        if kwargs.get("code") == "bad-code":
            raise errors.PhoneCodeInvalidError(None)
        self.authorized = True
        return self.user


class FakeGateway:
    def __init__(self, store: StateStore, client: FakeClient) -> None:
        self.store = store
        self.client = client
        self.retry_calls: list[str] = []
        self.login_code_requests = 0

    def session_path(self, session_name: str) -> Path:
        return self.store.session_artifact_path(session_name)

    @asynccontextmanager
    async def open_client(self, session_name: str, api_id: int, api_hash: str):
        self.session_path(session_name).parent.mkdir(parents=True, exist_ok=True)
        self.session_path(session_name).touch()
        await self.client.connect()
        try:
            yield self.client
        finally:
            await self.client.disconnect()

    async def run_with_retry(self, operation, *, operation_name: str = "", retry_policy=None):
        self.retry_calls.append(operation_name)
        return await operation()

    async def get_current_user(self, client: FakeClient) -> object:
        return await client.get_me()

    async def request_login_code(
        self,
        client: FakeClient,
        phone: str,
        *,
        force_sms: bool = False,
    ) -> object:
        self.login_code_requests += 1
        return await client.send_code_request(phone, force_sms=force_sms)

    async def sign_in(self, client: FakeClient, **kwargs: object) -> object:
        return await client.sign_in(**kwargs)


def test_create_session_persists_metadata_and_can_mark_active(tmp_path: Path) -> None:
    async def run() -> None:
        store = StateStore(tmp_path / ".runtime")
        user = SimpleNamespace(
            username="alice",
            first_name="Alice",
            last_name="Example",
            phone="15551234567",
        )
        client = FakeClient(authorized=False, user=user)
        gateway = FakeGateway(store, client)
        manager = SessionManager(store, gateway=gateway)
        prompts = FakePrompts()

        session = await manager.create_session(
            session_name="Сессия 👋",
            api_id=123456,
            api_hash="hash",
            prompts=prompts,
            mark_active=True,
        )

        assert session.session_name == "Сессия 👋"
        assert session.account_label == "@alice"
        assert session.phone_number == "+15551234567"
        assert session.is_active is True
        assert store.load_active_session().session_name == "Сессия 👋"
        assert store.load_session("Сессия 👋").is_active is True
        assert prompts.phone_requests == ["Сессия 👋"]
        assert prompts.code_requests == [("Сессия 👋", "+15551234567", 1)]
        assert (store.session_artifact_path("Сессия 👋")).exists()

    asyncio.run(run())


def test_create_session_reuses_phone_code_hash_after_invalid_code(tmp_path: Path) -> None:
    async def run() -> None:
        store = StateStore(tmp_path / ".runtime")
        user = SimpleNamespace(
            username="alice",
            first_name="Alice",
            last_name="Example",
            phone="15551234567",
        )
        client = FakeClient(authorized=False, user=user)
        gateway = FakeGateway(store, client)
        manager = SessionManager(store, gateway=gateway)
        prompts = RetryOncePrompts()

        session = await manager.create_session(
            session_name="retry-session",
            api_id=123456,
            api_hash="hash",
            prompts=prompts,
        )

        assert session.session_name == "retry-session"
        assert gateway.login_code_requests == 1
        assert prompts.code_requests == [
            ("retry-session", "+15551234567", 1),
            ("retry-session", "+15551234567", 2),
        ]
        assert [call for call in client.calls if call[0] == "sign_in"] == [
            (
                "sign_in",
                (),
                {
                    "phone": "+15551234567",
                    "code": "bad-code",
                    "phone_code_hash": "hash-1",
                },
            ),
            (
                "sign_in",
                (),
                {
                    "phone": "+15551234567",
                    "code": "good-code",
                    "phone_code_hash": "hash-1",
                },
            ),
        ]

    asyncio.run(run())


def test_open_authorized_client_returns_active_client(tmp_path: Path) -> None:
    async def run() -> None:
        store = StateStore(tmp_path / ".runtime")
        session = SessionMeta(
            session_name="profile",
            api_id=123456,
            api_hash="hash",
            created_at=datetime(2026, 4, 14, 12, 0, tzinfo=timezone.utc),
            updated_at=datetime(2026, 4, 14, 12, 0, tzinfo=timezone.utc),
            account_label="Test User",
            phone_number="+15551234567",
            is_active=True,
        )
        store.save_session(session)
        store.set_active_session(session.session_name)

        client = FakeClient(
            authorized=True,
            user=SimpleNamespace(username="test_user", phone="15551234567"),
        )
        gateway = FakeGateway(store, client)
        manager = SessionManager(store, gateway=gateway)

        async with manager.open_authorized_client() as opened_client:
            assert opened_client is client

        assert client.calls[0][0] == "connect"
        assert client.calls[-1][0] == "disconnect"

    asyncio.run(run())


def test_open_authorized_client_rejects_unauthorized_session(tmp_path: Path) -> None:
    async def run() -> None:
        store = StateStore(tmp_path / ".runtime")
        session = SessionMeta(
            session_name="profile",
            api_id=123456,
            api_hash="hash",
            created_at=datetime(2026, 4, 14, 12, 0, tzinfo=timezone.utc),
            updated_at=datetime(2026, 4, 14, 12, 0, tzinfo=timezone.utc),
            account_label="Test User",
            phone_number="+15551234567",
            is_active=False,
        )
        store.save_session(session)
        store.set_active_session(session.session_name)

        client = FakeClient(authorized=False, user=SimpleNamespace(username="test_user"))
        gateway = FakeGateway(store, client)
        manager = SessionManager(store, gateway=gateway)

        with pytest.raises(SessionAuthorizationError):
            async with manager.open_authorized_client():
                pass

    asyncio.run(run())


def test_open_authorized_client_requires_active_session(tmp_path: Path) -> None:
    store = StateStore(tmp_path / ".runtime")
    manager = SessionManager(store, gateway=FakeGateway(store, FakeClient(authorized=True, user=object())))

    with pytest.raises(NoActiveSessionError):
        asyncio.run(_open_without_session(manager))


async def _open_without_session(manager: SessionManager) -> None:
    async with manager.open_authorized_client():
        pass


def test_set_active_session_marks_store_and_returns_active_meta(tmp_path: Path) -> None:
    store = StateStore(tmp_path / ".runtime")
    timestamp = datetime(2026, 4, 14, 12, 0, tzinfo=timezone.utc)
    store.save_session(
        SessionMeta(
            session_name="first",
            api_id=123456,
            api_hash="hash",
            created_at=timestamp,
            updated_at=timestamp,
            account_label="First User",
        )
    )
    store.save_session(
        SessionMeta(
            session_name="second",
            api_id=123456,
            api_hash="hash",
            created_at=timestamp,
            updated_at=timestamp,
            account_label="Second User",
        )
    )
    manager = SessionManager(store, gateway=FakeGateway(store, FakeClient(authorized=True, user=object())))

    active_session = manager.set_active_session("second")

    assert active_session.session_name == "second"
    assert active_session.is_active is True
    assert store.load_active_session().session_name == "second"
    assert store.load_session("first").is_active is False
