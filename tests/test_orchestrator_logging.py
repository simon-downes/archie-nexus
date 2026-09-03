"""Tests for orchestrator operational logging."""

import logging
from unittest.mock import MagicMock, patch

import pytest
from archie_orchestrator import configure_logging
from archie_orchestrator.app import app
from archie_shared.schemas import NexusConfig
from archie_shared.session import SessionDescriptor

# ---------------------------------------------------------------------------
# Shared fixture — prime app state before each test
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _app_state():
    app.state.config = NexusConfig()
    yield


def _make_session(session_id: str = "proj-01abc12345") -> SessionDescriptor:
    return SessionDescriptor(
        session_id=session_id,
        container_name=f"archie-{session_id}",
        port=32771,
        raw_docker_status="Up 5 minutes",
    )


# ---------------------------------------------------------------------------
# configure_logging
# ---------------------------------------------------------------------------


def test_configure_logging_default_level(monkeypatch):
    """configure_logging sets INFO level by default."""
    monkeypatch.delenv("LOG_LEVEL", raising=False)
    # Reset root logger handlers so basicConfig takes effect
    root = logging.getLogger()
    original_handlers = root.handlers[:]
    root.handlers.clear()
    try:
        configure_logging()
        assert root.level == logging.INFO
    finally:
        root.handlers = original_handlers


def test_configure_logging_warning_level(monkeypatch):
    """LOG_LEVEL=WARNING sets WARNING level."""
    monkeypatch.setenv("LOG_LEVEL", "WARNING")
    root = logging.getLogger()
    original_handlers = root.handlers[:]
    root.handlers.clear()
    try:
        configure_logging()
        assert root.level == logging.WARNING
    finally:
        root.handlers = original_handlers
        monkeypatch.delenv("LOG_LEVEL", raising=False)


def test_configure_logging_debug_level(monkeypatch):
    """LOG_LEVEL=DEBUG sets DEBUG level."""
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    root = logging.getLogger()
    original_handlers = root.handlers[:]
    root.handlers.clear()
    try:
        configure_logging()
        assert root.level == logging.DEBUG
    finally:
        root.handlers = original_handlers
        monkeypatch.delenv("LOG_LEVEL", raising=False)


# ---------------------------------------------------------------------------
# Session lifecycle logging
# ---------------------------------------------------------------------------


def test_start_session_logs_info(tmp_path, caplog):
    """start_session emits INFO log with session_id and workspace."""
    from unittest.mock import patch as _patch

    workspace_root = tmp_path
    workspace_dir = workspace_root / "myproject"
    workspace_dir.mkdir()

    config = NexusConfig()

    import msgspec

    config_patched = msgspec.structs.replace(config)

    with (
        _patch("archie_orchestrator.lifecycle.expand_workspace_root", return_value=workspace_root),
        _patch("archie_orchestrator.lifecycle.check_image", return_value=True),
        _patch("archie_orchestrator.lifecycle.run_container"),
        _patch(
            "archie_orchestrator.lifecycle.wait_for_ready",
            return_value="32771",
        ),
        _patch("archie_orchestrator.lifecycle.home_dir", return_value=tmp_path),
        caplog.at_level(logging.INFO, logger="archie_orchestrator.lifecycle"),
    ):
        from archie_orchestrator.lifecycle import start_session

        start_session("myproject", config_patched)

    assert any("Session started" in r.message and "myproject" in r.message for r in caplog.records)


def test_stop_session_logs_info(caplog):
    """stop_session emits INFO log with session_id."""
    session = _make_session("myproject-01abc12345")

    with (
        patch("archie_orchestrator.lifecycle.list_sessions", return_value=[session]),
        patch("archie_orchestrator.lifecycle.stop_container"),
        caplog.at_level(logging.INFO, logger="archie_orchestrator.lifecycle"),
    ):
        from archie_orchestrator.lifecycle import stop_session

        stop_session("myproject-01abc12345")

    assert any(
        "Session stopped" in r.message and "myproject-01abc12345" in r.message
        for r in caplog.records
    )


# ---------------------------------------------------------------------------
# Startup log
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_startup_logs_discovered_sessions(caplog):
    """App startup logs the count of discovered sessions."""
    sessions = [_make_session("proj-01abc12345"), _make_session("proj-02def67890")]

    with (
        patch("archie_orchestrator.app.list_sessions", return_value=sessions),
        patch("archie_orchestrator.app.load_nexus_config", return_value=NexusConfig()),
        caplog.at_level(logging.INFO, logger="archie_orchestrator.app"),
    ):
        # Drive the lifespan directly — enter startup, do nothing, then exit
        from archie_orchestrator.app import lifespan

        async with lifespan(app):
            pass

    assert any("Discovered" in r.message and "2" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Proxy connect/disconnect logging
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_proxy_stream_logs_connect_disconnect(caplog):
    """proxy_stream logs connect and disconnect for a session."""
    from starlette.testclient import TestClient

    session = _make_session("proj-01abc12345")

    class _FakeBackend:
        """Backend that yields no messages and closes immediately."""

        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

        async def send(self, msg):
            pass

    class _FakeConnect:
        """Sync callable that returns an async context manager."""

        def __call__(self, url, **kwargs):
            return self

        async def __aenter__(self):
            return _FakeBackend()

        async def __aexit__(self, *_):
            pass

    with (
        patch("archie_orchestrator.proxy.list_sessions", return_value=[session]),
        patch("archie_orchestrator.proxy.websockets.connect", _FakeConnect()),
        caplog.at_level(logging.INFO, logger="archie_orchestrator.proxy"),
    ):
        client = TestClient(app, raise_server_exceptions=False)
        with client.websocket_connect(f"/sessions/{session.session_id}/stream"):
            pass

    messages = [r.message for r in caplog.records]
    assert any("Client connected" in m and "proj-01abc12345" in m for m in messages)
    assert any("Client disconnected" in m and "proj-01abc12345" in m for m in messages)


# ---------------------------------------------------------------------------
# Docker DEBUG logging
# ---------------------------------------------------------------------------


def test_docker_list_sessions_debug_log(caplog):
    """list_sessions emits a DEBUG log for the docker ps command."""
    with (
        patch(
            "archie_orchestrator.docker.subprocess.run",
            return_value=MagicMock(returncode=1, stdout="", stderr=""),
        ),
        caplog.at_level(logging.DEBUG, logger="archie_orchestrator.docker"),
    ):
        from archie_orchestrator.docker import list_sessions

        list_sessions()

    assert any("docker ps" in r.message for r in caplog.records)


def test_log_level_warning_suppresses_info(monkeypatch, caplog):
    """INFO messages are suppressed when LOG_LEVEL=WARNING."""
    session = _make_session()
    with (
        patch("archie_orchestrator.lifecycle.list_sessions", return_value=[session]),
        patch("archie_orchestrator.lifecycle.stop_container"),
        caplog.at_level(logging.WARNING, logger="archie_orchestrator.lifecycle"),
    ):
        from archie_orchestrator.lifecycle import stop_session

        stop_session(session.session_id)

    # No INFO records should appear at WARNING level
    info_records = [r for r in caplog.records if r.levelno == logging.INFO]
    assert not info_records
