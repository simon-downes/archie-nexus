"""Tests for the TUI protocol version mismatch warning."""

from archie_shared.events import Handshake
from archie_shared.protocol import PROTOCOL_VERSION

from tests.tui_fakes import local_texts, make_controller


def _make_session_info(protocol_version: int, event_id: str = "01J00000000000000000000001"):
    return Handshake(
        id=event_id,
        protocol_version=protocol_version,
        session_id="proj-01abc12345",
    )


def test_matching_version_no_warning():
    """A handshake with the client's protocol version shows no warning."""
    controller, listener = make_controller()
    controller.apply(_make_session_info(protocol_version=PROTOCOL_VERSION))

    assert not any("Protocol version" in e for e in local_texts(controller))
    assert controller.agent_protocol == PROTOCOL_VERSION
    assert listener.of("local") == []


def test_older_session_version_no_warning():
    """A version below the client's shows no warning (backwards compatible)."""
    controller, _ = make_controller()
    # Only meaningful if PROTOCOL_VERSION > 1; otherwise use current version
    if PROTOCOL_VERSION > 1:
        controller.apply(_make_session_info(protocol_version=PROTOCOL_VERSION - 1))
        assert not any("Protocol version" in e for e in local_texts(controller))


def test_newer_session_version_shows_warning():
    """A version above the client's warns once, suggesting an update."""
    controller, listener = make_controller()
    controller.apply(_make_session_info(protocol_version=PROTOCOL_VERSION + 1))

    errors = local_texts(controller)
    assert len(errors) == 1
    assert "Protocol version" in errors[0]
    assert str(PROTOCOL_VERSION + 1) in errors[0]
    assert str(PROTOCOL_VERSION) in errors[0]
    assert "updating" in errors[0].lower() or "update" in errors[0].lower()
    assert listener.of("local") == [(controller.locals[0],)]

    # A reconnect's handshake does not repeat it.
    controller.apply(_make_session_info(PROTOCOL_VERSION + 1, "01J00000000000000000000002"))
    assert len(local_texts(controller)) == 1


def test_protocol_version_warning_does_not_disconnect():
    """A version mismatch only warns; nothing raises and no reconnect starts."""
    controller, _ = make_controller()
    controller.apply(_make_session_info(protocol_version=PROTOCOL_VERSION + 99))

    assert len(controller.locals) == 1
    assert not controller.reconnecting and controller.ws is None
