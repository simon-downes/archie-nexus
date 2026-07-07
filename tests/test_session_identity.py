"""Tests for session identity module."""

from archie_shared.session.identity import (
    CONTAINER_PREFIX,
    container_name,
    generate_session_id,
    parse_container_name,
    split_id,
)


def test_generate_session_id_format():
    """ID is {project}-{10_char_ulid_lowercase}."""
    sid = generate_session_id("my-project")
    parts = sid.rsplit("-", 1)
    assert parts[0] == "my-project"
    assert len(parts[1]) == 10
    assert parts[1] == parts[1].lower()


def test_generate_session_id_unique():
    """Calls at different timestamps produce different IDs."""
    import time

    id1 = generate_session_id("test")
    time.sleep(0.002)  # >1ms to ensure different ULID timestamp
    id2 = generate_session_id("test")
    assert id1 != id2


def test_container_name_build():
    """container_name prefixes with CONTAINER_PREFIX."""
    assert container_name("proj-01j3abcdef") == "archie-proj-01j3abcdef"


def test_parse_container_name_valid():
    """Valid archie container name → session_id."""
    sid = generate_session_id("my-app")
    cname = container_name(sid)
    assert parse_container_name(cname) == sid


def test_parse_container_name_invalid():
    """Non-archie container name → None."""
    assert parse_container_name("nginx-proxy") is None
    assert parse_container_name("archie-") is None
    assert parse_container_name("archie-too-short") is None


def test_parse_container_name_hyphenated_project():
    """Project with hyphens parses correctly (greedy on project)."""
    # Simulate: project="my-cool-app", ulid="01j3abcdef"
    cname = "archie-my-cool-app-01j3abcdef"
    sid = parse_container_name(cname)
    assert sid == "my-cool-app-01j3abcdef"


def test_roundtrip_generate_container_parse():
    """generate → container_name → parse_container_name round-trips."""
    sid = generate_session_id("archie-nexus")
    cname = container_name(sid)
    parsed = parse_container_name(cname)
    assert parsed == sid


def test_split_id_simple():
    """split_id separates project from ulid prefix."""
    project, ulid_prefix = split_id("my-project-01j3abcdef")
    assert project == "my-project"
    assert ulid_prefix == "01j3abcdef"


def test_split_id_hyphenated_project():
    """Hyphenated project name splits correctly."""
    project, ulid_prefix = split_id("my-cool-app-01j3abcdef")
    assert project == "my-cool-app"
    assert ulid_prefix == "01j3abcdef"
    assert len(ulid_prefix) == 10


def test_container_prefix():
    """CONTAINER_PREFIX is 'archie-'."""
    assert CONTAINER_PREFIX == "archie-"
