"""Test helper for host config edits that agent code should see (plan 090)."""

from pathlib import Path

from archie_agent import host_config
from archie_shared.config import ConfigError


def write_host_config(home: Path, text: str) -> None:
    """Write ``home/config.yaml`` and sync the agent's copy, as its periodic sync would."""
    (home / "config.yaml").write_text(text)
    try:
        host_config.current().sync()
    except ConfigError:
        pass  # nothing held yet: the next read starts from the file
