"""Stdlib-only bootstrap launcher for the Telegram export tool."""

from __future__ import annotations

import hashlib
import logging
import os
import subprocess
import sys
import venv
from pathlib import Path
from typing import Callable

from app.runtime_logging import configure_runtime_logging

MINIMUM_PYTHON = (3, 10)
PROJECT_ROOT = Path(__file__).resolve().parent
VENV_DIRECTORY_NAME = ".venv"
REQUIREMENTS_FILENAME = "requirements.txt"
REQUIREMENTS_MARKER_FILENAME = ".requirements.sha256"
logger = logging.getLogger(__name__)


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
    command = [
        str(venv_python),
        "-m",
        "pip",
        "install",
        "-r",
        str(requirements_file),
    ]
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        if result.stdout.strip():
            logger.debug("pip install stdout:\n%s", result.stdout.strip())
        if result.stderr.strip():
            logger.debug("pip install stderr:\n%s", result.stderr.strip())
        return

    if result.stdout.strip():
        logger.error("pip install stdout:\n%s", result.stdout.strip())
    if result.stderr.strip():
        logger.error("pip install stderr:\n%s", result.stderr.strip())
    raise subprocess.CalledProcessError(
        result.returncode,
        command,
        output=result.stdout,
        stderr=result.stderr,
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


def _path_matches(candidate: str | Path | None, target: Path) -> bool:
    if candidate is None:
        return False
    candidate_path = Path(candidate)
    if candidate_path == target:
        return True
    try:
        return candidate_path.samefile(target)
    except OSError:
        return False


def _same_interpreter(venv_python: Path, current_executable: Path | None = None) -> bool:
    project_venv = venv_python.parent.parent
    runtime_prefix = sys.prefix
    base_prefix = sys.base_prefix
    virtual_env = os.environ.get("VIRTUAL_ENV")

    if _path_matches(runtime_prefix, project_venv):
        return True
    if _path_matches(virtual_env, project_venv):
        return True
    if runtime_prefix != base_prefix and current_executable is not None:
        return project_venv in current_executable.parents
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

    root = _project_root(project_root)
    configure_runtime_logging(root / ".runtime")
    logger.info("Bootstrap starting for %s", root)

    try:
        _require_supported_python(version_info)
        venv_dir = _venv_dir(root)
        venv_python = _venv_python(root)
        requirements_file = root / REQUIREMENTS_FILENAME

        if not requirements_file.exists():
            raise FileNotFoundError(requirements_file)

        if not venv_python.exists():
            logger.info("Creating virtual environment at %s", venv_dir)
            create_venv(venv_dir)

        if not _requirements_are_current(venv_dir, requirements_file):
            logger.info("Installing requirements from %s", requirements_file)
            install_requirements(venv_python, requirements_file)
            _record_requirements_hash(venv_dir, requirements_file)

        if _same_interpreter(venv_python, current_executable=current_executable):
            logger.info("Running app in the current interpreter")
            exit_code = run_app()
            logger.info("App finished with exit code %s", exit_code)
            return exit_code

        logger.info("Handing off to app interpreter at %s", venv_python)
        handoff_to_app(venv_python)
        return 0
    except Exception:
        logger.exception("Bootstrap failed")
        raise


def main() -> int:
    return bootstrap()


if __name__ == "__main__":
    raise SystemExit(main())
