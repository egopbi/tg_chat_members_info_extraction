"""Runtime logging helpers for bootstrap and application code."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

RUNTIME_LOG_FILENAME = "runtime.log"
_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
_LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
_RUNTIME_HANDLER_ATTR = "_runtime_log_path"
_excepthook_installed = False


def runtime_log_path(runtime_dir: Path | str = ".runtime") -> Path:
    return Path(runtime_dir) / RUNTIME_LOG_FILENAME


def _best_effort_chmod(path: Path, mode: int) -> None:
    try:
        path.chmod(mode)
    except OSError:
        pass


def _install_unhandled_exception_hook() -> None:
    global _excepthook_installed
    if _excepthook_installed:
        return

    previous_hook = sys.excepthook

    def hook(exc_type: type[BaseException], exc_value: BaseException, exc_tb) -> None:
        if issubclass(exc_type, KeyboardInterrupt):
            previous_hook(exc_type, exc_value, exc_tb)
            return
        logging.getLogger(__name__).critical(
            "Unhandled exception",
            exc_info=(exc_type, exc_value, exc_tb),
        )
        previous_hook(exc_type, exc_value, exc_tb)

    sys.excepthook = hook
    _excepthook_installed = True


def configure_runtime_logging(runtime_dir: Path | str = ".runtime") -> Path:
    runtime_dir = Path(runtime_dir)
    runtime_dir.mkdir(parents=True, exist_ok=True)
    _best_effort_chmod(runtime_dir, 0o700)

    log_path = runtime_log_path(runtime_dir)
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)

    for handler in list(root_logger.handlers):
        handler_path = getattr(handler, _RUNTIME_HANDLER_ATTR, None)
        if handler_path is None:
            continue
        if Path(handler_path) != log_path:
            root_logger.removeHandler(handler)
            handler.close()

    if not any(
        Path(getattr(handler, _RUNTIME_HANDLER_ATTR)) == log_path
        for handler in root_logger.handlers
        if getattr(handler, _RUNTIME_HANDLER_ATTR, None) is not None
    ):
        file_handler = logging.FileHandler(log_path, encoding="utf-8")
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(
            logging.Formatter(_LOG_FORMAT, datefmt=_LOG_DATE_FORMAT)
        )
        setattr(file_handler, _RUNTIME_HANDLER_ATTR, log_path)
        root_logger.addHandler(file_handler)

    _best_effort_chmod(log_path, 0o600)
    _install_unhandled_exception_hook()
    logging.getLogger(__name__).debug("Runtime logging configured at %s", log_path)
    return log_path
