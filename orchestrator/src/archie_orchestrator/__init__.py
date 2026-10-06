"""Archie orchestrator — host-side control plane."""

import logging
import os
import time
from pathlib import Path

from archie_shared.config import home_dir

_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"
_LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
_HANDLER_MARKER = "archie_orchestrator_managed"
_ROTATED_HOME: Path | None = None

# ANSI colours are deliberately retained in log files so they remain useful when
# viewed interactively with a pager or tail -f.
_RESET = "\033[0m"
_LEVEL_COLOURS = {
    "DEBUG": "\033[36m",
    "INFO": "\033[32m",
    "WARNING": "\033[33m",
    "ERROR": "\033[31m",
    "CRITICAL": "\033[1;31m",
}
_SERVICE_COLOURS = {
    "archie_orchestrator.app": "\033[35m",  # magenta
    "archie_orchestrator.auth": "\033[34m",  # blue
    "archie_orchestrator.credential_refresh": "\033[36m",  # cyan
    "archie_orchestrator.docker": "\033[33m",  # yellow
    "archie_orchestrator.lifecycle": "\033[32m",  # green
    "archie_orchestrator.metrics": "\033[31m",  # red
    "archie_orchestrator.proxy": "\033[95m",  # bright magenta
    "httpx": "\033[92m",  # bright green
    "uvicorn.access": "\033[96m",  # bright cyan
    "uvicorn.error": "\033[91m",  # bright red
}
_DEFAULT_SERVICE_COLOUR = "\033[37m"  # white


class UTCFormatter(logging.Formatter):
    """Format log timestamps as UTC regardless of the host timezone."""

    converter = time.gmtime


class OrchestratorFormatter(UTCFormatter):
    """Format the level and logger/service name with ANSI colours."""

    def __init__(self, *, colour: bool) -> None:
        super().__init__(_LOG_FORMAT, datefmt=_LOG_DATE_FORMAT)
        self.colour = colour

    def format(self, record: logging.LogRecord) -> str:
        rendered = super().format(record)
        if not self.colour:
            return rendered
        level = f"{_LEVEL_COLOURS.get(record.levelname, '')}{record.levelname}{_RESET}"
        service_colour = _SERVICE_COLOURS.get(record.name, _DEFAULT_SERVICE_COLOUR)
        service = f"{service_colour}{record.name}{_RESET}"
        return rendered.replace(record.levelname, level, 1).replace(record.name, service, 1)


class _AccessFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return record.name == "uvicorn.access" or record.name.startswith("uvicorn.access.")


class _NonAccessFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return not (record.name == "uvicorn.access" or record.name.startswith("uvicorn.access."))


def _rotate(path: Path) -> None:
    """Move one existing log aside, replacing the previous one-generation backup."""
    if path.exists():
        path.replace(path.with_name(f"{path.name}.1"))


def _managed_handler(root: logging.Logger, filename: str) -> logging.Handler | None:
    for handler in root.handlers:
        if (
            getattr(handler, _HANDLER_MARKER, False)
            and getattr(handler, "_log_filename", None) == filename
        ):
            return handler
    return None


def configure_logging() -> None:
    """Configure terminal, general/error, and HTTP access logging.

    Reads ``LOG_LEVEL`` (default ``INFO``). Log files are created below
    ``<ARCHIE_HOME_DIR>/logs`` and existing files are rotated once at process
    startup to a single ``.1`` backup. Safe to call multiple times.
    """
    global _ROTATED_HOME

    raw = os.environ.get("LOG_LEVEL", "INFO")
    level = getattr(logging, raw.upper(), None)
    unknown = not isinstance(level, int)
    if unknown:
        level = logging.INFO

    root = logging.getLogger()
    root.setLevel(level)
    log_dir = home_dir() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    error_path = log_dir / "orchestrator.error.log"
    access_path = log_dir / "orchestrator.access.log"

    if _ROTATED_HOME != log_dir:
        _rotate(error_path)
        _rotate(access_path)
        _ROTATED_HOME = log_dir

    # Preserve handlers installed by pytest or embedding applications, but use
    # our formatter on them. The managed handlers below are not duplicated on
    # subsequent calls (CLI invokes this before Uvicorn and lifespan invokes it).
    terminal = next(
        (
            h
            for h in root.handlers
            if getattr(h, _HANDLER_MARKER, False) and getattr(h, "_log_terminal", False)
        ),
        None,
    )
    if terminal is None:
        terminal = logging.StreamHandler()
        terminal.archie_orchestrator_managed = True
        terminal._log_terminal = True
        root.addHandler(terminal)
    terminal.setLevel(level)
    terminal.setFormatter(OrchestratorFormatter(colour=True))
    for filter_ in list(terminal.filters):
        if isinstance(filter_, _NonAccessFilter):
            terminal.removeFilter(filter_)

    error_handler = _managed_handler(root, error_path.name)
    if error_handler is None:
        error_handler = logging.FileHandler(error_path, encoding="utf-8")
        error_handler.archie_orchestrator_managed = True
        error_handler._log_filename = error_path.name
        root.addHandler(error_handler)
    error_handler.setLevel(level)
    error_handler.setFormatter(OrchestratorFormatter(colour=True))
    if not any(isinstance(f, _NonAccessFilter) for f in error_handler.filters):
        error_handler.addFilter(_NonAccessFilter())

    access_logger = logging.getLogger("uvicorn.access")
    access_logger.setLevel(level)
    access_logger.propagate = True
    access_handler = next(
        (h for h in access_logger.handlers if getattr(h, _HANDLER_MARKER, False)), None
    )
    if access_handler is None:
        access_handler = logging.FileHandler(access_path, encoding="utf-8")
        access_handler.archie_orchestrator_managed = True
        access_logger.addHandler(access_handler)
    access_handler.setLevel(level)
    access_handler.setFormatter(OrchestratorFormatter(colour=True))
    if not any(isinstance(f, _AccessFilter) for f in access_handler.filters):
        access_handler.addFilter(_AccessFilter())

    # Keep existing application handlers consistently formatted as well.
    for handler in root.handlers:
        if handler is not terminal and handler is not error_handler:
            handler.setFormatter(UTCFormatter(_LOG_FORMAT, datefmt=_LOG_DATE_FORMAT))
    for logger in logging.Logger.manager.loggerDict.values():
        if isinstance(logger, logging.Logger):
            for handler in logger.handlers:
                if handler is not access_handler:
                    handler.setFormatter(OrchestratorFormatter(colour=False))

    if unknown:
        logging.warning(
            "Unknown LOG_LEVEL %r — defaulting to INFO. Valid values: DEBUG, INFO, WARNING, CRITICAL.",
            raw,
        )
