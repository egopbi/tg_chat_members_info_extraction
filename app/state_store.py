"""Restricted local persistence for runtime state under `.runtime/`."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

from .models import ActiveSessionState, SessionMeta

PRIVATE_DIRECTORY_MODE = 0o700
PRIVATE_FILE_MODE = 0o600


def _best_effort_chmod(path: Path, mode: int) -> None:
    try:
        path.chmod(mode)
    except OSError:
        pass


def _encode_session_name(session_name: str) -> str:
    return quote(session_name, safe="")


class StateStore:
    def __init__(self, runtime_dir: Path | str = ".runtime") -> None:
        self.runtime_dir = Path(runtime_dir)
        self.sessions_dir = self.runtime_dir / "sessions"
        self.active_session_file = self.runtime_dir / "active_session.json"
        self._ensure_private_directory(self.runtime_dir)
        self._ensure_private_directory(self.sessions_dir)

    def _ensure_private_directory(self, path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True)
        _best_effort_chmod(path, PRIVATE_DIRECTORY_MODE)

    def _session_path(self, session_name: str) -> Path:
        return self.sessions_dir / f"{_encode_session_name(session_name)}.json"

    def _write_json(self, path: Path, payload: dict[str, Any]) -> None:
        self._ensure_private_directory(path.parent)
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            delete=False,
        ) as temp_file:
            json.dump(payload, temp_file, ensure_ascii=False, indent=2, sort_keys=True)
            temp_file.write("\n")
            temp_file.flush()
            os.fsync(temp_file.fileno())
            temp_path = Path(temp_file.name)
        _best_effort_chmod(temp_path, PRIVATE_FILE_MODE)
        os.replace(temp_path, path)
        _best_effort_chmod(path, PRIVATE_FILE_MODE)

    def _read_json(self, path: Path) -> dict[str, Any]:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)

    def save_session(self, session: SessionMeta) -> None:
        self._write_json(self._session_path(session.session_name), session.to_dict())

    def load_session(self, session_name: str) -> SessionMeta:
        return SessionMeta.from_dict(self._read_json(self._session_path(session_name)))

    def list_sessions(self) -> list[SessionMeta]:
        sessions = [
            SessionMeta.from_dict(self._read_json(path))
            for path in self.sessions_dir.glob("*.json")
            if path.is_file()
        ]
        return sorted(sessions, key=lambda item: item.session_name.casefold())

    def save_active_session(self, state: ActiveSessionState) -> None:
        self._write_json(self.active_session_file, state.to_dict())

    def set_active_session(self, session_name: str) -> ActiveSessionState:
        state = ActiveSessionState(
            session_name=session_name,
            updated_at=datetime.now(timezone.utc),
        )
        self.save_active_session(state)
        return state

    def load_active_session(self) -> ActiveSessionState | None:
        if not self.active_session_file.exists():
            return None
        return ActiveSessionState.from_dict(self._read_json(self.active_session_file))

    def clear_active_session(self) -> None:
        self.active_session_file.unlink(missing_ok=True)
