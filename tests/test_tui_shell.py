from archie_cli.tui.conversation import ShellOutput


def _header(widget: ShellOutput):
    return list(widget.compose())[0].content


def _styles(text) -> dict[str, str]:
    return {text.plain[s.start : s.end]: str(s.style) for s in text.spans}


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
