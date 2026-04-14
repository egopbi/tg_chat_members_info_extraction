from __future__ import annotations

import os
import stat
from datetime import datetime, timezone
from pathlib import Path

from app.models import ActiveSessionState, SessionMeta
from app.state_store import StateStore


def test_state_store_persists_sessions_and_active_state(tmp_path: Path) -> None:
    store = StateStore(tmp_path / ".runtime")
    timestamp = datetime(2026, 4, 14, 12, 0, tzinfo=timezone.utc)
    session = SessionMeta(
        session_name="Сессия 👋",
        api_id=123456,
        api_hash="super-secret",
        created_at=timestamp,
        updated_at=timestamp,
        account_label="Telegram User",
    )

    store.save_session(session)
    active_state = store.set_active_session(session.session_name)

    loaded_session = store.load_session(session.session_name)
    listed_sessions = store.list_sessions()

    assert loaded_session.session_name == session.session_name
    assert loaded_session.is_active is True
    assert listed_sessions == [loaded_session]
    assert store.load_active_session() == active_state
    assert active_state.session_name == session.session_name

    runtime_dir = tmp_path / ".runtime"
    session_meta_dir = runtime_dir / "session_meta"
    session_file = next(
        path
        for path in session_meta_dir.glob("*.json")
        if path.name != "active_session.json"
    )

    if os.name == "posix":
        assert stat.S_IMODE(runtime_dir.stat().st_mode) == 0o700
        assert stat.S_IMODE(session_meta_dir.stat().st_mode) == 0o700
        assert stat.S_IMODE((runtime_dir / "sessions").stat().st_mode) == 0o700
        assert stat.S_IMODE(session_file.stat().st_mode) == 0o600
        assert stat.S_IMODE((runtime_dir / "active_session.json").stat().st_mode) == 0o600

    assert (runtime_dir / "sessions").exists()


def test_state_store_clears_active_session(tmp_path: Path) -> None:
    store = StateStore(tmp_path / ".runtime")
    state = ActiveSessionState(
        session_name="profile",
        updated_at=datetime(2026, 4, 14, 12, 5, tzinfo=timezone.utc),
    )

    store.save_active_session(state)
    assert store.load_active_session() == state

    store.clear_active_session()
    assert store.load_active_session() is None
