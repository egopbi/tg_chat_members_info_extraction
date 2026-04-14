"""Application package entrypoint."""

from __future__ import annotations

import logging
from pathlib import Path

from .runtime_logging import configure_runtime_logging

_logger = logging.getLogger(__name__)
_PROJECT_ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    runtime_log = configure_runtime_logging(_PROJECT_ROOT / ".runtime")
    _logger.info("Application startup")
    _logger.debug("Runtime log file: %s", runtime_log)
    try:
        from .ui import main as _ui_main

        exit_code = _ui_main()
    except Exception:
        _logger.exception("Application crashed")
        return 1
    _logger.info("Application finished with exit code %s", exit_code)
    return exit_code

__all__ = ["main"]
