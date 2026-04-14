"""Named Telegram session lifecycle helpers."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Protocol

from telethon import errors

from .models import SessionMeta
from .state_store import StateStore
from .telegram_client import TelegramGateway


class SessionManagerError(RuntimeError):
    """Base class for session management failures."""


class NoActiveSessionError(SessionManagerError):
    """Raised when an operation requires an active session but none exists."""


class SessionNotFoundError(SessionManagerError):
    """Raised when a session name is not known to the local store."""


class SessionLoginError(SessionManagerError):
    """Raised when the Telegram login flow cannot be completed safely."""


class SessionAuthorizationError(SessionManagerError):
    """Raised when an authorized client was requested but auth is missing."""


class SessionPrompts(Protocol):
    def request_phone(self, session_name: str) -> str: ...

    def request_code(
        self,
        session_name: str,
        phone_number: str,
        attempt_number: int,
    ) -> str: ...

    def request_password(self, session_name: str, attempt_number: int) -> str: ...


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _clean_text(value: str, label: str) -> str:
    text = value.strip()
    if not text:
        raise ValueError(f"{label} must not be empty")
    return text


def _normalize_phone_number(value: str) -> str:
    phone_number = _clean_text(value, "phone number")
    return phone_number if phone_number.startswith("+") else f"+{phone_number}"


def _describe_account(user: Any) -> str | None:
    username = getattr(user, "username", None)
    if username:
        return f"@{username}"

    first_name = getattr(user, "first_name", None)
    last_name = getattr(user, "last_name", None)
    display_name = " ".join(part for part in [first_name, last_name] if part)
    if display_name.strip():
        return display_name.strip()

    phone = getattr(user, "phone", None)
    if phone:
        return _normalize_phone_number(str(phone))

    return None


class SessionManager:
    def __init__(
        self,
        state_store: StateStore,
        gateway: TelegramGateway | None = None,
    ) -> None:
        self.state_store = state_store
        self.gateway = gateway or TelegramGateway(state_store)

    def list_sessions(self) -> list[SessionMeta]:
        return self.state_store.list_sessions()

    def load_session(self, session_name: str) -> SessionMeta:
        try:
            return self.state_store.load_session(session_name)
        except FileNotFoundError as exc:
            raise SessionNotFoundError(session_name) from exc

    def get_active_session(self) -> SessionMeta | None:
        active_state = self.state_store.load_active_session()
        if active_state is None:
            return None
        try:
            return self.load_session(active_state.session_name)
        except SessionNotFoundError:
            return None

    def set_active_session(self, session_name: str) -> SessionMeta:
        session = self.load_session(session_name)
        self.state_store.set_active_session(session.session_name)
        return self.load_session(session.session_name)

    async def create_session(
        self,
        *,
        session_name: str,
        api_id: int,
        api_hash: str,
        prompts: SessionPrompts,
        phone_number: str | None = None,
        mark_active: bool = False,
        force_sms: bool = False,
    ) -> SessionMeta:
        session_name = _clean_text(session_name, "session_name")
        existing = self._load_existing_session(session_name)
        supplied_phone = (
            phone_number
            or (existing.phone_number if existing is not None else None)
            or prompts.request_phone(session_name)
        )
        phone_number = _normalize_phone_number(supplied_phone)
        created_at = existing.created_at if existing is not None else _utc_now()

        async with self.gateway.open_client(session_name, api_id, api_hash) as client:
            authorized = await self.gateway.run_with_retry(
                client.is_user_authorized,
                operation_name="check Telegram authorization",
            )
            if authorized:
                user = await self.gateway.get_current_user(client)
            else:
                user = await self._complete_login(
                    client=client,
                    session_name=session_name,
                    prompts=prompts,
                    phone_number=phone_number,
                    force_sms=force_sms,
                )

        session = SessionMeta(
            session_name=session_name,
            api_id=api_id,
            api_hash=api_hash,
            created_at=created_at,
            updated_at=_utc_now(),
            account_label=_describe_account(user),
            phone_number=_normalize_phone_number(
                str(getattr(user, "phone", None) or phone_number)
            ),
            is_active=False,
        )
        self.state_store.save_session(session)

        if mark_active:
            self.state_store.set_active_session(session.session_name)
            session = self.state_store.load_session(session.session_name)
        return session

    @asynccontextmanager
    async def open_authorized_client(
        self,
        session_name: str | None = None,
    ) -> AsyncIterator[Any]:
        session = self._resolve_session(session_name)
        async with self.gateway.open_client(
            session.session_name,
            session.api_id,
            session.api_hash,
        ) as client:
            authorized = await self.gateway.run_with_retry(
                client.is_user_authorized,
                operation_name="check Telegram authorization",
            )
            if not authorized:
                raise SessionAuthorizationError(
                    f"Session {session.session_name!r} is not authorized"
                )
            yield client

    def _resolve_session(self, session_name: str | None) -> SessionMeta:
        if session_name is None:
            session = self.get_active_session()
            if session is None:
                raise NoActiveSessionError("No active session is selected")
            return session
        return self.load_session(session_name)

    def _load_existing_session(self, session_name: str) -> SessionMeta | None:
        try:
            return self.state_store.load_session(session_name)
        except FileNotFoundError:
            return None

    async def _complete_login(
        self,
        *,
        client: Any,
        session_name: str,
        prompts: SessionPrompts,
        phone_number: str,
        force_sms: bool,
    ) -> Any:
        sent_code = await self.gateway.request_login_code(
            client,
            phone_number,
            force_sms=force_sms,
        )
        phone_code_hash = getattr(sent_code, "phone_code_hash", None)
        if not phone_code_hash:
            raise SessionLoginError("Telegram did not return a phone code hash")

        for code_attempt in range(1, 4):
            code = _clean_text(
                prompts.request_code(session_name, phone_number, code_attempt),
                "login code",
            )
            try:
                return await self._sign_in_with_code(
                    client=client,
                    session_name=session_name,
                    prompts=prompts,
                    code=code,
                    phone_number=phone_number,
                    phone_code_hash=phone_code_hash,
                )
            except errors.PhoneCodeInvalidError as exc:
                if code_attempt >= 3:
                    raise SessionLoginError("Telegram login code was rejected") from exc
                continue
            except errors.PhoneCodeExpiredError as exc:
                if code_attempt >= 3:
                    raise SessionLoginError("Telegram login code expired") from exc

            sent_code = await self.gateway.request_login_code(
                client,
                phone_number,
                force_sms=force_sms,
            )
            phone_code_hash = getattr(sent_code, "phone_code_hash", None)
            if not phone_code_hash:
                raise SessionLoginError("Telegram did not return a phone code hash")

        raise SessionLoginError("Telegram login code attempts were exhausted")

    async def _complete_login_with_password(
        self,
        *,
        client: Any,
        session_name: str,
        prompts: SessionPrompts,
        attempt_number: int,
    ) -> Any:
        for password_attempt in range(attempt_number, 4):
            password = _clean_text(
                prompts.request_password(session_name, password_attempt),
                "Telegram password",
            )
            try:
                await self.gateway.sign_in(client, password=password)
                return await self.gateway.get_current_user(client)
            except errors.PasswordHashInvalidError as exc:
                if password_attempt >= 3:
                    raise SessionLoginError("Telegram password was rejected") from exc

        raise SessionLoginError("Telegram password attempts were exhausted")

    async def _sign_in_with_code(
        self,
        *,
        client: Any,
        session_name: str,
        prompts: SessionPrompts,
        phone_number: str,
        code: str,
        phone_code_hash: str,
    ) -> Any:
        try:
            await self.gateway.sign_in(
                client,
                phone=phone_number,
                code=code,
                phone_code_hash=phone_code_hash,
            )
            return await self.gateway.get_current_user(client)
        except errors.SessionPasswordNeededError:
            return await self._complete_login_with_password(
                client=client,
                session_name=session_name,
                prompts=prompts,
                attempt_number=1,
            )
