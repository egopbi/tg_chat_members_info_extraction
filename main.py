"""Stdlib-only bootstrap launcher for the Telegram export tool."""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import venv
from pathlib import Path
from typing import Callable

MINIMUM_PYTHON = (3, 10)
PROJECT_ROOT = Path(__file__).resolve().parent
VENV_DIRECTORY_NAME = ".venv"
REQUIREMENTS_FILENAME = "requirements.txt"
REQUIREMENTS_MARKER_FILENAME = ".requirements.sha256"


def _project_root(path: Path | None = None) -> Path:
    return PROJECT_ROOT if path is None else Path(path)


def _venv_dir(project_root: Path) -> Path:
    return project_root / VENV_DIRECTORY_NAME


def _venv_python(project_root: Path) -> Path:
    if os.name == "nt":
        return _venv_dir(project_root) / "Scripts" / "python.exe"
    return _venv_dir(project_root) / "bin" / "python"


def _require_supported_python(version_info: tuple[int, int] | None = None) -> None:
    version = version_info or sys.version_info[:2]
    if tuple(version) < MINIMUM_PYTHON:
        raise RuntimeError(
            f"Python {MINIMUM_PYTHON[0]}.{MINIMUM_PYTHON[1]} or newer is required"
        )


def _create_venv(venv_dir: Path) -> None:
    builder = venv.EnvBuilder(with_pip=True)
    builder.create(venv_dir)


def _install_requirements(venv_python: Path, requirements_file: Path) -> None:
    subprocess.run(
        [str(venv_python), "-m", "pip", "install", "-r", str(requirements_file)],
        check=True,
    )


def _run_app() -> int:
    import app

    result = app.main()
    return 0 if result is None else int(result)


def _handoff_to_app(venv_python: Path) -> None:
    os.execv(
        str(venv_python),
        [str(venv_python), "-c", "import app; raise SystemExit(app.main())"],
    )


def _same_interpreter(venv_python: Path, current_executable: Path | None = None) -> bool:
    executable = Path(sys.executable if current_executable is None else current_executable)
    try:
        return executable.resolve() == venv_python.resolve()
    except FileNotFoundError:
        return False


def _requirements_hash(requirements_file: Path) -> str:
    return hashlib.sha256(requirements_file.read_bytes()).hexdigest()


def _requirements_marker(venv_dir: Path) -> Path:
    return venv_dir / REQUIREMENTS_MARKER_FILENAME


def _requirements_are_current(venv_dir: Path, requirements_file: Path) -> bool:
    marker = _requirements_marker(venv_dir)
    if not marker.exists():
        return False
    return marker.read_text(encoding="utf-8").strip() == _requirements_hash(requirements_file)


def _record_requirements_hash(venv_dir: Path, requirements_file: Path) -> None:
    _requirements_marker(venv_dir).write_text(
        _requirements_hash(requirements_file),
        encoding="utf-8",
    )


def bootstrap(
    project_root: Path | None = None,
    *,
    version_info: tuple[int, int] | None = None,
    current_executable: Path | None = None,
    create_venv: Callable[[Path], None] = _create_venv,
    install_requirements: Callable[[Path, Path], None] = _install_requirements,
    run_app: Callable[[], int] = _run_app,
    handoff_to_app: Callable[[Path], None] = _handoff_to_app,
) -> int:
    """Prepare the self-managed virtual environment and launch the app package."""

    _require_supported_python(version_info)
    root = _project_root(project_root)
    venv_dir = _venv_dir(root)
    venv_python = _venv_python(root)
    requirements_file = root / REQUIREMENTS_FILENAME

    if not requirements_file.exists():
        raise FileNotFoundError(requirements_file)

    if not venv_python.exists():
        create_venv(venv_dir)

    if not _requirements_are_current(venv_dir, requirements_file):
        install_requirements(venv_python, requirements_file)
        _record_requirements_hash(venv_dir, requirements_file)

    if _same_interpreter(venv_python, current_executable=current_executable):
        return run_app()

    handoff_to_app(venv_python)
    return 0


def main() -> int:
    return bootstrap()


if __name__ == "__main__":
    raise SystemExit(main())
