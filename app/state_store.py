"""Restricted local persistence for runtime state under `.runtime/`."""

from __future__ import annotations

import logging
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
logger = logging.getLogger(__name__)


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
        self.session_meta_dir = self.runtime_dir / "session_meta"
        self.sessions_dir = self.runtime_dir / "sessions"
        self.active_session_file = self.runtime_dir / "active_session.json"
        self._ensure_private_directory(self.runtime_dir)
        self._ensure_private_directory(self.session_meta_dir)
        self._ensure_private_directory(self.sessions_dir)

    def _ensure_private_directory(self, path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True)
        _best_effort_chmod(path, PRIVATE_DIRECTORY_MODE)

    def session_meta_path(self, session_name: str) -> Path:
        return self.session_meta_dir / f"{_encode_session_name(session_name)}.json"

    def session_artifact_path(self, session_name: str) -> Path:
        return self.sessions_dir / f"{_encode_session_name(session_name)}.session"

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

    def _active_session_name(self) -> str | None:
        active_session = self.load_active_session()
        return None if active_session is None else active_session.session_name

    def _apply_active_marker(
        self,
        session: SessionMeta,
        active_session_name: str | None = None,
    ) -> SessionMeta:
        if active_session_name is None:
            active_session_name = self._active_session_name()
        is_active = active_session_name == session.session_name
        if session.is_active == is_active:
            return session
        return SessionMeta(
            session_name=session.session_name,
            api_id=session.api_id,
            api_hash=session.api_hash,
            created_at=session.created_at,
            updated_at=session.updated_at,
            account_label=session.account_label,
            phone_number=session.phone_number,
            is_active=is_active,
        )

    def save_session(self, session: SessionMeta) -> None:
        logger.debug("Saving session metadata for %r", session.session_name)
        self._write_json(self.session_meta_path(session.session_name), session.to_dict())

    def load_session(self, session_name: str) -> SessionMeta:
        logger.debug("Loading session metadata for %r", session_name)
        session = SessionMeta.from_dict(self._read_json(self.session_meta_path(session_name)))
        return self._apply_active_marker(session)

    def list_sessions(self) -> list[SessionMeta]:
        logger.debug("Listing sessions from %s", self.session_meta_dir)
        active_session_name = self._active_session_name()
        sessions = [
            self._apply_active_marker(
                SessionMeta.from_dict(self._read_json(path)),
                active_session_name,
            )
            for path in self.session_meta_dir.glob("*.json")
            if path.is_file()
        ]
        logger.debug("Listed %d sessions", len(sessions))
        return sorted(sessions, key=lambda item: item.session_name.casefold())

    def save_active_session(self, state: ActiveSessionState) -> None:
        logger.debug("Saving active session state for %r", state.session_name)
        self._write_json(self.active_session_file, state.to_dict())

    def set_active_session(self, session_name: str) -> ActiveSessionState:
        logger.info("Setting active session to %r", session_name)
        state = ActiveSessionState(
            session_name=session_name,
            updated_at=datetime.now(timezone.utc),
        )
        self.save_active_session(state)
        return state

    def load_active_session(self) -> ActiveSessionState | None:
        if not self.active_session_file.exists():
            logger.debug("No active session state is stored")
            return None
        logger.debug("Loading active session state")
        return ActiveSessionState.from_dict(self._read_json(self.active_session_file))

    def clear_active_session(self) -> None:
        logger.info("Clearing active session state")
        self.active_session_file.unlink(missing_ok=True)
