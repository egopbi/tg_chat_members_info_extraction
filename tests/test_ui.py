from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import app
import app.ui as ui
from app.member_export import ExportSummary
from app.models import DialogCandidate, SessionMeta
from app.state_store import StateStore
from app.ui import MENU_EXIT, ScriptedPromptBackend, TerminalUI
from telethon import errors


class _FakePrompt:
    def __init__(self, result: str) -> None:
        self.result = result
        self.ask_calls = 0

    def ask(self) -> str:
        self.ask_calls += 1
        return self.result


class _FakeQuestionary:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, object]]] = []

    def text(self, message: str, **kwargs: object) -> _FakePrompt:
        self.calls.append(("text", message, dict(kwargs)))
        return _FakePrompt("plain-value")

    def password(self, message: str, **kwargs: object) -> _FakePrompt:
        self.calls.append(("password", message, dict(kwargs)))
        return _FakePrompt("secret-value")


class FakeLoginClient:
    def __init__(self, *, authorized: bool, user: object) -> None:
        self.authorized = authorized
        self.user = user
        self.calls: list[str] = []

    async def is_user_authorized(self) -> bool:
        self.calls.append("is_user_authorized")
        return self.authorized


class FakeBoundGateway:
    def __init__(self) -> None:
        self.entities: list[object] = []

    async def get_entity(self, peer: object) -> object:
        self.entities.append(peer)
        return SimpleNamespace(peer=peer)


class FakeGateway:
    def __init__(self, *, login_user: object, export_user: object) -> None:
        self.login_user = login_user
        self.export_user = export_user
        self.open_client_calls: list[tuple[str, int, str]] = []
        self.request_login_code_calls: list[tuple[str, bool]] = []
        self.sign_in_calls: list[dict[str, object]] = []
        self.get_current_user_calls: int = 0
        self.open_authorized_clients: list[str] = []
        self.bound_gateway = FakeBoundGateway()

    @asynccontextmanager
    async def open_client(self, session_name: str, api_id: int, api_hash: str):
        self.open_client_calls.append((session_name, api_id, api_hash))
        yield FakeLoginClient(authorized=False, user=self.login_user)

    async def run_with_retry(self, operation, *, operation_name: str = "", retry_policy=None):
        return await operation()

    async def get_current_user(self, client: object) -> object:
        self.get_current_user_calls += 1
        return self.login_user if self.get_current_user_calls == 1 else self.export_user

    async def request_login_code(
        self,
        client: object,
        phone: str,
        *,
        force_sms: bool = False,
    ) -> object:
        self.request_login_code_calls.append((phone, force_sms))
        return SimpleNamespace(phone_code_hash="hash-1")

    async def sign_in(self, client: object, **kwargs: object) -> object:
        self.sign_in_calls.append(dict(kwargs))
        if kwargs.get("code") == "bad-code":
            raise errors.PhoneCodeInvalidError(None)
        return self.login_user

    @asynccontextmanager
    async def open_authorized_client(self, session_name: str):
        self.open_authorized_clients.append(session_name)
        yield SimpleNamespace(session_name=session_name)

    def bind_client(self, client: object) -> FakeBoundGateway:
        return self.bound_gateway


class FakeSessionManager:
    def __init__(self, runtime_dir: Path, *, active_session: SessionMeta | None = None) -> None:
        self.state_store = StateStore(runtime_dir)
        login_user = SimpleNamespace(username="alice", first_name="Alice", last_name="Example", phone="15551234567")
        export_user = SimpleNamespace(username="exporter", first_name="Export", last_name="User", phone="15551234567")
        self.gateway = FakeGateway(login_user=login_user, export_user=export_user)
        self._sessions: list[SessionMeta] = []
        self._active_session = active_session
        self.set_active_calls: list[str] = []
        if active_session is not None:
            self._sessions.append(active_session)

    def list_sessions(self) -> list[SessionMeta]:
        return list(self._sessions)

    def get_active_session(self) -> SessionMeta | None:
        return self._active_session

    def set_active_session(self, session_name: str) -> SessionMeta:
        self.set_active_calls.append(session_name)
        try:
            session = next(item for item in self._sessions if item.session_name == session_name)
        except StopIteration:
            session = self.state_store.load_session(session_name)
            self._sessions.append(session)
        self._active_session = SessionMeta(
            session_name=session.session_name,
            api_id=session.api_id,
            api_hash=session.api_hash,
            created_at=session.created_at,
            updated_at=session.updated_at,
            account_label=session.account_label,
            phone_number=session.phone_number,
            is_active=True,
        )
        self.state_store.save_session(self._active_session)
        self.state_store.set_active_session(session_name)
        self._sessions = [
            SessionMeta(
                session_name=item.session_name,
                api_id=item.api_id,
                api_hash=item.api_hash,
                created_at=item.created_at,
                updated_at=item.updated_at,
                account_label=item.account_label,
                phone_number=item.phone_number,
                is_active=item.session_name == session_name,
            )
            for item in self._sessions
        ]
        return self._active_session

    @asynccontextmanager
    async def open_authorized_client(self, session_name: str):
        async with self.gateway.open_authorized_client(session_name) as client:
            yield client


def _make_summary(runtime_dir: Path) -> ExportSummary:
    return ExportSummary(
        run_id="run-001",
        chat_label="Telegram Forum 👋",
        current_user_id=1,
        csv_path=runtime_dir / "exports" / "run-001" / "members.csv",
        avatars_dir=runtime_dir / "exports" / "run-001" / "avatars",
        rows=(),
        total_seen=2,
        exported_count=1,
        skipped_current_account=1,
        deduplicated_count=0,
        failed_user_ids=(),
        warnings=("user 2: one warning",),
    )


def test_questionary_prompt_backend_omits_none_default_for_text_and_password(monkeypatch) -> None:
    fake_questionary = _FakeQuestionary()
    monkeypatch.setattr(ui, "questionary", fake_questionary)

    backend = ui.QuestionaryPromptBackend()

    assert backend.ask_text("Session name:") == "plain-value"
    assert backend.ask_text("API hash:", secret=True) == "secret-value"
    assert backend.ask_text("Optional value:", default="fallback") == "plain-value"

    assert fake_questionary.calls == [
        ("text", "Session name:", {}),
        ("password", "API hash:", {}),
        ("text", "Optional value:", {"default": "fallback"}),
    ]


def test_app_main_refuses_non_interactive_stdio(monkeypatch, capsys) -> None:
    monkeypatch.setattr(ui, "_is_interactive_terminal", lambda: False)

    exit_code = app.main()

    assert exit_code == 1
    assert "Interactive terminal required" in capsys.readouterr().out


def test_terminal_ui_run_allows_prompt_backend_to_use_own_event_loop(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(ui, "_is_interactive_terminal", lambda: True)

    class LoopSafeBackend(ScriptedPromptBackend):
        def ask_select(self, message: str, choices, *, default=None):  # type: ignore[override]
            asyncio.run(asyncio.sleep(0))
            return super().ask_select(message, choices, default=default)

    manager = FakeSessionManager(tmp_path / ".runtime")
    backend = LoopSafeBackend(select_responses=[MENU_EXIT])
    terminal = TerminalUI(manager, backend=backend, printer=lambda *_args, **_kwargs: None)

    assert terminal.run() == 0
    assert backend.select_messages[0][0] == "Choose an action:"


def test_create_session_flow_saves_metadata_and_marks_active(tmp_path: Path) -> None:
    manager = FakeSessionManager(tmp_path / ".runtime")
    backend = ScriptedPromptBackend(
        text_responses=["new-session", "123456", "hash-abc", "15551234567", "12345"],
        confirm_responses=[True],
    )
    terminal = TerminalUI(manager, backend=backend, printer=lambda *_args, **_kwargs: None)

    terminal.create_session_flow()

    session = manager.state_store.load_session("new-session")
    assert session.account_label == "@alice"
    assert session.phone_number == "+15551234567"
    assert session.is_active is True
    assert manager.set_active_calls == ["new-session"]
    assert manager.gateway.open_client_calls == [
        ("new-session", 123456, "hash-abc"),
        ("new-session", 123456, "hash-abc"),
    ]
    assert manager.gateway.request_login_code_calls == [("15551234567", False)]
    assert manager.gateway.sign_in_calls[0]["code"] == "12345"
    assert manager.gateway.get_current_user_calls >= 1


def test_switch_active_session_flow_shows_context_and_updates_selection(tmp_path: Path) -> None:
    timestamp = datetime(2026, 4, 14, 12, 0, tzinfo=timezone.utc)
    active = SessionMeta(
        session_name="active",
        api_id=123,
        api_hash="hash",
        created_at=timestamp,
        updated_at=timestamp,
        account_label="@active",
        phone_number="+10000000000",
        is_active=True,
    )
    other = SessionMeta(
        session_name="other",
        api_id=456,
        api_hash="hash",
        created_at=timestamp,
        updated_at=timestamp,
        account_label=None,
        phone_number="+19999999999",
        is_active=False,
    )
    manager = FakeSessionManager(tmp_path / ".runtime", active_session=active)
    manager._sessions.append(other)
    backend = ScriptedPromptBackend(select_responses=["other"])
    terminal = TerminalUI(manager, backend=backend, printer=lambda *_args, **_kwargs: None)

    terminal.switch_active_session_flow()

    assert manager.set_active_calls == ["other"]
    assert backend.select_messages[0][1] == (
        "active | @active *",
        "other | +1999…9999",
    )


def test_export_members_flow_uses_duplicate_picker_and_updates_last_status(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(ui, "_is_interactive_terminal", lambda: True)
    timestamp = datetime(2026, 4, 14, 12, 0, tzinfo=timezone.utc)
    active = SessionMeta(
        session_name="active",
        api_id=123,
        api_hash="hash",
        created_at=timestamp,
        updated_at=timestamp,
        account_label="@active",
        phone_number="+10000000000",
        is_active=True,
    )
    manager = FakeSessionManager(tmp_path / ".runtime", active_session=active)
    backend = ScriptedPromptBackend(text_responses=["Telegram Forum 👋"], select_responses=[])
    terminal = TerminalUI(manager, backend=backend, printer=lambda *_args, **_kwargs: None)

    candidates = [
        DialogCandidate(
            title="Telegram Forum 👋",
            entity_type="forum",
            peer_id=-1001,
            username="forum-one",
            participants_count=10,
            last_message_date=timestamp,
        ),
        DialogCandidate(
            title="Telegram Forum 👋",
            entity_type="forum",
            peer_id=-1002,
            username=None,
            participants_count=20,
            last_message_date=timestamp,
        ),
    ]

    async def fake_find_dialog_candidates(client: object, query: str):
        assert query == "Telegram Forum 👋"
        return candidates

    async def fake_export_members(gateway: object, chat: object, **kwargs: object) -> ExportSummary:
        assert isinstance(chat, SimpleNamespace)
        assert chat.peer == -1002
        return _make_summary(manager.state_store.runtime_dir)

    monkeypatch.setattr(ui, "find_dialog_candidates", fake_find_dialog_candidates)
    monkeypatch.setattr(ui, "export_members", fake_export_members)
    backend._select_responses.append(candidates[1])

    terminal.export_members_flow()

    assert manager.gateway.open_authorized_clients == ["active", "active"]
    assert terminal.last_export_summary is not None
    assert terminal.last_export_summary.exported_count == 1
    assert backend.select_messages[0][0] == "Multiple dialogs matched. Choose one:"
