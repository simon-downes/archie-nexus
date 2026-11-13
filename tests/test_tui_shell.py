"""The ``!`` direct shell: its output row, and the local-profile rule (084 D20, D21)."""

from unittest.mock import AsyncMock, MagicMock, patch

from archie_cli.tui.client import ProfileInfo
from archie_cli.tui.conversation import ClientErrorMessage, ShellOutput
from archie_cli.tui.session import SHELL_NOT_LOCAL_TEXT, ShellOutputEntry

from tests.tui_fakes import (
    LOCAL_PROFILE,
    FakeOrchestrator,
    attached,
    local_texts,
    make_app,
    make_controller,
    make_record,
    settle,
)

REMOTE_PROFILE = ProfileInfo(label="gpu-box", web_url="http://10.0.0.1:7601/", local=False)
SIZE = (170, 40)


def _header(widget: ShellOutput):
    return list(widget.compose())[0].content


def _styles(text) -> dict[str, str]:
    return {text.plain[s.start : s.end]: str(s.style) for s in text.spans}


def _process(output: bytes = b"hi\n") -> MagicMock:
    process = MagicMock(returncode=0)
    process.communicate = AsyncMock(return_value=(output, None))
    return process


def test_shell_output_uses_green_success_bullet():
    header = _header(ShellOutput("printf hello", "hello\n", exit_code=0))

    assert header.plain == "● $ printf hello"
    assert _styles(header)["●"] == "bold #67c26d"


def test_shell_output_uses_red_failure_bullet_and_exit_code():
    header = _header(ShellOutput("false", "", exit_code=2))

    assert _styles(header)["●"] == "bold #ff6d67"
    assert header.plain.endswith("(exit 2)")


def test_shell_output_shows_brackets_and_backslashes_literally():
    command = r"rg x tenants/\[slug\]/a.ts [bold]"
    widget = ShellOutput(command, "out [/] \\", exit_code=0)
    header, body = (static.content for static in widget.compose())

    assert header.plain == f"● $ {command}"
    assert body.plain == "out [/] \\"


async def test_a_controller_without_direct_shell_answers_bang_with_a_client_entry():
    client = FakeOrchestrator()
    controller, listener = make_controller(client)
    controller.direct_shell = False
    with patch(
        "archie_cli.tui.session.asyncio.create_subprocess_exec", new_callable=AsyncMock
    ) as exec_:
        controller.submit("!ls -la")
        controller.submit("!")  # an empty command is still ignored
    exec_.assert_not_called()
    assert local_texts(controller) == [SHELL_NOT_LOCAL_TEXT]
    assert not controller.shell_active and not controller.turn_active
    assert client.streams == []  # nothing reaches the agent either


async def test_bang_runs_in_the_container_on_a_local_profile():
    client = FakeOrchestrator(sessions=[make_record("s1")])
    app = make_app(client, profile=LOCAL_PROFILE)
    async with app.run_test(size=SIZE) as pilot:
        pane = await attached(app, pilot)
        assert app.controller.direct_shell
        with patch(
            "archie_cli.tui.session.asyncio.create_subprocess_exec",
            new_callable=AsyncMock,
            return_value=_process(),
        ) as exec_:
            await pilot.press(*"!echo hi", "enter")
            await settle(pilot, lambda: pane.conversation.query(ShellOutput))
        assert exec_.await_args.args == (
            "docker",
            "exec",
            "-w",
            "/workspace",
            "archie-s1",
            "bash",
            "-c",
            "echo hi",
        )
        assert app.controller.locals == [ShellOutputEntry(None, "echo hi", "hi\n", 0)]
        assert client.latest.sent == []  # not sent to the agent


async def test_bang_on_a_remote_profile_shows_a_client_error_and_runs_nothing():
    app = make_app(profile=REMOTE_PROFILE)
    async with app.run_test(size=SIZE) as pilot:
        pane = await attached(app, pilot)
        assert not app.controller.direct_shell
        with patch(
            "archie_cli.tui.session.asyncio.create_subprocess_exec", new_callable=AsyncMock
        ) as exec_:
            await pilot.press(*"!ls", "enter")
            await settle(pilot, lambda: pane.conversation.query(ClientErrorMessage))
        exec_.assert_not_called()
        (row,) = pane.conversation.query(ClientErrorMessage)
        assert row.get_copy_text() == SHELL_NOT_LOCAL_TEXT
        assert not pane.conversation.query(ShellOutput)
        assert app._client.latest.sent == [] and not app.controller.turn_active
        assert pane.input.text == "" and not pane.input.disabled
