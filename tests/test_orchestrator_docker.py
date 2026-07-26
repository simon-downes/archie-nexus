"""Tests for archie_orchestrator.docker — session discovery via docker subprocess."""

import json
from unittest.mock import MagicMock, patch

from archie_orchestrator.docker import list_sessions


def _make_run_result(stdout: str = "", returncode: int = 0) -> MagicMock:
    """Helper to build a mock subprocess.CompletedProcess."""
    mock = MagicMock()
    mock.returncode = returncode
    mock.stdout = stdout
    return mock


def _ps_line(name: str, status: str = "Up 2 hours") -> str:
    """Build a JSON line as docker ps --format '{{json .}}' would emit."""
    return json.dumps({"Names": name, "Status": status})


def _port_result(port: str | None) -> MagicMock:
    """Mock result for docker port query."""
    if port is None:
        return _make_run_result(stdout="", returncode=1)
    return _make_run_result(stdout=f"127.0.0.1:{port}\n", returncode=0)


# --- Tests ---


def test_list_sessions_two_archie_containers():
    """Two archie containers in docker ps → two SessionDescriptors."""
    ps_output = "\n".join(
        [
            _ps_line("archie-myproject-01abc12345", "Up 3 hours"),
            _ps_line("archie-otherapp-01def67890", "Up 1 hour"),
        ]
    )

    port_results = {
        "archie-myproject-01abc12345": _port_result("32771"),
        "archie-otherapp-01def67890": _port_result("32772"),
    }

    def mock_run(cmd, **kwargs):
        if cmd[0:2] == ["docker", "ps"]:
            return _make_run_result(stdout=ps_output)
        # docker port <name> <port>
        name = cmd[2]
        return port_results[name]

    with patch("archie_orchestrator.docker.subprocess.run", side_effect=mock_run):
        sessions = list_sessions()

    assert len(sessions) == 2
    by_id = {s.session_id: s for s in sessions}

    assert "myproject-01abc12345" in by_id
    assert by_id["myproject-01abc12345"].port == 32771
    assert by_id["myproject-01abc12345"].container_name == "archie-myproject-01abc12345"
    assert by_id["myproject-01abc12345"].raw_docker_status == "Up 3 hours"

    assert "otherapp-01def67890" in by_id
    assert by_id["otherapp-01def67890"].port == 32772


def test_list_sessions_empty_docker_ps():
    """Empty docker ps output → returns empty list."""
    with patch(
        "archie_orchestrator.docker.subprocess.run",
        return_value=_make_run_result(stdout=""),
    ):
        sessions = list_sessions()
    assert sessions == []


def test_list_sessions_non_archie_containers_filtered():
    """Containers not matching archie name pattern are excluded."""
    ps_output = "\n".join(
        [
            _ps_line("archie-myproject-01abc12345", "Up 1 hour"),
            _ps_line("postgres-db", "Up 2 days"),
            _ps_line("nginx-proxy", "Up 1 week"),
        ]
    )

    def mock_run(cmd, **kwargs):
        if cmd[0:2] == ["docker", "ps"]:
            return _make_run_result(stdout=ps_output)
        # docker port — only called for the archie container
        return _port_result("32771")

    with patch("archie_orchestrator.docker.subprocess.run", side_effect=mock_run):
        sessions = list_sessions()

    assert len(sessions) == 1
    assert sessions[0].session_id == "myproject-01abc12345"


def test_list_sessions_port_none_when_not_published():
    """Container with no published port → port is None."""
    ps_output = _ps_line("archie-myproject-01abc12345", "Up 30 seconds")

    def mock_run(cmd, **kwargs):
        if cmd[0:2] == ["docker", "ps"]:
            return _make_run_result(stdout=ps_output)
        return _port_result(None)

    with patch("archie_orchestrator.docker.subprocess.run", side_effect=mock_run):
        sessions = list_sessions()

    assert len(sessions) == 1
    assert sessions[0].port is None


def test_list_sessions_docker_ps_failure():
    """docker ps returning non-zero → returns empty list."""
    with patch(
        "archie_orchestrator.docker.subprocess.run",
        return_value=_make_run_result(stdout="", returncode=1),
    ):
        sessions = list_sessions()
    assert sessions == []


def test_list_sessions_malformed_json_line():
    """docker ps output containing a malformed JSON line → that line is skipped."""
    ps_output = "\n".join(
        [
            _ps_line("archie-myproject-01abc12345", "Up 1 hour"),
            "this is not json",
            _ps_line("archie-otherapp-01def67890", "Up 2 hours"),
        ]
    )

    def mock_run(cmd, **kwargs):
        if cmd[0:2] == ["docker", "ps"]:
            return _make_run_result(stdout=ps_output)
        return _port_result("32771")

    with patch("archie_orchestrator.docker.subprocess.run", side_effect=mock_run):
        sessions = list_sessions()

    # Malformed line skipped; valid archie containers still returned
    assert len(sessions) == 2
    session_ids = {s.session_id for s in sessions}
    assert "myproject-01abc12345" in session_ids
    assert "otherapp-01def67890" in session_ids
