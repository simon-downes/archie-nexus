"""Archie orchestrator — host-side control plane."""

import logging
import os
import time


_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"
_LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


class UTCFormatter(logging.Formatter):
    """Format log timestamps as UTC regardless of the host timezone."""

    converter = time.gmtime


def configure_logging() -> None:
    """Configure consistent logging for the orchestrator process.

    Reads LOG_LEVEL env var (default: INFO). Safe to call multiple times and
    updates handlers installed by Uvicorn as well as the root handler.
    """
    raw = os.environ.get("LOG_LEVEL", "INFO")
    level = getattr(logging, raw.upper(), None)
    unknown = not isinstance(level, int)
    if unknown:
        level = logging.INFO

    root = logging.getLogger()
    if not root.handlers:
        logging.basicConfig(level=level)
    else:
        root.setLevel(level)

    formatter = UTCFormatter(_LOG_FORMAT, datefmt=_LOG_DATE_FORMAT)
    handlers = list(root.handlers)
    for logger in logging.Logger.manager.loggerDict.values():
        if isinstance(logger, logging.Logger):
            handlers.extend(logger.handlers)
    for handler in dict.fromkeys(handlers):
        handler.setFormatter(formatter)

    if unknown:
        logging.warning(
            "Unknown LOG_LEVEL %r — defaulting to INFO. "
            "Valid values: DEBUG, INFO, WARNING, ERROR, CRITICAL.",
            raw,
        )
