from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

import pytest

import main


def test_bootstrap_creates_venv_installs_dependencies_and_hands_off(tmp_path: Path) -> None:
    (tmp_path / "requirements.txt").write_text(
        "Telethon>=1.40,<2\nquestionary>=2,<3\n",
        encoding="utf-8",
    )

    calls: list[tuple[str, Path]] = []

    def create_venv(venv_dir: Path) -> None:
        calls.append(("create", venv_dir))
        (venv_dir / "bin").mkdir(parents=True, exist_ok=True)
        (venv_dir / "bin" / "python").write_text("", encoding="utf-8")

    def install_requirements(venv_python: Path, requirements_file: Path) -> None:
        calls.append(("install", venv_python))
        assert requirements_file == tmp_path / "requirements.txt"

    def handoff_to_app(venv_python: Path) -> None:
        calls.append(("handoff", venv_python))

    exit_code = main.bootstrap(
        project_root=tmp_path,
        version_info=(3, 10),
        current_executable=Path("/usr/bin/python3"),
        create_venv=create_venv,
        install_requirements=install_requirements,
        handoff_to_app=handoff_to_app,
        run_app=lambda: 0,
    )

    assert exit_code == 0
    assert calls == [
        ("create", tmp_path / ".venv"),
        ("install", tmp_path / ".venv" / "bin" / "python"),
        ("handoff", tmp_path / ".venv" / "bin" / "python"),
    ]


def test_bootstrap_runs_app_directly_inside_project_venv(tmp_path: Path) -> None:
    requirements_file = tmp_path / "requirements.txt"
    requirements_file.write_text(
        "Telethon>=1.40,<2\nquestionary>=2,<3\n",
        encoding="utf-8",
    )
    venv_python = tmp_path / ".venv" / "bin" / "python"
    venv_python.parent.mkdir(parents=True, exist_ok=True)
    venv_python.write_text("", encoding="utf-8")
    marker = tmp_path / ".venv" / ".requirements.sha256"
    marker.write_text(
        hashlib.sha256(requirements_file.read_bytes()).hexdigest(),
        encoding="utf-8",
    )

    original_prefix = main.sys.prefix
    original_base_prefix = main.sys.base_prefix
    main.sys.prefix = str(tmp_path / ".venv")
    main.sys.base_prefix = "/usr"

    calls: list[str] = []

    def create_venv(_: Path) -> None:
        calls.append("create")

    def install_requirements(_: Path, __: Path) -> None:
        calls.append("install")

    def run_app() -> int:
        calls.append("run")
        return 7

    def handoff_to_app(_: Path) -> None:
        calls.append("handoff")

    try:
        exit_code = main.bootstrap(
            project_root=tmp_path,
            version_info=(3, 10),
            current_executable=Path("/usr/bin/python3"),
            create_venv=create_venv,
            install_requirements=install_requirements,
            run_app=run_app,
            handoff_to_app=handoff_to_app,
        )
    finally:
        main.sys.prefix = original_prefix
        main.sys.base_prefix = original_base_prefix

    assert exit_code == 7
    assert calls == ["run"]


def test_bootstrap_uses_runtime_prefix_instead_of_binary_path_for_venv_detection(
    tmp_path: Path,
) -> None:
    requirements_file = tmp_path / "requirements.txt"
    requirements_file.write_text(
        "Telethon>=1.40,<2\nquestionary>=2,<3\n",
        encoding="utf-8",
    )
    venv_python = tmp_path / ".venv" / "bin" / "python"
    venv_python.parent.mkdir(parents=True, exist_ok=True)
    venv_python.write_text("", encoding="utf-8")
    marker = tmp_path / ".venv" / ".requirements.sha256"
    marker.write_text(
        hashlib.sha256(requirements_file.read_bytes()).hexdigest(),
        encoding="utf-8",
    )

    original_prefix = main.sys.prefix
    original_base_prefix = main.sys.base_prefix
    main.sys.prefix = str(tmp_path / ".venv")
    main.sys.base_prefix = "/usr"

    calls: list[str] = []

    def install_requirements(_: Path, __: Path) -> None:
        calls.append("install")

    def run_app() -> int:
        calls.append("run")
        return 0

    def handoff_to_app(_: Path) -> None:
        calls.append("handoff")

    try:
        exit_code = main.bootstrap(
            project_root=tmp_path,
            version_info=(3, 10),
            current_executable=Path("/usr/bin/python3"),
            install_requirements=install_requirements,
            run_app=run_app,
            handoff_to_app=handoff_to_app,
        )
    finally:
        main.sys.prefix = original_prefix
        main.sys.base_prefix = original_base_prefix

    assert exit_code == 0
    assert calls == ["run"]


def test_bootstrap_rejects_unsupported_python_version(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="Python 3.10 or newer is required"):
        main.bootstrap(project_root=tmp_path, version_info=(3, 9))


def test_bootstrap_skips_dependency_installation_when_marker_matches(
    tmp_path: Path,
) -> None:
    requirements_file = tmp_path / "requirements.txt"
    requirements_file.write_text(
        "Telethon>=1.40,<2\nquestionary>=2,<3\n",
        encoding="utf-8",
    )
    venv_python = tmp_path / ".venv" / "bin" / "python"
    venv_python.parent.mkdir(parents=True, exist_ok=True)
    venv_python.write_text("", encoding="utf-8")
    marker = tmp_path / ".venv" / ".requirements.sha256"
    marker.write_text(
        hashlib.sha256(requirements_file.read_bytes()).hexdigest(),
        encoding="utf-8",
    )

    calls: list[str] = []

    def install_requirements(_: Path, __: Path) -> None:
        calls.append("install")

    def run_app() -> int:
        calls.append("run")
        return 0

    def handoff_to_app(_: Path) -> None:
        calls.append("handoff")

    exit_code = main.bootstrap(
        project_root=tmp_path,
        version_info=(3, 10),
        current_executable=Path("/usr/bin/python3"),
        install_requirements=install_requirements,
        run_app=run_app,
        handoff_to_app=handoff_to_app,
    )

    assert exit_code == 0
    assert calls == ["handoff"]


def test_bootstrap_repairs_missing_pip_and_retries_install(tmp_path: Path) -> None:
    requirements_file = tmp_path / "requirements.txt"
    requirements_file.write_text(
        "Telethon>=1.40,<2\nquestionary>=2,<3\n",
        encoding="utf-8",
    )
    venv_python = tmp_path / ".venv" / "bin" / "python"
    venv_python.parent.mkdir(parents=True, exist_ok=True)
    venv_python.write_text("", encoding="utf-8")

    calls: list[tuple[str, Path]] = []
    attempts = 0

    def install_requirements(python_path: Path, requested_requirements: Path) -> None:
        nonlocal attempts
        calls.append(("install", python_path))
        assert requested_requirements == requirements_file
        attempts += 1
        if attempts == 1:
            raise subprocess.CalledProcessError(
                1,
                [str(python_path), "-m", "pip", "install", "-r", str(requested_requirements)],
                stderr="/tmp/project/.venv/bin/python: No module named pip",
            )

    def ensure_pip(python_path: Path) -> None:
        calls.append(("ensure_pip", python_path))

    def handoff_to_app(python_path: Path) -> None:
        calls.append(("handoff", python_path))

    exit_code = main.bootstrap(
        project_root=tmp_path,
        version_info=(3, 10),
        current_executable=Path("/usr/bin/python3"),
        install_requirements=install_requirements,
        ensure_pip=ensure_pip,
        handoff_to_app=handoff_to_app,
        run_app=lambda: 0,
    )

    assert exit_code == 0
    assert calls == [
        ("install", venv_python),
        ("ensure_pip", venv_python),
        ("install", venv_python),
        ("handoff", venv_python),
    ]
