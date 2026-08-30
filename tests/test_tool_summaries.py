"""Tests for shared tool summary formatting."""

from archie_shared.tool_summaries import format_tool_activity, format_tool_complete


def test_tool_activity_uses_shared_exec_line_count():
    activity = format_tool_activity("exec", {"source": "async def main():\n    return 1"})
    assert activity == "Exec (2 lines)"


def test_tool_activity_reuses_non_exec_pending_summary():
    assert "Read" in format_tool_activity("read", {"path": "foo.py"})
    assert "foo.py" in format_tool_activity("read", {"path": "foo.py"})


def test_tool_duration_is_added_to_existing_metadata_parentheses():
    summary = format_tool_complete("read", {"path": "example.py"}, "File: example.py (3 lines)\n", False, 50)
    assert "(3 lines, 50ms)" in summary
    assert ") (50ms)" not in summary


def test_shell_error_summary_escapes_exit_marker_and_includes_lines():
    summary = format_tool_complete(
        "shell",
        {"command": "echo \\\"Hello World\\\" && false"},
        "[exit: 1]\nHello World",
        True,
        2,
    )
    assert "\\[exit: 1]" in summary
    assert "\\\\]" not in summary
    assert "(2 lines, 2ms)" in summary
    assert "Hello World" in summary


def test_skill_summary_reports_loaded_lines():
    summary = format_tool_complete(
        "skill", {"name": "workflow-review"}, "Loaded skill 'workflow-review' into system prompt (42 lines).", False, 50
    )
    assert "workflow-review" in summary
    assert "(42 lines, 50ms)" in summary
    assert "loaded" not in summary


def test_edit_diff_uses_text_colours_and_line_markers():
    """Edit diffs show red/green text with deletion/addition markers."""
    diff = """--- a/example.py
+++ b/example.py
@@ -1,2 +1,2 @@
-old line
+new line
 context
"""

    summary = format_tool_complete("edit", {"path": "example.py"}, diff, False)

    assert "[#ff6d67]   1- old line[/]" in summary
    assert "[#67c26d]   1+ new line[/]" in summary
    assert "[on red]" not in summary
    assert "[on green]" not in summary
