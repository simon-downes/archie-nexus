"""Tests for the session record (SessionDescriptor) and typed payloads."""

import msgspec
import pytest
from archie_shared.events import PendingQuestion, QuestionOption
from archie_shared.session.descriptor import TERMINAL_STATUSES, SessionDescriptor, StatusPayload


def test_session_descriptor_defaults_and_container_name():
    """Required fields first; optional ones default; container_name is computed."""
    desc = SessionDescriptor(
        session_id="my-app-01j3abcdef",
        workspace="my-app",
        backend="native",
        name="my-app-01j3abcdef",
        status="idle",
        started_at="2026-07-01T10:00:00+00:00",
    )
    assert desc.container_name == "archie-my-app-01j3abcdef"
    assert (desc.name_source, desc.unseen, desc.ended) == ("id", False, False)
    assert desc.error is desc.question is desc.model_key is None
    assert desc.last_activity_at is desc.ended_at is None


@pytest.mark.parametrize("status", sorted(TERMINAL_STATUSES))
def test_ended_session_has_no_container(status):
    desc = SessionDescriptor(
        session_id="test-01j3abcdef",
        workspace="test",
        backend="kiro",
        name="test-01j3abcdef",
        status=status,
        started_at="2026-07-01T10:00:00+00:00",
        ended_at="2026-07-01T11:00:00+00:00",
        container_name="archie-test-01j3abcdef",
    )
    assert desc.ended
    assert desc.container_name is None


def test_terminal_statuses():
    assert TERMINAL_STATUSES == {"stopped", "exited", "failed"}


def test_session_descriptor_json_round_trip_recomputes_container_name():
    desc = SessionDescriptor(
        session_id="p-01j3abcdef",
        workspace="p",
        backend="native",
        name="Named",
        status="blocked",
        started_at="2026-07-01T10:00:00+00:00",
        name_source="user",
        question=PendingQuestion(id="q", question="?", options=[QuestionOption("a")]),
    )
    data = msgspec.json.decode(msgspec.json.encode(desc))
    assert list(data)[:6] == ["session_id", "workspace", "backend", "name", "status", "started_at"]
    assert data["container_name"] == "archie-p-01j3abcdef"
    data["container_name"] = "ignored"
    decoded = msgspec.json.decode(msgspec.json.encode(data), type=SessionDescriptor)
    assert decoded == desc
    with pytest.raises(msgspec.ValidationError):
        msgspec.json.decode(msgspec.json.encode({**data, "status": "done"}), type=SessionDescriptor)


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
