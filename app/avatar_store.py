"""Local avatar path helpers for export runs."""

from __future__ import annotations

import os
import re
import unicodedata
from pathlib import Path
from typing import Iterable

PRIVATE_DIRECTORY_MODE = 0o700
PRIVATE_FILE_MODE = 0o600


def _best_effort_chmod(path: Path, mode: int) -> None:
    try:
        path.chmod(mode)
    except OSError:
        pass


def _safe_part(value: str | int | None) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if not text:
        return ""
    normalized = unicodedata.normalize("NFKD", text)
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii")
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", ascii_text).strip("._-")
    return cleaned.lower()


def safe_filename(*parts: str | int | None, default: str = "avatar") -> str:
    """Create a cross-platform safe filename stem."""

    cleaned_parts = [part for part in (_safe_part(part) for part in parts) if part]
    if not cleaned_parts:
        return default
    return "_".join(cleaned_parts)


class AvatarStore:
    def __init__(self, runtime_dir: Path | str = ".runtime") -> None:
        self.runtime_dir = Path(runtime_dir)
        self.exports_dir = self.runtime_dir / "exports"

    def _ensure_private_directory(self, path: Path) -> Path:
        path.mkdir(parents=True, exist_ok=True)
        if os.name == "posix":
            _best_effort_chmod(path, PRIVATE_DIRECTORY_MODE)
        return path

    def run_dir(self, run_id: str) -> Path:
        return self._ensure_private_directory(self.exports_dir / run_id)

    def avatars_dir(self, run_id: str) -> Path:
        return self._ensure_private_directory(self.run_dir(run_id) / "avatars")

    def avatar_path(
        self,
        run_id: str,
        user_id: int,
        *,
        username: str | None = None,
        display_name: str | None = None,
        extension: str = ".jpg",
    ) -> Path:
        stem = safe_filename(user_id, display_name, username, default=f"user_{user_id}")
        suffix = extension if extension.startswith(".") else f".{extension}"
        return self.avatars_dir(run_id) / f"{stem}{suffix}"

    def write_bytes(self, path: Path, data: bytes) -> Path:
        self._ensure_private_directory(path.parent)
        path.write_bytes(data)
        if os.name == "posix":
            _best_effort_chmod(path, PRIVATE_FILE_MODE)
        return path

    def write_text(self, path: Path, data: str, encoding: str = "utf-8") -> Path:
        self._ensure_private_directory(path.parent)
        path.write_text(data, encoding=encoding)
        if os.name == "posix":
            _best_effort_chmod(path, PRIVATE_FILE_MODE)
        return path
