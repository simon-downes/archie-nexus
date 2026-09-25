from archie_cli.tui.conversation import ShellOutput


def test_shell_output_uses_green_success_bullet():
    widget = ShellOutput("printf hello", "hello\n", exit_code=0)
    rendered = list(widget.compose())[0]

    assert f"●" in rendered.renderable.plain
    assert "printf hello" in rendered.renderable.plain
    assert rendered.renderable.spans[0].style == "bold #67c26d"


def test_shell_output_uses_red_failure_bullet_and_exit_code():
    widget = ShellOutput("false", "", exit_code=2)
    rendered = list(widget.compose())[0]

    assert "●" in rendered.renderable.plain
    assert "(exit 2)" in rendered.renderable.plain
    assert rendered.renderable.spans[0].style == "bold #ff6d67"
