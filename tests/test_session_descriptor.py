"""Tests for session descriptor and typed payloads."""

import msgspec
from archie_shared.session.descriptor import SessionDescriptor, StatusPayload


def test_session_descriptor_construction():
    """SessionDescriptor holds running session info."""
    desc = SessionDescriptor(
        session_id="my-app-01j3abcdef",
        container_name="archie-my-app-01j3abcdef",
        port=32771,
        raw_docker_status="Up 5 minutes",
    )
    assert desc.session_id == "my-app-01j3abcdef"
    assert desc.port == 32771


def test_session_descriptor_port_none():
    """Port can be None (not yet published)."""
    desc = SessionDescriptor(
        session_id="test-01j3abcdef",
        container_name="archie-test-01j3abcdef",
    )
    assert desc.port is None
    assert desc.raw_docker_status == ""


def test_status_payload_ok():
    """StatusPayload for healthy agent."""
    payload = StatusPayload(
        status="ok",
        model="bedrock-claude-sonnet-4-6",
        session_id="test-01j3abc",
        turn_count=5,
        turn_active=False,
    )
    encoded = msgspec.json.encode(payload)
    decoded = msgspec.json.decode(encoded, type=StatusPayload)
    assert decoded.status == "ok"
    assert decoded.turn_count == 5


def test_status_payload_starting():
    """StatusPayload for starting state (minimal fields)."""
    payload = StatusPayload(status="starting")
    encoded = msgspec.json.encode(payload)
    decoded = msgspec.json.decode(encoded, type=StatusPayload)
    assert decoded.status == "starting"
    assert decoded.model is None
    assert decoded.turn_count is None
