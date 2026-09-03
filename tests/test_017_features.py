"""Tests for canonical live events."""

import pytest
from archie_shared.events import ErrorNotice, Handshake, SessionStatus, decode_event, encode_event


def test_live_event_round_trip():
    event = ErrorNotice(
        id="01J00000000000000000000001",
        kind="turn_active",
        message="Turn already active",
    )
    restored = decode_event(encode_event(event))
    assert restored == event


def test_handshake_contains_only_connection_metadata():
    event = Handshake(
        id="01J00000000000000000000001",
        protocol_version=2,
        session_id="test-abc",
    )
    raw = encode_event(event)
    assert "total_cost" not in raw
    assert "latest_event_id" not in raw
    assert decode_event(raw) == event


def test_session_status_is_canonical():
    status = SessionStatus(id="01J00000000000000000000001", model_key="haiku", git_branch="main")
    assert decode_event(encode_event(status)) == status
    assert status.model_key == "haiku"


def test_agent_shell_route_and_event_tag_are_removed():
    from archie_agent.app import app

    assert all(getattr(route, "path", None) != "/shell" for route in app.routes)
    with pytest.raises((ValueError, TypeError)):
        decode_event(
            '{"type":"shell_command","id":"01J00000000000000000000001",'
            '"command":"pwd","exit_code":0,"output":""}'
        )
