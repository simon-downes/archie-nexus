"""Archie orchestrator — host-side control plane."""

import logging
import os


def configure_logging() -> None:
    """Configure logging for the orchestrator process.

    Reads LOG_LEVEL env var (default: INFO). Format matches uvicorn's output style.
    Safe to call multiple times — basicConfig is a no-op if handlers are already set.
    """
    raw = os.environ.get("LOG_LEVEL", "INFO")
    level = getattr(logging, raw.upper(), None)
    unknown = not isinstance(level, int)
    if unknown:
        level = logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-8s %(name)s  %(message)s",
        datefmt="%H:%M:%S",
    )
    if unknown:
        logging.warning(
            "Unknown LOG_LEVEL %r — defaulting to INFO. "
            "Valid values: DEBUG, INFO, WARNING, ERROR, CRITICAL.",
            raw,
        )
