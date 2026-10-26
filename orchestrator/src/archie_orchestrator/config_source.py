"""Re-read ``config.yaml`` on use, so edits apply without restarting the orchestrator.

The parsed config is cached against the file's stat signature (mtime, size,
inode), so repeated reads cost one ``stat`` until the file changes. The path is
resolved from ``ARCHIE_HOME_DIR`` on every call.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

from archie_shared.config import ConfigError, home_dir
from archie_shared.schemas import NexusConfig, load_nexus_config

log = logging.getLogger(__name__)

_Signature = tuple[str, int, int, int] | tuple[str, None]


def _signature(path: Path) -> _Signature:
    try:
        st = path.stat()
    except FileNotFoundError:
        return (str(path), None)
    return (str(path), st.st_mtime_ns, st.st_size, st.st_ino)


class ConfigSource:
    """Load the Nexus config on demand, reloading when the file changes."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._signature: _Signature | None = None
        self._config: NexusConfig | None = None
        self._last_valid: NexusConfig | None = None
        self._warned: _Signature | None = None

    def _resolve(self) -> Path:
        return self._path if self._path is not None else home_dir() / "config.yaml"

    def get(self) -> NexusConfig:
        """Return the current config; raise ``ConfigError`` if the file is invalid."""
        path = self._resolve()
        signature = _signature(path)
        with self._lock:
            if signature == self._signature and self._config is not None:
                return self._config
        # A missing default file yields defaults; an explicit missing path is an error.
        config = load_nexus_config(path if self._path is not None else None)
        with self._lock:
            if self._signature is not None and signature != self._signature:
                log.info("Reloaded %s", path)
            self._signature = signature
            self._config = config
            self._last_valid = config
            self._warned = None
        return config

    def latest(self) -> NexusConfig:
        """Return the current config, or the last valid one if the file is invalid.

        Logs one warning per invalid file version. Raises ``ConfigError`` only
        when no valid config has been loaded yet.
        """
        try:
            return self.get()
        except ConfigError as exc:
            with self._lock:
                last_valid = self._last_valid
                signature = _signature(self._resolve())
                first_warning = signature != self._warned
                self._warned = signature
            if last_valid is None:
                raise
            if first_warning:
                log.warning("Invalid config; using the last valid config. %s", exc)
            return last_valid


config_source = ConfigSource()
