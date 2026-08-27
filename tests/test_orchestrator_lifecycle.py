"""Tests for archie_orchestrator.lifecycle — session start/stop logic."""

from pathlib import Path
from unittest.mock import patch

import pytest
from archie_orchestrator.lifecycle import REPO_ROOT, start_session, stop_session
from archie_shared.schemas import GlobalConfig, NexusConfig

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def config(tmp_path) -> NexusConfig:
    """NexusConfig with workspace_root pointing at a tmp directory."""
    return NexusConfig(
        global_=GlobalConfig(
            workspace_root=str(tmp_path),
            model="bedrock-claude-sonnet-4-6",
            region="eu-west-1",
        )
    )


@pytest.fixture
def workspace(tmp_path) -> tuple[str, Path]:
    """Create a workspace directory and return (name, path)."""
    ws_dir = tmp_path / "myproject"
    ws_dir.mkdir()
    return "myproject", ws_dir


# ---------------------------------------------------------------------------
# start_session tests
# ---------------------------------------------------------------------------


def test_start_session_workspace_not_found(config):
    """Workspace directory not found → ValueError."""
    with pytest.raises(ValueError, match="not found"):
        start_session("nonexistent", config)


def test_start_session_workspace_path_traversal(config):
    """Workspace name with path traversal → ValueError before any Docker call."""
    traversal_names = ["../etc", "../../root", "foo/bar", ".", ".."]
    for name in traversal_names:
        with pytest.raises(ValueError, match="Invalid workspace name"):
            start_session(name, config)


def test_start_session_workspace_backslash_rejected(config):
    """Workspace name with backslash → ValueError."""
    with pytest.raises(ValueError, match="Invalid workspace name"):
        start_session("foo\\bar", config)


def test_start_session_image_not_found(config, workspace):
    """Image not found → RuntimeError before docker run."""
    ws_name, _ = workspace
    with patch("archie_orchestrator.lifecycle.check_image", return_value=False):
        with patch("archie_orchestrator.lifecycle.run_container") as mock_run:
            with pytest.raises(RuntimeError, match="archie:latest"):
                start_session(ws_name, config)
    # docker run must not have been called
    mock_run.assert_not_called()


def test_start_session_correct_docker_command(config, workspace, tmp_path):
    """start_session constructs the correct docker run command."""
    ws_name, ws_path = workspace

    captured_cmd = []

    def fake_run(cmd):
        captured_cmd.extend(cmd)

    fake_descriptor_port = "32771"

    with (
        patch("archie_orchestrator.lifecycle.check_image", return_value=True),
        patch("archie_orchestrator.lifecycle.run_container", side_effect=fake_run),
        patch("archie_orchestrator.lifecycle.wait_for_ready", return_value=fake_descriptor_port),
        patch("archie_orchestrator.lifecycle.home_dir", return_value=tmp_path / ".nexus"),
        patch("archie_shared.session.identity.ULID") as mock_ulid,
    ):
        mock_ulid.return_value.__str__ = lambda self: "01ABC1234567890"
        descriptor = start_session(ws_name, config)

    # Verify key command elements
    assert "docker" in captured_cmd
    assert "run" in captured_cmd
    assert "--rm" in captured_cmd
    assert "--add-host=host.docker.internal:host-gateway" in captured_cmd
    assert "127.0.0.1:0:8080" in captured_cmd

    # Workspace mount (resolved path)
    assert f"{ws_path.resolve()}:/workspace:rw" in captured_cmd

    # Agent and shared mounts from REPO_ROOT
    assert f"{REPO_ROOT / 'agent'}:/opt/archie/agent:rw" in captured_cmd
    assert f"{REPO_ROOT / 'shared'}:/opt/archie/shared:ro" in captured_cmd
    # Persona mount (repo-tracked skills + prompts), read-write
    assert f"{REPO_ROOT / 'persona'}:/opt/archie/persona:rw" in captured_cmd

    # Return value
    assert descriptor.port == 32771
    assert descriptor.container_name.startswith("archie-")
    assert descriptor.session_id.startswith("myproject-")


def test_start_session_wait_for_ready_called(config, workspace, tmp_path):
    """start_session passes the docker command to wait_for_ready."""
    ws_name, _ = workspace

    with (
        patch("archie_orchestrator.lifecycle.check_image", return_value=True),
        patch("archie_orchestrator.lifecycle.run_container"),
        patch("archie_orchestrator.lifecycle.wait_for_ready", return_value="9999") as mock_wait,
        patch("archie_orchestrator.lifecycle.home_dir", return_value=tmp_path / ".nexus"),
    ):
        start_session(ws_name, config)

    # wait_for_ready must have been called with a container name and the docker command
    mock_wait.assert_called_once()
    args = mock_wait.call_args[0]
    assert args[0].startswith("archie-")  # container name
    assert args[1][0] == "docker"  # docker run cmd


def test_start_session_container_crash_propagates(config, workspace, tmp_path):
    """RuntimeError from wait_for_ready propagates out of start_session."""
    ws_name, _ = workspace

    with (
        patch("archie_orchestrator.lifecycle.check_image", return_value=True),
        patch("archie_orchestrator.lifecycle.run_container"),
        patch(
            "archie_orchestrator.lifecycle.wait_for_ready",
            side_effect=RuntimeError("Container exited"),
        ),
        patch("archie_orchestrator.lifecycle.home_dir", return_value=tmp_path / ".nexus"),
    ):
        with pytest.raises(RuntimeError, match="Container exited"):
            start_session(ws_name, config)


def test_start_session_forwards_brain_env_without_mount(config, workspace, tmp_path, monkeypatch):
    ws_name, _ = workspace
    captured_cmd = []
    monkeypatch.setenv("ARCHIE_BRAIN_DIR", "/custom/brain")
    with (
        patch("archie_orchestrator.lifecycle.check_image", return_value=True),
        patch(
            "archie_orchestrator.lifecycle.run_container",
            side_effect=lambda c: captured_cmd.extend(c),
        ),
        patch("archie_orchestrator.lifecycle.wait_for_ready", return_value="32771"),
        patch("archie_orchestrator.lifecycle.home_dir", return_value=tmp_path / ".nexus"),
    ):
        start_session(ws_name, config)
    assert "ARCHIE_BRAIN_DIR=/custom/brain" in captured_cmd
    assert not any("/custom/brain:/custom/brain" in item for item in captured_cmd)


def test_start_session_env_vars_in_command(config, workspace, tmp_path):
    """docker run command includes ARCHIE_HOME_DIR and ARCHIE_SESSION_ID env vars."""
    ws_name, _ = workspace
    captured_cmd = []

    with (
        patch("archie_orchestrator.lifecycle.check_image", return_value=True),
        patch(
            "archie_orchestrator.lifecycle.run_container",
            side_effect=lambda c: captured_cmd.extend(c),
        ),
        patch("archie_orchestrator.lifecycle.wait_for_ready", return_value="32771"),
        patch("archie_orchestrator.lifecycle.home_dir", return_value=tmp_path / ".nexus"),
    ):
        start_session(ws_name, config)

    cmd_str = " ".join(captured_cmd)
    assert "ARCHIE_HOME_DIR=" in cmd_str
    assert "ARCHIE_PERSONA_DIR=/opt/archie/persona" in cmd_str
    assert "ARCHIE_SESSION_ID=" in cmd_str


# ---------------------------------------------------------------------------
# stop_session tests
# ---------------------------------------------------------------------------


def test_stop_session_calls_stop_container(config, workspace):
    """stop_session calls stop_container with the correct container name."""
    from archie_shared.session import SessionDescriptor

    fake_sessions = [
        SessionDescriptor(
            session_id="myproject-01abc12345",
            container_name="archie-myproject-01abc12345",
            port=32771,
            raw_docker_status="Up 1 hour",
        )
    ]

    with (
        patch("archie_orchestrator.lifecycle.list_sessions", return_value=fake_sessions),
        patch("archie_orchestrator.lifecycle.stop_container") as mock_stop,
    ):
        stop_session("myproject-01abc12345")

    mock_stop.assert_called_once_with("archie-myproject-01abc12345")


def test_stop_session_not_found_raises_key_error():
    """stop_session raises KeyError when session ID is not running."""
    with patch("archie_orchestrator.lifecycle.list_sessions", return_value=[]):
        with pytest.raises(KeyError, match="myproject-01abc12345"):
            stop_session("myproject-01abc12345")


def test_stop_session_docker_failure_propagates():
    """RuntimeError from stop_container propagates out of stop_session."""
    from archie_shared.session import SessionDescriptor

    fake_sessions = [
        SessionDescriptor(
            session_id="myproject-01abc12345",
            container_name="archie-myproject-01abc12345",
            port=32771,
            raw_docker_status="Up",
        )
    ]

    with (
        patch("archie_orchestrator.lifecycle.list_sessions", return_value=fake_sessions),
        patch(
            "archie_orchestrator.lifecycle.stop_container",
            side_effect=RuntimeError("docker stop failed"),
        ),
    ):
        with pytest.raises(RuntimeError, match="docker stop failed"):
            stop_session("myproject-01abc12345")
