from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from app.member_export import ExportSummary
from app.models import DialogCandidate, SessionMeta
from app.state_store import StateStore
from app.ui import ScriptedPromptBackend, TerminalUI
import app.ui as ui


class FakeGateway:
    def __init__(self) -> None:
        self.bound_clients: list[object] = []

    def bind_client(self, client: object) -> "FakeAdapter":
        self.bound_clients.append(client)
        return FakeAdapter()


class FakeAdapter:
    async def get_entity(self, peer: object) -> object:
        return SimpleNamespace(peer=peer)


class FakeSessionManager:
    def __init__(self, runtime_dir: Path, *, active_session: SessionMeta | None = None) -> None:
        self.state_store = StateStore(runtime_dir)
        self.gateway = FakeGateway()
        self._sessions: list[SessionMeta] = []
        self._active_session = active_session
        self.create_calls: list[dict[str, object]] = []
        self.set_active_calls: list[str] = []
        self.open_authorized_clients: list[str] = []
        if active_session is not None:
            self._sessions.append(active_session)
            self._active_session = active_session

    def list_sessions(self) -> list[SessionMeta]:
        return list(self._sessions)

    def get_active_session(self) -> SessionMeta | None:
        return self._active_session

    def set_active_session(self, session_name: str) -> SessionMeta:
        self.set_active_calls.append(session_name)
        session = next(item for item in self._sessions if item.session_name == session_name)
        self._active_session = session
        return session

    async def create_session(self, **kwargs: object) -> SessionMeta:
        self.create_calls.append(dict(kwargs))
        session = SessionMeta(
            session_name=str(kwargs["session_name"]),
            api_id=int(kwargs["api_id"]),
            api_hash=str(kwargs["api_hash"]),
            created_at=datetime(2026, 4, 14, 12, 0, tzinfo=timezone.utc),
            updated_at=datetime(2026, 4, 14, 12, 0, tzinfo=timezone.utc),
            account_label="@alice",
            phone_number="+15551234567",
            is_active=False,
        )
        self._sessions.append(session)
        return session

    @asynccontextmanager
    async def open_authorized_client(self, session_name: str):
        self.open_authorized_clients.append(session_name)
        yield SimpleNamespace(session_name=session_name)


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


def test_terminal_ui_refuses_non_interactive_stdio(monkeypatch, capsys, tmp_path: Path) -> None:
    monkeypatch.setattr(ui, "_is_interactive_terminal", lambda: False)
    terminal = TerminalUI(FakeSessionManager(tmp_path / ".runtime"), backend=ScriptedPromptBackend())
    exit_code = asyncio.run(terminal.run())

    assert exit_code == 1
    assert "Interactive terminal required" in capsys.readouterr().out


def test_create_session_flow_marks_new_session_active(tmp_path: Path) -> None:
    manager = FakeSessionManager(tmp_path / ".runtime")
    backend = ScriptedPromptBackend(
        text_responses=["new-session", "123456", "hash-abc", "15551234567"],
        confirm_responses=[True],
    )
    terminal = TerminalUI(manager, backend=backend, printer=lambda *_args, **_kwargs: None)

    asyncio.run(terminal.create_session_flow())

    assert manager.create_calls == [
        {
            "session_name": "new-session",
            "api_id": 123456,
            "api_hash": "hash-abc",
            "prompts": terminal.session_prompts,
            "mark_active": False,
        }
    ]
    assert manager.set_active_calls == ["new-session"]
    assert manager.get_active_session().session_name == "new-session"


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

    asyncio.run(terminal.switch_active_session_flow())

    assert manager.set_active_calls == ["other"]
    assert backend.select_messages[0][1] == (
        "active | @active *",
        "other | +1999…9999",
    )


def test_export_members_flow_uses_duplicate_picker_and_updates_last_status(
    tmp_path: Path,
    monkeypatch,
) -> None:
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

    asyncio.run(terminal.export_members_flow())

    assert manager.open_authorized_clients == ["active"]
    assert terminal.last_export_summary is not None
    assert terminal.last_export_summary.exported_count == 1
    assert backend.select_messages[0][0] == "Multiple dialogs matched. Choose one:"
