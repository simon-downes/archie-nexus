"""Tests for archie_orchestrator.lifecycle — session start/stop logic."""

import os
import shutil
from pathlib import Path
from unittest.mock import patch

import pytest
from archie_orchestrator.docker import DockerError, RunningContainer
from archie_orchestrator.lifecycle import (
    REPO_ROOT,
    prepare_start,
    run_start,
    start_session,
    stop_session,
)
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


def test_start_session_missing_configured_mount_is_skipped(
    config, workspace, tmp_path, monkeypatch
):
    """A missing optional configured mount does not prevent session startup."""
    from archie_shared.schemas import NexusConfig

    ws_name, _ = workspace
    config = NexusConfig(global_=config.global_, mounts=(str(tmp_path / "missing"),))
    captured_cmd = []
    monkeypatch.setattr("archie_orchestrator.lifecycle.home_dir", lambda: tmp_path / ".nexus")
    with (
        patch("archie_orchestrator.lifecycle.check_image", return_value=True),
        patch(
            "archie_orchestrator.lifecycle.run_container",
            side_effect=lambda c, env=None: captured_cmd.extend(c),
        ),
        patch("archie_orchestrator.lifecycle.wait_for_ready", return_value="32771"),
    ):
        descriptor = start_session(ws_name, config)

    assert descriptor.port == 32771
    assert str(tmp_path / "missing") not in captured_cmd


def test_start_session_configured_mounts(config, workspace, tmp_path, monkeypatch):
    """start_session appends configured mounts to the docker command."""
    from archie_shared.schemas import NexusConfig

    ws_name, _ = workspace
    source = tmp_path / ".gitconfig"
    source.write_text("")
    config = NexusConfig(
        global_=config.global_, mounts=((str(source), "/home/archie/.gitconfig:ro"),)
    )
    captured_cmd = []
    monkeypatch.setattr("archie_orchestrator.lifecycle.home_dir", lambda: tmp_path / ".nexus")
    with (
        patch("archie_orchestrator.lifecycle.check_image", return_value=True),
        patch(
            "archie_orchestrator.lifecycle.run_container",
            side_effect=lambda c, env=None: captured_cmd.extend(c),
        ),
        patch("archie_orchestrator.lifecycle.wait_for_ready", return_value="32771"),
    ):
        start_session(ws_name, config)

    assert f"{source}:/home/archie/.gitconfig:ro" in captured_cmd


def test_start_session_correct_docker_command(config, workspace, tmp_path):
    """start_session constructs the correct docker run command."""
    ws_name, ws_path = workspace

    captured_cmd = []

    def fake_run(cmd, env=None):
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
    # Plugins mount (repo-tracked plugin packages), read-only
    assert f"{REPO_ROOT / 'plugins'}:/opt/archie/plugins:ro" in captured_cmd
    # gh wrapper bind-mounted over the image copy (D18)
    assert f"{REPO_ROOT / 'container' / 'bin' / 'gh'}:/usr/local/bin/gh:ro" in captured_cmd

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
            side_effect=lambda c, env=None: captured_cmd.extend(c),
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
            side_effect=lambda c, env=None: captured_cmd.extend(c),
        ),
        patch("archie_orchestrator.lifecycle.wait_for_ready", return_value="32771"),
        patch("archie_orchestrator.lifecycle.home_dir", return_value=tmp_path / ".nexus"),
    ):
        start_session(ws_name, config)

    cmd_str = " ".join(captured_cmd)
    assert "ARCHIE_HOME_DIR=" in cmd_str
    assert "ARCHIE_PERSONA_DIR=/opt/archie/persona" in cmd_str
    assert "ARCHIE_PLUGINS_DIR=/opt/archie/plugins" in cmd_str
    assert "ARCHIE_SESSION_ID=" in cmd_str


# ---------------------------------------------------------------------------
# stop_session tests
# ---------------------------------------------------------------------------


def test_stop_session_stops_by_name_without_docker_ps():
    """stop_session stops the container by name; no docker ps lookup (082 D19)."""
    with (
        patch("archie_orchestrator.docker.list_sessions") as mock_ls,
        patch("archie_orchestrator.lifecycle.stop_container") as mock_stop,
        patch(
            "archie_orchestrator.lifecycle._remove_empty_session_log", return_value=False
        ) as mock_remove,
    ):
        assert stop_session("myproject-01abc12345") is False

    mock_stop.assert_called_once_with("archie-myproject-01abc12345")
    mock_ls.assert_not_called()
    mock_remove.assert_called_once_with("myproject-01abc12345")


def test_stop_session_removes_a_prompt_less_log(session_logs):
    """A log with no prompt is removed and reported (D19a); one with a prompt is kept."""
    w = session_logs
    w.write("empty-01abc12345", [w.session_started()])
    w.write("used-01abc12345", [w.session_started(), w.user("hi")])
    with patch("archie_orchestrator.lifecycle.stop_container"):
        assert stop_session("empty-01abc12345") is True
        assert stop_session("used-01abc12345") is False
    assert not (w.dir / "empty-01abc12345.jsonl").exists()
    assert (w.dir / "used-01abc12345.jsonl").exists()


def test_stop_session_docker_failure_propagates_and_keeps_the_log():
    """A failed docker stop propagates and the log is not touched."""
    error = DockerError(command=["docker", "stop"], stderr="daemon busy", returncode=1)
    with (
        patch("archie_orchestrator.lifecycle.stop_container", side_effect=error),
        patch("archie_orchestrator.lifecycle._remove_empty_session_log") as mock_remove,
    ):
        with pytest.raises(DockerError, match="daemon busy"):
            stop_session("myproject-01abc12345")
    mock_remove.assert_not_called()


# ---------------------------------------------------------------------------
# Kiro backend (077)
# ---------------------------------------------------------------------------


def _start_capturing(config, ws_name, tmp_path, **kwargs):
    captured: list[str] = []
    with (
        patch("archie_orchestrator.lifecycle.check_image", return_value=True),
        patch(
            "archie_orchestrator.lifecycle.run_container",
            side_effect=lambda c, env=None: captured.extend(c),
        ),
        patch("archie_orchestrator.lifecycle.wait_for_ready", return_value="9999") as mock_wait,
        patch("archie_orchestrator.lifecycle.home_dir", return_value=tmp_path / ".nexus"),
    ):
        start_session(ws_name, config, **kwargs)
    return captured, mock_wait


def test_start_session_kiro_sets_backend_env_and_timeout(config, workspace, tmp_path):
    cmd, mock_wait = _start_capturing(config, workspace[0], tmp_path, backend="kiro")
    i = cmd.index("ARCHIE_BACKEND=kiro")
    assert cmd[i - 1] == "-e"
    assert mock_wait.call_args.kwargs["timeout"] == 45.0


def test_start_session_native_adds_no_backend_env(config, workspace, tmp_path):
    cmd, mock_wait = _start_capturing(config, workspace[0], tmp_path)
    assert not any(arg.startswith("ARCHIE_BACKEND") for arg in cmd)
    assert mock_wait.call_args.kwargs["timeout"] == 30.0


def test_start_session_invalid_backend(config, workspace):
    with pytest.raises(ValueError, match="Invalid backend 'x'"):
        start_session(workspace[0], config, backend="x")


def _fail_ready(config, ws_name, tmp_path, *, backend, write_reason: bool):
    from archie_orchestrator.docker import DockerError

    def fail(name, cmd, timeout):
        session_id = name.removeprefix("archie-")
        if write_reason:
            path = tmp_path / ".nexus" / "logs" / "kiro" / f"{session_id}.startup-error"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("Kiro contentCollection is 'enabled' after setting 'disabled'\n")
        raise DockerError(command=["docker", "inspect", name], stderr="exited", returncode=1)

    with (
        patch("archie_orchestrator.lifecycle.check_image", return_value=True),
        patch("archie_orchestrator.lifecycle.run_container"),
        patch("archie_orchestrator.lifecycle.wait_for_ready", side_effect=fail),
        patch("archie_orchestrator.lifecycle.stop_container") as mock_stop,
        patch("archie_orchestrator.lifecycle.home_dir", return_value=tmp_path / ".nexus"),
        pytest.raises(Exception) as exc,
    ):
        start_session(ws_name, config, backend=backend)
    return exc.value, mock_stop


def test_start_session_kiro_failure_includes_startup_error(config, workspace, tmp_path):
    error, mock_stop = _fail_ready(
        config, workspace[0], tmp_path, backend="kiro", write_reason=True
    )
    assert "Kiro startup failed: Kiro contentCollection is 'enabled'" in str(error)
    mock_stop.assert_called_once()


def test_start_session_kiro_failure_without_file_reraises(config, workspace, tmp_path):
    error, mock_stop = _fail_ready(
        config, workspace[0], tmp_path, backend="kiro", write_reason=False
    )
    assert "Kiro startup failed" not in str(error)
    assert "exited" in str(error)
    mock_stop.assert_called_once()


def test_start_session_native_docker_error_unchanged(config, workspace, tmp_path):
    error, mock_stop = _fail_ready(
        config, workspace[0], tmp_path, backend="native", write_reason=True
    )
    assert "Kiro startup failed" not in str(error)
    mock_stop.assert_not_called()


# ---------------------------------------------------------------------------
# prepare_start / run_start split (082 D15)
# ---------------------------------------------------------------------------


def test_prepare_start_validates_and_builds_without_running(config, workspace, tmp_path):
    with (
        patch("archie_orchestrator.lifecycle.check_image", return_value=True),
        patch("archie_orchestrator.lifecycle.run_container") as mock_run,
        patch("archie_orchestrator.lifecycle.wait_for_ready") as mock_wait,
        patch("archie_orchestrator.lifecycle.home_dir", return_value=tmp_path / ".nexus"),
    ):
        prepared = prepare_start(workspace[0], config, backend="kiro")
    mock_run.assert_not_called()
    mock_wait.assert_not_called()
    assert prepared.session_id.startswith("myproject-")
    assert prepared.container_name == f"archie-{prepared.session_id}"
    assert (prepared.workspace, prepared.backend) == ("myproject", "kiro")
    assert prepared.command[:2] == ["docker", "run"]
    assert f"ARCHIE_SESSION_ID={prepared.session_id}" in prepared.command
    assert prepared.nexus_home == tmp_path / ".nexus"


def test_prepare_start_rejects_bad_input_before_docker(config):
    with patch("archie_orchestrator.lifecycle.check_image") as mock_check:
        with pytest.raises(ValueError, match="not found"):
            prepare_start("nonexistent", config)
    mock_check.assert_not_called()


def test_run_start_runs_the_prepared_command_and_returns_the_port(config, workspace, tmp_path):
    with (
        patch("archie_orchestrator.lifecycle.check_image", return_value=True),
        patch("archie_orchestrator.lifecycle.home_dir", return_value=tmp_path / ".nexus"),
    ):
        prepared = prepare_start(workspace[0], config)
    with (
        patch("archie_orchestrator.lifecycle.run_container") as mock_run,
        patch("archie_orchestrator.lifecycle.wait_for_ready", return_value="40123") as mock_wait,
    ):
        assert run_start(prepared) == 40123
    mock_run.assert_called_once_with(
        prepared.command, env={"ARCHIE_AGENT_TOKEN": prepared.agent_token}
    )
    assert mock_wait.call_args.args == (prepared.container_name, prepared.command)


def test_start_session_returns_running_container(config, workspace, tmp_path):
    with (
        patch("archie_orchestrator.lifecycle.check_image", return_value=True),
        patch("archie_orchestrator.lifecycle.run_container"),
        patch("archie_orchestrator.lifecycle.wait_for_ready", return_value="9999"),
        patch("archie_orchestrator.lifecycle.home_dir", return_value=tmp_path / ".nexus"),
    ):
        started = start_session(workspace[0], config)
    assert isinstance(started, RunningContainer)
    assert started.port == 9999
    assert started.container_name == f"archie-{started.session_id}"


# ---------------------------------------------------------------------------
# Agent token and orchestrator URL (plan 090)
# ---------------------------------------------------------------------------


def _prepare(config, workspace, tmp_path):
    with (
        patch("archie_orchestrator.lifecycle.check_image", return_value=True),
        patch("archie_orchestrator.lifecycle.home_dir", return_value=tmp_path / ".nexus"),
    ):
        return prepare_start(workspace[0], config)


def test_prepare_start_names_the_agent_token_without_its_value(config, workspace, tmp_path):
    prepared = _prepare(config, workspace, tmp_path)
    cmd = prepared.command
    assert cmd[cmd.index("ARCHIE_AGENT_TOKEN") - 1] == "-e"
    assert not any(arg.startswith("ARCHIE_AGENT_TOKEN=") for arg in cmd)
    assert len(prepared.agent_token) >= 32
    assert prepared.agent_token not in " ".join(cmd)
    assert prepared.agent_token not in repr(prepared)
    assert cmd[cmd.index("ARCHIE_AGENT_CONTAINER=1") - 1] == "-e"
    assert "ARCHIE_ORCHESTRATOR_URL=http://host.docker.internal:7600" in cmd


def test_orchestrator_url_follows_the_default_profile_port(config, workspace, tmp_path):
    import msgspec

    custom = msgspec.structs.replace(
        config,
        orchestrator=msgspec.convert(
            {"profiles": {"default": {"port": 7611}}}, type(config.orchestrator)
        ),
    )
    cmd = _prepare(custom, workspace, tmp_path).command
    assert "ARCHIE_ORCHESTRATOR_URL=http://host.docker.internal:7611" in cmd


def test_each_session_gets_its_own_token(config, workspace, tmp_path):
    first = _prepare(config, workspace, tmp_path)
    second = _prepare(config, workspace, tmp_path)
    assert first.agent_token != second.agent_token


# ---------------------------------------------------------------------------
# Scratch workspace (093)
# ---------------------------------------------------------------------------


@pytest.fixture
def scratch(tmp_path) -> tuple[NexusConfig, Path]:
    """A config whose scratch workspace is ``chat``, and that workspace's directory."""
    ws_dir = tmp_path / "chat"
    ws_dir.mkdir()
    config = NexusConfig(
        global_=GlobalConfig(workspace_root=str(tmp_path), scratch_workspace="chat")
    )
    return config, ws_dir


def _prepare_in(config, name, tmp_path):
    with (
        patch("archie_orchestrator.lifecycle.check_image", return_value=True),
        patch("archie_orchestrator.lifecycle.home_dir", return_value=tmp_path / ".nexus"),
    ):
        return prepare_start(name, config)


def _run_seeing(prepared, directory):
    """Run ``prepared``; return the entries of ``directory`` when docker run was called."""
    seen: dict[str, object] = {}

    def fake_run(cmd, env=None):
        seen["entries"] = sorted(os.listdir(directory))
        agents = directory / "AGENTS.md"
        seen["agents"] = agents.read_text() if agents.exists() else None

    with (
        patch("archie_orchestrator.lifecycle.run_container", side_effect=fake_run),
        patch("archie_orchestrator.lifecycle.wait_for_ready", return_value="40123"),
    ):
        assert run_start(prepared) == 40123
    return seen


def test_a_scratch_start_mounts_a_fresh_session_directory(scratch, tmp_path):
    config, ws_dir = scratch
    (ws_dir / "AGENTS.md").write_text("chat rules\n")
    (ws_dir / "old.txt").write_text("left by an earlier chat")
    prepared = _prepare_in(config, "chat", tmp_path)
    session_dir = ws_dir.resolve() / prepared.session_id

    assert prepared.session_id.startswith("chat-")
    assert f"{session_dir}:/workspace:rw" in prepared.command
    assert f"{ws_dir.resolve()}:/workspace:rw" not in prepared.command
    assert not session_dir.exists()  # prepare_start creates nothing

    seen = _run_seeing(prepared, session_dir)
    assert seen == {"entries": ["AGENTS.md"], "agents": "chat rules\n"}


def test_a_scratch_workspace_without_agents_md_gets_an_empty_directory(scratch, tmp_path):
    config, ws_dir = scratch
    prepared = _prepare_in(config, "chat", tmp_path)
    seen = _run_seeing(prepared, ws_dir.resolve() / prepared.session_id)
    assert seen == {"entries": [], "agents": None}


def test_a_symlinked_agents_md_is_not_copied(scratch, tmp_path, caplog):
    config, ws_dir = scratch
    secret = tmp_path / "secret.txt"
    secret.write_text("a host file")
    (ws_dir / "AGENTS.md").symlink_to(secret)
    prepared = _prepare_in(config, "chat", tmp_path)
    seen = _run_seeing(prepared, ws_dir.resolve() / prepared.session_id)
    assert seen == {"entries": [], "agents": None}
    assert "it is a symlink" in caplog.text


def test_other_workspaces_mount_as_before_with_scratch_on(scratch, workspace, tmp_path):
    config, _ = scratch
    ws_name, ws_path = workspace
    prepared = _prepare_in(config, ws_name, tmp_path)
    assert f"{ws_path.resolve()}:/workspace:rw" in prepared.command
    assert (prepared.scratch_dir, prepared.agents_md) == (None, None)
    seen = _run_seeing(prepared, ws_path)
    assert seen == {"entries": [], "agents": None}  # nothing created in the workspace


def test_a_scratch_directory_that_already_exists_fails_the_start(scratch, tmp_path):
    config, ws_dir = scratch
    prepared = _prepare_in(config, "chat", tmp_path)
    prepared.scratch_dir.mkdir()
    (prepared.scratch_dir / "keep.txt").write_text("x")
    with patch("archie_orchestrator.lifecycle.run_container") as mock_run:
        with pytest.raises(FileExistsError):
            run_start(prepared)
    mock_run.assert_not_called()
    assert (prepared.scratch_dir / "keep.txt").exists()


def _scratch_session(config, ws_dir, tmp_path, *, agents: str | None = "rules\n"):
    """A started scratch session (docker patched out): its ID and directory."""
    if agents is not None:
        (ws_dir / "AGENTS.md").write_text(agents)
    prepared = _prepare_in(config, "chat", tmp_path)
    _run_seeing(prepared, ws_dir.resolve() / prepared.session_id)
    return prepared.session_id, prepared.scratch_dir


def _stop(session_id, config):
    with (
        patch("archie_orchestrator.lifecycle.stop_container"),
        patch("archie_orchestrator.lifecycle._remove_empty_session_log", return_value=False),
    ):
        return stop_session(session_id, config=config)


@pytest.mark.parametrize("agents", [None, "rules\n"])
def test_stop_removes_an_unused_scratch_directory(scratch, tmp_path, agents):
    config, ws_dir = scratch
    session_id, directory = _scratch_session(config, ws_dir, tmp_path, agents=agents)
    assert directory.is_dir()
    assert _stop(session_id, config) is False  # the log result is unchanged
    assert not directory.exists()
    assert (agents is None) or (ws_dir / "AGENTS.md").read_text() == agents  # source kept


def test_stop_keeps_a_scratch_directory_the_session_wrote_to(scratch, tmp_path):
    config, ws_dir = scratch
    session_id, directory = _scratch_session(config, ws_dir, tmp_path)
    (directory / "notes.md").write_text("an artefact")
    _stop(session_id, config)
    assert sorted(os.listdir(directory)) == ["AGENTS.md", "notes.md"]


def test_stop_keeps_a_scratch_directory_whose_agents_md_became_a_symlink(scratch, tmp_path):
    config, ws_dir = scratch
    session_id, directory = _scratch_session(config, ws_dir, tmp_path)
    (directory / "AGENTS.md").unlink()
    (directory / "AGENTS.md").symlink_to(ws_dir / "AGENTS.md")  # same bytes when followed
    _stop(session_id, config)
    assert (directory / "AGENTS.md").is_symlink()


def test_stop_without_config_or_for_other_workspaces_leaves_directories_alone(
    scratch, workspace, tmp_path
):
    config, ws_dir = scratch
    session_id, directory = _scratch_session(config, ws_dir, tmp_path)
    _stop(session_id, None)
    assert directory.is_dir()
    ws_name, ws_path = workspace
    (ws_path / f"{ws_name}-01abc12345").mkdir()  # looks like a scratch dir, but isn't one
    _stop(f"{ws_name}-01abc12345", config)
    assert (ws_path / f"{ws_name}-01abc12345").is_dir()


def test_stop_of_a_scratch_session_whose_directory_is_gone_is_quiet(scratch, tmp_path, caplog):
    config, ws_dir = scratch
    session_id, directory = _scratch_session(config, ws_dir, tmp_path)
    shutil.rmtree(directory)
    assert _stop(session_id, config) is False
    assert "WARNING" not in caplog.text


def test_a_scratch_cleanup_error_is_logged_and_the_stop_still_succeeds(scratch, tmp_path, caplog):
    config, ws_dir = scratch
    session_id, directory = _scratch_session(config, ws_dir, tmp_path)
    with patch("archie_orchestrator.lifecycle.os.scandir", side_effect=PermissionError("denied")):
        assert _stop(session_id, config) is False
    assert "Cannot inspect scratch directory" in caplog.text
    assert directory.is_dir()


def _fail_start(prepared, *, running):
    """Run ``prepared`` with a readiness failure and ``container_running`` as given."""
    error = DockerError(command=["docker", "inspect"], stderr="not ready", returncode=1)
    alive = (
        {"side_effect": running} if isinstance(running, Exception) else {"return_value": running}
    )
    with (
        patch("archie_orchestrator.lifecycle.run_container"),
        patch("archie_orchestrator.lifecycle.wait_for_ready", side_effect=error),
        patch("archie_orchestrator.lifecycle.container_running", **alive),
        pytest.raises(DockerError) as raised,
    ):
        run_start(prepared)
    return raised.value


def test_a_failed_start_removes_its_scratch_directory_when_the_container_is_gone(scratch, tmp_path):
    config, ws_dir = scratch
    (ws_dir / "AGENTS.md").write_text("rules\n")
    prepared = _prepare_in(config, "chat", tmp_path)
    error = _fail_start(prepared, running=False)
    assert "not ready" in str(error)  # the original error is re-raised
    assert not prepared.scratch_dir.exists()


@pytest.mark.parametrize("running", [True, DockerError(["docker"], "daemon down", 1)])
def test_a_failed_start_keeps_its_scratch_directory_if_the_container_may_run(
    scratch, tmp_path, running, caplog
):
    config, ws_dir = scratch
    prepared = _prepare_in(config, "chat", tmp_path)
    error = _fail_start(prepared, running=running)
    assert "not ready" in str(error)
    assert prepared.scratch_dir.is_dir()
    assert "Keeping" in caplog.text


def test_a_symlinked_alias_of_the_scratch_workspace_is_scratch_too(scratch, tmp_path):
    config, ws_dir = scratch
    (tmp_path / "alias").symlink_to(ws_dir)
    prepared = _prepare_in(config, "alias", tmp_path)
    assert prepared.scratch_dir == ws_dir.resolve() / prepared.session_id
    assert f"{prepared.scratch_dir}:/workspace:rw" in prepared.command


def test_a_fifo_agents_md_is_skipped_without_blocking(scratch, tmp_path):
    config, ws_dir = scratch
    os.mkfifo(ws_dir / "AGENTS.md")
    prepared = _prepare_in(config, "chat", tmp_path)
    seen = _run_seeing(prepared, prepared.scratch_dir)
    assert seen == {"entries": [], "agents": None}


def test_a_failed_agents_md_copy_removes_the_new_directory(scratch, tmp_path):
    config, ws_dir = scratch
    agents = ws_dir / "AGENTS.md"
    agents.write_text("rules")
    agents.chmod(0)
    prepared = _prepare_in(config, "chat", tmp_path)
    try:
        with patch("archie_orchestrator.lifecycle.run_container") as mock_run:
            with pytest.raises(PermissionError):
                run_start(prepared)
    finally:
        agents.chmod(0o644)
    mock_run.assert_not_called()
    assert not prepared.scratch_dir.exists()


@pytest.mark.parametrize("content", ["rules!", "RULES"])  # another size; same size
def test_stop_keeps_a_scratch_directory_whose_agents_md_differs(scratch, tmp_path, content):
    config, ws_dir = scratch
    session_id, directory = _scratch_session(config, ws_dir, tmp_path, agents="rules")
    (directory / "AGENTS.md").write_text(content)
    _stop(session_id, config)
    assert (directory / "AGENTS.md").read_text() == content


def test_stop_does_not_read_a_huge_agents_md_into_memory(scratch, tmp_path):
    config, ws_dir = scratch
    session_id, directory = _scratch_session(config, ws_dir, tmp_path)
    with open(directory / "AGENTS.md", "r+b") as f:
        f.truncate(1 << 40)  # sparse: 1 TiB that would not fit in memory
    _stop(session_id, config)
    assert (directory / "AGENTS.md").stat().st_size == 1 << 40


def test_a_scratch_directory_that_cannot_be_removed_is_logged(scratch, tmp_path, caplog):
    config, ws_dir = scratch
    session_id, directory = _scratch_session(config, ws_dir, tmp_path, agents=None)
    ws_dir.chmod(0o555)
    try:
        assert _stop(session_id, config) is False
    finally:
        ws_dir.chmod(0o755)
    assert "Cannot remove scratch directory" in caplog.text
    assert directory.is_dir()
