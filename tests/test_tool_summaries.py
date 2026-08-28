"""Tests for shared tool summary formatting."""

from archie_shared.tool_summaries import format_tool_activity, format_tool_complete


def test_tool_activity_uses_shared_exec_line_count():
    activity = format_tool_activity("exec", {"source": "async def main():\n    return 1"})
    assert activity == "Exec (2 lines)"


def test_tool_activity_reuses_non_exec_pending_summary():
    assert "Read" in format_tool_activity("read", {"path": "foo.py"})
    assert "foo.py" in format_tool_activity("read", {"path": "foo.py"})


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
