from __future__ import annotations

import asyncio
import hashlib
import logging
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from telethon import errors

import main
from app.models import SessionMeta
from app.runtime_logging import configure_runtime_logging, runtime_log_path
from app.state_store import StateStore
from app.telegram_client import TelegramGateway


def test_runtime_logging_uses_a_stable_file_path(tmp_path: Path) -> None:
    runtime_dir = tmp_path / ".runtime"
    log_path = configure_runtime_logging(runtime_dir)

    assert log_path == runtime_log_path(runtime_dir)
    assert log_path == runtime_dir / "runtime.log"

    logging.getLogger("runtime.test").debug("debug message")

    content = log_path.read_text(encoding="utf-8")
    assert "Runtime logging configured" in content
    assert "debug message" in content


def test_bootstrap_logs_lifecycle_and_failures(tmp_path: Path) -> None:
    (tmp_path / "requirements.txt").write_text(
        "Telethon>=1.40,<2\nquestionary>=2,<3\n",
        encoding="utf-8",
    )
    venv_python = tmp_path / ".venv" / "bin" / "python"
    venv_python.parent.mkdir(parents=True, exist_ok=True)
    venv_python.write_text("", encoding="utf-8")
    marker = tmp_path / ".venv" / ".requirements.sha256"
    marker.write_text(
        hashlib.sha256((tmp_path / "requirements.txt").read_bytes()).hexdigest(),
        encoding="utf-8",
    )

    original_prefix = main.sys.prefix
    original_base_prefix = main.sys.base_prefix
    main.sys.prefix = str(tmp_path / ".venv")
    main.sys.base_prefix = "/usr"

    def run_app() -> int:
        raise RuntimeError("boom")

    try:
        with pytest.raises(RuntimeError, match="boom"):
            main.bootstrap(
                project_root=tmp_path,
                version_info=(3, 10),
                current_executable=Path("/usr/bin/python3"),
                run_app=run_app,
                handoff_to_app=lambda _: None,
            )
    finally:
        main.sys.prefix = original_prefix
        main.sys.base_prefix = original_base_prefix

    log_text = (tmp_path / ".runtime" / "runtime.log").read_text(encoding="utf-8")
    assert "Bootstrap starting" in log_text
    assert "Running app in the current interpreter" in log_text
    assert "Bootstrap failed" in log_text
    assert "RuntimeError: boom" in log_text


def test_state_store_logs_state_operations(tmp_path: Path) -> None:
    runtime_dir = tmp_path / ".runtime"
    configure_runtime_logging(runtime_dir)
    store = StateStore(runtime_dir)
    timestamp = datetime(2026, 4, 14, 12, 0, tzinfo=timezone.utc)
    session = SessionMeta(
        session_name="Сессия 👋",
        api_id=123456,
        api_hash="secret",
        created_at=timestamp,
        updated_at=timestamp,
        account_label="Test User",
    )

    store.save_session(session)
    store.set_active_session(session.session_name)
    store.load_session(session.session_name)
    store.list_sessions()
    store.clear_active_session()

    log_text = (runtime_dir / "runtime.log").read_text(encoding="utf-8")
    assert "Saving session metadata for 'Сессия 👋'" in log_text
    assert "Setting active session to 'Сессия 👋'" in log_text
    assert "Loading session metadata for 'Сессия 👋'" in log_text
    assert "Listing sessions" in log_text
    assert "Clearing active session state" in log_text


def test_gateway_logs_connect_retry_and_authentication(tmp_path: Path) -> None:
    async def run() -> None:
        runtime_dir = tmp_path / ".runtime"
        configure_runtime_logging(runtime_dir)
        store = StateStore(runtime_dir)
        sleeps: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            sleeps.append(seconds)

        gateway = TelegramGateway(store, sleep=fake_sleep)

        class FlakyClient:
            def __init__(self) -> None:
                self.connected = False
                self.connect_attempts = 0

            async def connect(self) -> None:
                self.connect_attempts += 1
                if self.connect_attempts == 1:
                    exc = errors.FloodWaitError(None)
                    exc.seconds = 2
                    raise exc
                self.connected = True

            def is_connected(self) -> bool:
                return self.connected

            async def disconnect(self) -> None:
                self.connected = False

            async def is_user_authorized(self) -> bool:
                return True

            async def get_me(self) -> object:
                return SimpleNamespace(username="alice")

        client = FlakyClient()
        gateway.build_client = lambda *args, **kwargs: client  # type: ignore[assignment]

        async with gateway.open_client("profile", 123456, "hash") as opened:
            await gateway.ensure_authorized(opened)
            user = await gateway.get_current_user(opened)
            assert getattr(user, "username") == "alice"

        assert client.connect_attempts == 2
        assert sleeps == [2.0]

    asyncio.run(run())

    log_text = (tmp_path / ".runtime" / "runtime.log").read_text(encoding="utf-8")
    assert "Connecting Telegram session 'profile'" in log_text
    assert "connect Telegram session 'profile' hit FloodWaitError" in log_text
    assert "Telegram authorization check returned True" in log_text
    assert "Fetching current Telegram account" in log_text
    assert "Disconnecting Telegram session 'profile'" in log_text
