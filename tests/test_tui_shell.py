from archie_cli.tui.conversation import ShellOutput


def test_shell_output_uses_green_success_bullet():
    widget = ShellOutput("printf hello", "hello\n", exit_code=0)
    rendered = list(widget.compose())[0]

    assert "[bold #67c26d]●[/]" in rendered.content
    assert "printf hello" in rendered.content


def test_shell_output_uses_red_failure_bullet_and_exit_code():
    widget = ShellOutput("false", "", exit_code=2)
    rendered = list(widget.compose())[0]

    assert "[bold #ff6d67]●[/]" in rendered.content
    assert "(exit 2)" in rendered.content
