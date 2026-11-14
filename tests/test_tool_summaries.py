"""Tests for shared tool summary formatting."""

import pytest
from archie_shared.markup import plain_text
from archie_shared.tool_summaries import (
    call_description,
    format_tool_activity,
    format_tool_complete,
    format_tool_pending,
)

# --- descriptions (086) ---


def test_call_description_is_one_line_for_shell_and_exec_only():
    assert call_description("shell", {"description": "Run\n  tests"}) == "Run tests"
    assert call_description("exec", {"description": " Count rows "}) == "Count rows"
    assert call_description("read", {"description": "x"}) == ""
    for value in (None, "", "  ", 3):
        assert call_description("shell", {"description": value}) == ""
    assert call_description("shell", {}) == ""


def test_activity_shows_the_description():
    shell = {"command": "git status", "description": "Check the tree"}
    assert format_tool_activity("shell", shell) == "Shell [bold]Check the tree[/]"
    assert format_tool_pending("shell", shell) == "Shell [bold]Check the tree[/]"
    exec_input = {"source": "x\ny", "description": "Count rows"}
    assert format_tool_activity("exec", exec_input) == "Exec [bold]Count rows[/]"


@pytest.mark.parametrize(
    ("result", "is_error"),
    [("[exit: 0]\nhi", False), ("[exit: 1]\nboom", False), ("[exit: 1]\nboom", True)],
)
def test_finished_shell_summary_shows_the_description(result, is_error):
    tool_input = {"command": "echo hi", "description": "Say hi"}
    summary = format_tool_complete("shell", tool_input, result, is_error, 12)
    first = plain_text(summary.split("\n")[0])
    assert first.startswith("Shell Say hi") and "echo" not in first


def test_description_markup_is_escaped():
    tool_input = {"command": "ls", "description": "list a/[slug]/b"}
    assert plain_text(format_tool_activity("shell", tool_input)) == "Shell list a/[slug]/b"
    exec_input = {"source": "x", "description": "[bold]x"}
    assert plain_text(format_tool_activity("exec", exec_input)) == "Exec [bold]x"


def test_summaries_without_a_description_are_unchanged():
    assert format_tool_activity("shell", {"command": "ls"}) == "Shell [bold]ls[/]"
    assert format_tool_activity("exec", {"source": "x\ny"}) == "Exec (2 lines)"


def test_tool_activity_uses_shared_exec_line_count():
    activity = format_tool_activity("exec", {"source": "async def main():\n    return 1"})
    assert activity == "Exec (2 lines)"


def test_tool_activity_reuses_non_exec_pending_summary():
    assert "Read" in format_tool_activity("read", {"path": "foo.py"})
    assert "foo.py" in format_tool_activity("read", {"path": "foo.py"})


def test_brain_search_summary_shows_query_and_limit():
    summary = format_tool_pending("brain_search", {"query": "deployment aws", "limit": 5})
    assert "Brain search" in summary
    assert "deployment aws" in summary
    assert "limit 5" in summary


def test_brain_search_completion_shows_query_and_result_count():
    summary = format_tool_complete(
        "brain_search",
        {"query": "deployment aws", "limit": 5},
        '[{"name": "one"}, {"name": "two"}]',
        False,
    )
    assert "deployment aws" in summary
    assert "(2 results)" in summary


def test_tool_duration_is_added_to_existing_metadata_parentheses():
    summary = format_tool_complete(
        "read", {"path": "example.py"}, "File: example.py (3 lines)\n", False, 50
    )
    assert "(3 lines, 50ms)" in summary
    assert ") (50ms)" not in summary


def test_shell_error_summary_escapes_exit_marker_and_includes_lines():
    summary = format_tool_complete(
        "shell",
        {"command": 'echo \\"Hello World\\" && false'},
        "[exit: 1]\nHello World",
        True,
        2,
    )
    assert "\\[exit: 1]" in summary
    assert "\\\\]" not in summary
    assert "(2 lines, 2ms)" in summary
    assert "Hello World" in summary


def test_skill_summary_reports_result_size_and_references():
    result = (
        "Skill 'workflow-review' loaded.\n\n"
        '<skill name="workflow-review">\nbody\n</skill>\n\n'
        '<reference name="workflow-review" file="references/guide.md">\nref\n</reference>'
    )
    summary = format_tool_complete(
        "skill",
        {"name": "workflow-review", "references": ["references/guide.md"]},
        result,
        False,
        50,
    )
    assert "workflow-review" in summary
    assert f"({len(result.encode('utf-8'))} bytes, 50ms)" in summary
    assert "body" not in summary
    assert "\nref\n" not in summary


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


def test_question_pending_shows_question():
    assert (
        format_tool_pending("question", {"question": "Which db?"}) == "Question [bold]Which db?[/]"
    )


def test_question_complete_shows_selected_option():
    summary = format_tool_complete(
        "question", {"question": "Which db?"}, "User selected option 2: Postgres", False
    )
    assert summary == "Question [bold]Which db?[/] → [bold]Postgres[/]"


def test_question_complete_shows_free_text_answer():
    summary = format_tool_complete(
        "question", {"question": "Which db?"}, "User answered: MySQL: maybe", False
    )
    assert summary == "Question [bold]Which db?[/] → [bold]MySQL: maybe[/]"


def test_question_complete_shows_dismissal():
    result = "User dismissed the question without answering. Do not assume an answer."
    summary = format_tool_complete("question", {"question": "Which db?"}, result, False)
    assert summary == "Question [bold]Which db?[/] [dim](dismissed)[/]"
