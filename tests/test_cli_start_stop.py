"""Tests for archie start and archie stop CLI commands (orchestrator API path)."""

import itertools
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import msgspec
from archie_cli.cli import _startup_status, main
from archie_shared.session import SessionDescriptor, split_id
from click.testing import CliRunner

SID = "myproject-01abc12345"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _record(session_id: str = SID, status: str = "idle", **fields) -> SessionDescriptor:
    return SessionDescriptor(
        session_id=session_id,
        workspace=split_id(session_id)[0],
        backend=fields.pop("backend", "native"),
        name=fields.pop("name", session_id),
        status=status,
        started_at="2026-07-01T10:00:00+00:00",
        **fields,
    )


def _session_response(
    session_id: str = SID, status: str = "starting", status_code: int = 202, **fields
) -> MagicMock:
    """Build a mock httpx.Response containing one SessionDescriptor."""
    descriptor = _record(session_id, status, **fields)
    mock = MagicMock()
    mock.status_code = status_code
    mock.content = msgspec.json.encode(descriptor)
    mock.json.return_value = msgspec.to_builtins(descriptor)
    mock.raise_for_status = MagicMock()
    return mock


def _sessions_list_response(sessions: list[SessionDescriptor]) -> MagicMock:
    """Build a mock httpx.Response for GET /sessions."""
    mock = MagicMock()
    mock.status_code = 200
    mock.content = msgspec.json.encode(sessions)
    mock.raise_for_status = MagicMock()
    return mock


def _error_response(status_code: int, error: str) -> MagicMock:
    """Build a mock httpx.Response with an error payload."""
    mock = MagicMock()
    mock.status_code = status_code
    mock.json.return_value = {"error": error}
    mock.raise_for_status = MagicMock()
    return mock


def test_startup_status_uses_backend_label_and_tty_spinner():
    """The terminal status display names the backend while startup is blocking."""
    for kiro, message in ((False, "Starting session…"), (True, "Starting Kiro session…")):
        console = MagicMock(is_terminal=True)
        with patch("archie_cli.cli.Console", return_value=console):
            with _startup_status(kiro):
                pass

        console.status.assert_called_once_with(message, spinner="dots")
        console.print.assert_not_called()


def test_startup_status_prints_non_tty_fallback():
    """Captured output receives a readable startup line without terminal control codes."""
    console = MagicMock(is_terminal=False)
    with patch("archie_cli.cli.Console", return_value=console):
        with _startup_status(True):
            pass

    console.print.assert_called_once_with("Starting Kiro session…")
    console.status.assert_not_called()


def test_startup_status_wraps_successful_request(monkeypatch, tmp_path):
    """Startup status starts before POST and stops after the response."""
    monkeypatch.setenv("ARCHIE_HOME_DIR", str(tmp_path))
    events = []

    @contextmanager
    def status(kiro):
        events.append(("enter", kiro))
        yield
        events.append(("exit", kiro))

    import httpx as real_httpx

    with patch("archie_cli.cli.httpx") as mock_httpx, patch("archie_cli.cli.time.sleep"):
        mock_httpx.post.side_effect = lambda *args, **kwargs: (
            events.append(("post", kwargs["json"])) or _session_response()
        )
        mock_httpx.get.side_effect = lambda *args, **kwargs: (
            events.append(("get", args[0])) or _session_response(status="idle", status_code=200)
        )
        mock_httpx.ConnectError = real_httpx.ConnectError
        mock_httpx.HTTPStatusError = real_httpx.HTTPStatusError
        with patch("archie_cli.cli._startup_status", side_effect=status):
            result = CliRunner().invoke(main, ["start", "--detach", "myproject"])

    assert result.exit_code == 0, result.output
    # The indicator stays up until the session has left "starting" (082 D22).
    assert events == [
        ("enter", False),
        ("post", {"workspace": "myproject"}),
        ("get", f"http://127.0.0.1:7600/api/sessions/{SID}"),
        ("exit", False),
    ]


def test_startup_status_wraps_failed_request(monkeypatch, tmp_path):
    """Startup status stops before a failed POST is reported."""
    monkeypatch.setenv("ARCHIE_HOME_DIR", str(tmp_path))
    events = []

    @contextmanager
    def status(kiro):
        events.append(("enter", kiro))
        try:
            yield
        finally:
            events.append(("exit", kiro))

    import httpx as real_httpx

    with patch("archie_cli.cli.httpx") as mock_httpx:
        mock_httpx.post.side_effect = lambda *args, **kwargs: (
            events.append(("post", kwargs["json"]))
            or (_ for _ in ()).throw(real_httpx.ConnectError("refused"))
        )
        mock_httpx.ConnectError = real_httpx.ConnectError
        mock_httpx.HTTPError = real_httpx.HTTPError
        with patch("archie_cli.cli._startup_status", side_effect=status):
            result = CliRunner().invoke(main, ["start", "--detach", "myproject"])

    assert result.exit_code != 0
    assert "archie serve" in result.output
    assert events == [("enter", False), ("post", {"workspace": "myproject"}), ("exit", False)]


# ---------------------------------------------------------------------------
# archie start
# ---------------------------------------------------------------------------


def _start(args, *, post=None, polls=(), monotonic=None):
    """Run ``archie start`` with a mocked orchestrator; returns (result, httpx mock, time mock)."""
    import httpx as real_httpx

    with patch("archie_cli.cli.httpx") as mock_httpx, patch("archie_cli.cli.time") as mock_time:
        mock_httpx.post.return_value = post or _session_response()
        mock_httpx.get.side_effect = list(polls)
        mock_httpx.ConnectError = real_httpx.ConnectError
        mock_httpx.HTTPError = real_httpx.HTTPError
        mock_httpx.HTTPStatusError = real_httpx.HTTPStatusError
        mock_time.monotonic.side_effect = monotonic or itertools.count()
        result = CliRunner().invoke(main, ["start", *args])
    return result, mock_httpx, mock_time


def test_start_detach_prints_session_and_container(monkeypatch, tmp_path):
    """archie start --detach prints the session ID and container, not an agent URL."""
    monkeypatch.setenv("ARCHIE_HOME_DIR", str(tmp_path))

    result, _, _ = _start(
        ["--detach", "myproject"], polls=[_session_response(status="idle", status_code=200)]
    )

    assert result.exit_code == 0, result.output
    assert f"Session: {SID}" in result.output
    assert f"Container: archie-{SID}" in result.output
    assert "Agent:" not in result.output


def test_start_sends_correct_workspace(monkeypatch, tmp_path):
    """archie start sends the given workspace name in POST body."""
    monkeypatch.setenv("ARCHIE_HOME_DIR", str(tmp_path))

    _, mock_httpx, _ = _start(["--detach", "my-app"], post=_session_response(status="idle"))

    mock_httpx.post.assert_called_once()
    call_kwargs = mock_httpx.post.call_args
    assert call_kwargs.args[0] == "http://127.0.0.1:7600/api/sessions"
    assert call_kwargs.kwargs["json"] == {"workspace": "my-app"}


def test_start_polls_every_half_second_until_ready(monkeypatch, tmp_path):
    monkeypatch.setenv("ARCHIE_HOME_DIR", str(tmp_path))

    result, mock_httpx, mock_time = _start(
        ["--detach", "myproject"],
        polls=[
            _session_response(status="starting", status_code=200),
            _session_response(status="idle", status_code=200),
        ],
    )

    assert result.exit_code == 0, result.output
    assert [c.args[0] for c in mock_httpx.get.call_args_list] == [
        f"http://127.0.0.1:7600/api/sessions/{SID}"
    ] * 2
    assert [c.args for c in mock_time.sleep.call_args_list] == [(0.5,), (0.5,)]
    mock_httpx.delete.assert_not_called()


def test_start_failed_reports_the_stored_error(monkeypatch, tmp_path):
    monkeypatch.setenv("ARCHIE_HOME_DIR", str(tmp_path))
    failed = _session_response(
        status="failed",
        status_code=200,
        error="Docker command failed: Kiro startup failed: kiro-cli is not logged in",
        ended_at="2026-07-01T10:00:05+00:00",
    )

    result, mock_httpx, _ = _start(["--detach", "--kiro", "myproject"], polls=[failed])

    assert result.exit_code == 1
    assert "Kiro startup failed: kiro-cli is not logged in" in result.output
    assert "Session:" not in result.output
    mock_httpx.delete.assert_not_called()


def test_start_ended_another_way_during_startup(monkeypatch, tmp_path):
    monkeypatch.setenv("ARCHIE_HOME_DIR", str(tmp_path))
    exited = _session_response(
        status="exited", status_code=200, ended_at="2026-07-01T10:00:05+00:00"
    )

    result, _, _ = _start(["--detach", "myproject"], polls=[exited])

    assert result.exit_code == 1
    assert "Session ended during startup (exited)" in result.output


def test_start_still_starting_after_60_seconds(monkeypatch, tmp_path):
    monkeypatch.setenv("ARCHIE_HOME_DIR", str(tmp_path))
    still = _session_response(status="starting", status_code=200)

    result, mock_httpx, _ = _start(
        ["--detach", "myproject"], polls=[still, still], monotonic=[0.0, 0.0, 30.0, 60.0]
    )

    assert result.exit_code == 1
    assert "Session is still starting after 60 s; check archie ls" in result.output
    assert mock_httpx.get.call_count == 2
    mock_httpx.delete.assert_not_called()


def test_start_poll_error_is_reported(monkeypatch, tmp_path):
    monkeypatch.setenv("ARCHIE_HOME_DIR", str(tmp_path))
    gone = _error_response(404, "No session with ID 'x'")

    result, _, _ = _start(["--detach", "myproject"], polls=[gone])

    assert result.exit_code == 1
    assert "Orchestrator error (404)" in result.output


def test_start_non_202_is_an_orchestrator_error(monkeypatch, tmp_path):
    """Anything but 202 from POST is reported as an orchestrator error."""
    monkeypatch.setenv("ARCHIE_HOME_DIR", str(tmp_path))

    result, _, _ = _start(["--detach", "myproject"], post=_error_response(500, "boom"))

    assert result.exit_code == 1
    assert "Orchestrator error (500): boom" in result.output


def test_start_orchestrator_unreachable(monkeypatch, tmp_path):
    """archie start when orchestrator unreachable → error with 'archie serve'."""
    monkeypatch.setenv("ARCHIE_HOME_DIR", str(tmp_path))

    import httpx as real_httpx

    with patch("archie_cli.cli.httpx") as mock_httpx:
        mock_httpx.post.side_effect = real_httpx.ConnectError("refused")
        mock_httpx.ConnectError = real_httpx.ConnectError
        mock_httpx.HTTPError = real_httpx.HTTPError
        mock_httpx.HTTPStatusError = real_httpx.HTTPStatusError
        runner = CliRunner()
        result = runner.invoke(main, ["start", "--detach", "myproject"])

    assert result.exit_code != 0
    assert "archie serve" in result.output


def test_start_workspace_not_found_400(monkeypatch, tmp_path):
    """archie start when orchestrator returns 400 → workspace error displayed."""
    monkeypatch.setenv("ARCHIE_HOME_DIR", str(tmp_path))

    result, _, _ = _start(
        ["--detach", "bad"], post=_error_response(400, "Workspace 'bad' not found")
    )

    assert result.exit_code != 0
    assert "not found" in result.output.lower()


def test_start_launches_tui_when_not_detached(monkeypatch, tmp_path):
    """archie start without --detach launches the TUI once the session is ready."""
    monkeypatch.setenv("ARCHIE_HOME_DIR", str(tmp_path))

    with patch("archie_cli.tui.app.ArchieApp") as mock_tui:
        mock_tui.return_value.run = MagicMock()
        _start(["myproject"], polls=[_session_response(status="idle", status_code=200)])

    mock_tui.assert_called_once_with(
        ws_url="ws://127.0.0.1:7600/api/sessions/myproject-01abc12345/stream",
        api_url="http://127.0.0.1:7600/api/sessions/myproject-01abc12345",
        container_name="archie-myproject-01abc12345",
    )
    mock_tui.return_value.run.assert_called_once()


# ---------------------------------------------------------------------------
# archie stop
# ---------------------------------------------------------------------------


def _make_sessions(*args) -> list[SessionDescriptor]:
    return [_record(sid, status) for sid, status in args]


def test_stop_sends_delete_for_matching_session(monkeypatch, tmp_path):
    """archie stop with prefix → resolves and sends DELETE /api/sessions/{id}."""
    monkeypatch.setenv("ARCHIE_HOME_DIR", str(tmp_path))

    import httpx as real_httpx

    sessions = _make_sessions(("myproject-01abc12345", "idle"))
    sessions_resp = _sessions_list_response(sessions)
    stop_resp = MagicMock()
    stop_resp.status_code = 200
    stop_resp.json.return_value = {"stopped": "myproject-01abc12345"}

    with patch("archie_cli.cli.httpx") as mock_httpx:
        mock_httpx.get.return_value = sessions_resp
        mock_httpx.delete.return_value = stop_resp
        mock_httpx.ConnectError = real_httpx.ConnectError
        mock_httpx.HTTPStatusError = real_httpx.HTTPStatusError
        runner = CliRunner()
        result = runner.invoke(main, ["stop", "myproject"])

    assert result.exit_code == 0
    assert "myproject-01abc12345" in result.output
    mock_httpx.delete.assert_called_once()
    assert mock_httpx.get.call_args[0][0] == "http://127.0.0.1:7600/api/sessions"
    assert (
        mock_httpx.delete.call_args[0][0]
        == "http://127.0.0.1:7600/api/sessions/myproject-01abc12345"
    )


def test_stop_no_sessions(monkeypatch, tmp_path):
    """archie stop with no sessions → clear error."""
    monkeypatch.setenv("ARCHIE_HOME_DIR", str(tmp_path))

    import httpx as real_httpx

    with patch("archie_cli.cli.httpx") as mock_httpx:
        mock_httpx.get.return_value = _sessions_list_response([])
        mock_httpx.ConnectError = real_httpx.ConnectError
        mock_httpx.HTTPStatusError = real_httpx.HTTPStatusError
        runner = CliRunner()
        result = runner.invoke(main, ["stop"])

    assert result.exit_code != 0
    assert "No running sessions" in result.output


def test_stop_orchestrator_unreachable(monkeypatch, tmp_path):
    """archie stop when orchestrator unreachable → error with 'archie serve'."""
    monkeypatch.setenv("ARCHIE_HOME_DIR", str(tmp_path))

    import httpx as real_httpx

    with patch("archie_cli.cli.httpx") as mock_httpx:
        mock_httpx.get.side_effect = real_httpx.ConnectError("refused")
        mock_httpx.ConnectError = real_httpx.ConnectError
        mock_httpx.HTTPStatusError = real_httpx.HTTPStatusError
        runner = CliRunner()
        result = runner.invoke(main, ["stop"])

    assert result.exit_code != 0
    assert "archie serve" in result.output


def test_start_kiro_posts_backend(monkeypatch, tmp_path):
    """archie start --kiro posts backend kiro and waits on the record, not /status (082)."""
    monkeypatch.setenv("ARCHIE_HOME_DIR", str(tmp_path))

    result, mock_httpx, _ = _start(
        ["--detach", "--kiro", "my-app"],
        post=_session_response(backend="kiro"),
        polls=[_session_response(status="idle", status_code=200, backend="kiro")],
    )

    assert result.exit_code == 0, result.output
    assert mock_httpx.post.call_args.kwargs["json"] == {"workspace": "my-app", "backend": "kiro"}
    assert [c.args[0] for c in mock_httpx.get.call_args_list] == [
        "http://127.0.0.1:7600/api/sessions/myproject-01abc12345"
    ]
    mock_httpx.delete.assert_not_called()
