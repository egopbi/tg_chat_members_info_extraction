"""Telethon gateway helpers used by the session service and future export flows."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, TypeVar

from telethon import TelegramClient, errors, utils
from telethon.tl import functions

from .models import RetryPolicy
from .state_store import StateStore

T = TypeVar("T")
logger = logging.getLogger(__name__)


class TelegramGatewayError(RuntimeError):
    """Raised when the gateway cannot satisfy a requested Telegram operation."""


class TelegramGateway:
    def __init__(
        self,
        state_store: StateStore,
        *,
        retry_policy: RetryPolicy | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.state_store = state_store
        self.retry_policy = retry_policy or RetryPolicy()
        self._sleep = sleep

    def session_path(self, session_name: str) -> Path:
        return self.state_store.session_artifact_path(session_name)

    def build_client(
        self,
        session_name: str,
        api_id: int,
        api_hash: str,
        **client_kwargs: Any,
    ) -> TelegramClient:
        logger.debug("Building Telegram client for session %r", session_name)
        kwargs = {
            "flood_sleep_threshold": 0,
            "request_retries": 0,
            "connection_retries": 0,
            "auto_reconnect": False,
        }
        kwargs.update(client_kwargs)
        return TelegramClient(self.session_path(session_name), api_id, api_hash, **kwargs)

    def bind_client(self, client: TelegramClient) -> "TelegramClientAdapter":
        return TelegramClientAdapter(client)

    @asynccontextmanager
    async def open_member_gateway(
        self,
        session_name: str,
        api_id: int,
        api_hash: str,
        **client_kwargs: Any,
    ) -> AsyncIterator["TelegramClientAdapter"]:
        async with self.open_client(
            session_name,
            api_id,
            api_hash,
            **client_kwargs,
        ) as client:
            yield self.bind_client(client)

    @asynccontextmanager
    async def open_client(
        self,
        session_name: str,
        api_id: int,
        api_hash: str,
        **client_kwargs: Any,
    ) -> AsyncIterator[TelegramClient]:
        client = self.build_client(session_name, api_id, api_hash, **client_kwargs)
        try:
            logger.info("Connecting Telegram session %r", session_name)
            await self.run_with_retry(
                client.connect,
                operation_name=f"connect Telegram session {session_name!r}",
            )
            logger.info("Connected Telegram session %r", session_name)
            yield client
        finally:
            if client.is_connected():
                logger.info("Disconnecting Telegram session %r", session_name)
                await client.disconnect()

    @staticmethod
    def is_retryable_error(error: BaseException) -> bool:
        if isinstance(error, (ConnectionError, OSError, TimeoutError)):
            return True
        name = error.__class__.__name__
        return name.endswith("WaitError") or name in {"ServerError", "TimedOutError"}

    async def run_with_retry(
        self,
        operation: Callable[[], Awaitable[T]],
        *,
        operation_name: str = "Telegram operation",
        retry_policy: RetryPolicy | None = None,
    ) -> T:
        policy = retry_policy or self.retry_policy
        waits_used = 0
        attempt_number = 1

        logger.debug("Starting %s", operation_name)
        while True:
            try:
                result = await operation()
                logger.debug("%s succeeded on attempt %d", operation_name, attempt_number)
                return result
            except Exception as error:
                if not self.is_retryable_error(error) or waits_used >= policy.max_waits:
                    logger.exception("%s failed on attempt %d", operation_name, attempt_number)
                    raise

                delay = policy.wait_seconds_for_attempt(attempt_number)
                seconds = getattr(error, "seconds", None)
                if isinstance(seconds, (int, float)) and seconds > 0:
                    delay = max(delay, float(seconds))
                logger.warning(
                    "%s hit %s; retrying in %.2fs",
                    operation_name,
                    error.__class__.__name__,
                    delay,
                    exc_info=True,
                )
                await self._sleep(delay)
                waits_used += 1
                attempt_number += 1

    async def ensure_authorized(self, client: TelegramClient) -> None:
        authorized = await self.run_with_retry(
            client.is_user_authorized,
            operation_name="check Telegram authorization",
        )
        logger.info("Telegram authorization check returned %s", authorized)
        if not authorized:
            logger.warning("Telegram client is not authorized")
            raise TelegramGatewayError("Telegram client is not authorized")

    async def get_current_user(self, client: TelegramClient) -> Any:
        logger.debug("Fetching current Telegram account")
        return await self.run_with_retry(
            client.get_me,
            operation_name="fetch current Telegram account",
        )

    async def request_login_code(
        self,
        client: TelegramClient,
        phone: str,
        *,
        force_sms: bool = False,
    ) -> Any:
        logger.info("Requesting Telegram login code (force_sms=%s)", force_sms)
        return await self.run_with_retry(
            lambda: client.send_code_request(phone, force_sms=force_sms),
            operation_name="request Telegram login code",
        )

    async def sign_in(
        self,
        client: TelegramClient,
        **kwargs: Any,
    ) -> Any:
        logger.info("Completing Telegram login")
        return await self.run_with_retry(
            lambda: client.sign_in(**kwargs),
            operation_name="complete Telegram login",
        )


@dataclass(slots=True)
class TelegramClientAdapter:
    client: TelegramClient

    async def get_me(self) -> Any:
        return await self.client.get_me()

    def iter_participants(self, chat: Any) -> AsyncIterator[Any]:
        return self.client.iter_participants(chat)

    async def get_full_user(self, user: Any) -> Any:
        custom_getter = getattr(self.client, "get_full_user", None)
        if callable(custom_getter):
            return await custom_getter(user)
        input_user = utils.get_input_user(user)
        response = await self.client(functions.users.GetFullUserRequest(input_user))
        full_user = getattr(response, "full_user", response)
        return full_user if full_user is not None else response

    async def get_entity(self, peer: Any) -> Any:
        return await self.client.get_entity(peer)

    async def download_profile_photo(self, entity: Any, file: Path) -> str | None:
        return await self.client.download_profile_photo(entity, file=file)
