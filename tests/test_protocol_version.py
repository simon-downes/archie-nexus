"""Tests for TUI protocol version mismatch warning."""

from unittest.mock import MagicMock

from archie_shared.canonical_events import Handshake
from archie_shared.events import PROTOCOL_VERSION


def _make_app():
    from archie_cli.tui.app import ArchieApp

    return ArchieApp(
        ws_url="ws://127.0.0.1:7600/sessions/proj-01abc/stream",
        api_url="http://127.0.0.1:7600/sessions/proj-01abc",
        container_name="archie-proj-01abc",
    )


def _make_session_info(protocol_version: int) -> Handshake:
    return Handshake(
        id="01J00000000000000000000001",
        protocol_version=protocol_version,
        model_key="bedrock-claude-sonnet-4-6",
        session_id="proj-01abc12345",
    )


def test_matching_version_no_warning():
    """SessionInfo with matching protocol version → no warning shown."""
    app = _make_app()
    errors = []
    app._show_client_error = lambda msg: errors.append(msg)

    # Mock query_one to return a fake StatusBar
    mock_status = MagicMock()
    app.query_one = MagicMock(return_value=mock_status)

    event = _make_session_info(protocol_version=PROTOCOL_VERSION)
    app._handle_event(event)

    assert not any("Protocol version" in e for e in errors)


def test_older_session_version_no_warning():
    """SessionInfo with version < client version → no warning (backwards compat)."""
    app = _make_app()
    errors = []
    app._show_client_error = lambda msg: errors.append(msg)
    app.query_one = MagicMock(return_value=MagicMock())

    event = _make_session_info(protocol_version=max(1, PROTOCOL_VERSION - 1))
    # Only meaningful if PROTOCOL_VERSION > 1; otherwise use current version
    if PROTOCOL_VERSION > 1:
        app._handle_event(event)
        assert not any("Protocol version" in e for e in errors)


def test_newer_session_version_shows_warning():
    """SessionInfo with version > client version → warning with update suggestion."""
    app = _make_app()
    errors = []
    app._show_client_error = lambda msg: errors.append(msg)
    app.query_one = MagicMock(return_value=MagicMock())

    event = _make_session_info(protocol_version=PROTOCOL_VERSION + 1)
    app._handle_event(event)

    assert len(errors) == 1
    assert "Protocol version" in errors[0]
    assert str(PROTOCOL_VERSION + 1) in errors[0]
    assert str(PROTOCOL_VERSION) in errors[0]
    assert "updating" in errors[0].lower() or "update" in errors[0].lower()


def test_protocol_version_warning_does_not_disconnect():
    """Version mismatch → warning shown but connection continues (no exception)."""
    app = _make_app()
    app._show_client_error = MagicMock()
    app.query_one = MagicMock(return_value=MagicMock())

    # Should not raise
    event = _make_session_info(protocol_version=PROTOCOL_VERSION + 99)
    app._handle_event(event)

    app._show_client_error.assert_called_once()
