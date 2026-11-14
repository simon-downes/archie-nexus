"""Two-line tool rows: every example in plans/tui-tool-rows.md (084 D11, D12)."""

from __future__ import annotations

import json

import pytest
from archie_shared.tool_rows import (
    Span,
    display_path,
    elide_path,
    format_duration,
    row_lines,
    row_text,
    strip_workspace,
    tool_row,
)
from archie_shared.tool_summaries import (
    QUESTION_DISMISSED_RESULT,
    QUESTION_SELECTED_PREFIX,
)
from archie_shared.transcript import SubagentNode, ToolNode


def _tool(
    name: str,
    tool_input: dict,
    result: str | None = None,
    *,
    error: bool = False,
    ms: int = 0,
    children: tuple[SubagentNode, ...] = (),
) -> ToolNode:
    """A call as the transcript model holds it once its ``tool_result`` landed."""
    node = ToolNode(id="tool-t", tool_use_id="t", name=name, input=tool_input)
    node.children = list(children)
    if result is not None:
        node.result = result
        node.status = "error" if error else "ok"
        node.duration_ms = ms
        node.result_lines = len(result.splitlines())
        node.result_bytes = len(result.encode())
    return node


def _lines(n: int, prefix: str = "line") -> str:
    return "\n".join(f"{prefix} {i}" for i in range(n))


def _check(tool: ToolNode, expected: str, *, cut: bool = False) -> None:
    """The row is ``expected``; ``cut`` lays it out exactly as wide as its widest line."""
    width = max(len(line) for line in expected.splitlines()) if cut else 200
    assert row_text(tool, width) == expected


# --- read ---------------------------------------------------------------------------


def test_read_whole_file_under_home():
    path = "/home/archie/.nexus/brain/projects/archie/nexus-roadmap/tui-sidebar.md"
    result = f"File: {path} (80 lines)\n\n" + _lines(80)
    _check(
        _tool("read", {"path": path}, result, ms=6),
        "● Read · 80 lines · 6ms\n╰ ~/.nexus/brain/projects/archie/nexus-roadmap/tui-sidebar.md",
    )


def test_read_a_range():
    path = "/workspace/archie-nexus/shared/src/archie_shared/transcript.py"
    result = (
        f"File: {path} (880 lines)\nShowing lines 120-260 of 880\n"
        "Use offset=261 to continue reading\n\n" + _lines(141)
    )
    _check(
        _tool("read", {"path": path, "offset": 120, "limit": 141}, result, ms=4),
        "● Read · 141 of 880 lines · 4ms\n"
        "╰ archie-nexus/shared/src/archie_shared/transcript.py:120-260",
    )


def test_read_an_image_shows_its_original_size():
    result = "Image: /tmp/proto/act.png (PNG, 1568x1061, resized from 1700x1150, 312000 bytes)"
    _check(
        _tool("read", {"path": "/tmp/proto/act.png"}, result, ms=9),
        "● Read · image 1700×1150 · 9ms\n╰ /tmp/proto/act.png",
    )


def test_read_error_drops_the_class_and_the_repeated_target():
    result = "ToolFileNotFoundError: File not found: /tmp/proto/02-blocked.png"
    _check(
        _tool("read", {"path": "/tmp/proto/02-blocked.png"}, result, error=True, ms=6),
        "✕ Read · 6ms · File not found\n╰ /tmp/proto/02-blocked.png",
    )


def test_read_from_an_offset_to_the_end():
    result = "File: a.py (50 lines)\n\n" + _lines(11)
    _check(
        _tool("read", {"path": "a.py", "offset": 40}, result, ms=3),
        "● Read · 11 of 50 lines · 3ms\n╰ a.py:40-50",
    )


def test_running_read_shows_the_requested_range():
    _check(
        _tool("read", {"path": "/workspace/a.py", "offset": 10, "limit": 5}),
        "⠋ Read\n╰ a.py:10-14",
    )


# --- write and edit -------------------------------------------------------------------


def test_write_counts_the_content_lines():
    path = "/workspace/archie-nexus/cli/src/archie_cli/tui/prototype/__init__.py"
    _check(
        _tool("write", {"path": path, "content": _lines(8)}, f"Written: {path} (8 lines)", ms=8),
        "● Write · 8 lines · 8ms\n╰ archie-nexus/cli/src/archie_cli/tui/prototype/__init__.py",
    )


def test_edit_counts_added_and_removed_lines():
    path = "/workspace/archie-nexus/cli/src/archie_cli/tui/prototype/data.py"
    diff = "--- a/x\n+++ b/x\n@@ -1,3 +1,6 @@\n ctx\n-old\n+a\n+b\n+c\n+d\n ctx\n"
    tool = _tool("edit", {"path": path, "old": "old", "new": "a"}, diff, ms=7)
    _check(tool, "● Edit · +4 −1 · 7ms\n╰ archie-nexus/cli/src/archie_cli/tui/prototype/data.py")
    # Green additions, red removals.
    assert tool_row(tool).metrics == [(Span("+4", "added"), Span(" "), Span("−1", "removed"))]


def test_edit_error():
    path = "/workspace/core/platform-services/waf/tests/main.tftest.hcl"
    _check(
        _tool("edit", {"path": path}, "ValueError: Text not found in file", error=True, ms=2),
        "✕ Edit · 2ms · Text not found in file\n╰ core/platform-services/waf/tests/main.tftest.hcl",
    )


# --- glob, grep, code -------------------------------------------------------------------


def test_glob_one_file():
    tool = _tool(
        "glob",
        {"pattern": "agent/src/archie_agent/**/shell*.py", "path": "/workspace/archie-nexus"},
        "1 files, most recent first\n\nagent/src/archie_agent/exec/tools/shell.py",
        ms=26,
    )
    _check(tool, "● Glob · 1 file · 26ms\n╰ agent/src/archie_agent/**/shell*.py in archie-nexus")


def test_glob_no_files():
    tool = _tool(
        "glob",
        {"pattern": "**/*.tcss", "path": "archie-nexus/orchestrator"},
        "No files found.",
        ms=12,
    )
    _check(tool, "● Glob · no files · 12ms\n╰ **/*.tcss in archie-nexus/orchestrator")


def test_glob_counts_every_file_when_the_list_is_capped():
    tool = _tool("glob", {"pattern": "*"}, "100 files shown of 340, most recent first.", ms=5)
    _check(tool, "● Glob · 340 files · 5ms\n╰ *")


def test_grep_matches_in_one_file():
    path = "/workspace/archie-nexus/shared/src/archie_shared/events.py"
    result = (
        "archie-nexus/shared/src/archie_shared/events.py:\n"
        "  10| class PendingQuestion(Struct):\n"
        "  20| class QuestionOption(Struct):\n"
        "  30| class QuestionOptionList(Struct):"
    )
    tool = _tool(
        "grep", {"pattern": "class (PendingQuestion|QuestionOption)", "path": path}, result, ms=14
    )
    _check(
        tool,
        "● Grep · 3 matches in 1 file · 14ms\n"
        '╰ "class (PendingQuestion|QuestionOption)" in archie-nexus/shared/src/archie_shared/events.py',
    )


def test_grep_with_an_include_glob():
    groups = []
    for n, count in enumerate((5, 4, 4, 4, 3, 3)):
        lines = "\n".join(f"  {i + 1}| render_tool_row()" for i in range(count))
        groups.append(f"cli/src/f{n}.py:\n{lines}")
    tool = _tool(
        "grep",
        {"pattern": "render_tool_row", "path": "archie-nexus/cli/src", "include": "*.py"},
        "\n\n".join(groups),
        ms=31,
    )
    _check(
        tool,
        '● Grep · 23 matches in 6 files · 31ms\n╰ "render_tool_row" in archie-nexus/cli/src · *.py',
    )


def test_grep_error_drops_the_repeated_path():
    root = "archie-nexus/orchestrator/src/archie_orchestrator/metrics"
    tool = _tool(
        "grep",
        {"pattern": "def default_index|def connect", "path": f"/workspace/{root}"},
        f"FileNotFoundError: Search path does not exist: /workspace/{root}",
        error=True,
        ms=9,
    )
    _check(
        tool,
        f'✕ Grep · 9ms · Search path does not exist\n╰ "def default_index|def connect" in {root}',
    )


def test_grep_no_matches():
    tool = _tool("grep", {"pattern": "zzz"}, "No matches found.", ms=4)
    _check(tool, '● Grep · no matches · 4ms\n╰ "zzz"')


def test_code_structure_counts_files():
    tool = _tool(
        "code", {"path": "archie-nexus/cli"}, "archie-nexus/cli/ (22 files, .py 20)\n src/", ms=16
    )
    _check(tool, "● Code · 22 files · 16ms\n╰ archie-nexus/cli")


def test_code_name_search_counts_symbols():
    result = "\n".join(
        f"cli/src/archie_cli/tui/m{i}.py:{i}-{i + 9} — class SessionList{i}:" for i in range(4)
    )
    tool = _tool("code", {"path": "archie-nexus/cli", "name": "SessionList"}, result, ms=9)
    _check(tool, '● Code · 4 symbols · 9ms\n╰ "SessionList" in archie-nexus/cli')


def test_code_file_outline_counts_symbols():
    result = "a.py (python, 40 lines)\nclass A [class, line 1-20]\n  def f(self) [method, line 2-5]"
    tool = _tool("code", {"path": "a.py"}, result, ms=3)
    _check(tool, "● Code · 2 symbols · 3ms\n╰ a.py")


def test_code_error_cuts_the_path_in_the_middle():
    path = "/workspace/archie-nexus/.git/build-it-worktrees/079-question-tool/shared/src/archie_shared/transcript"
    tool = _tool(
        "code", {"path": path}, f"FileNotFoundError: Path not found: {path}", error=True, ms=1
    )
    _check(
        tool,
        "✕ Code · 1ms · Path not found\n"
        "╰ archie-nexus/…/079-question-tool/shared/src/archie_shared/transcript",
        cut=True,
    )


# --- shell and exec --------------------------------------------------------------------


def test_shell_cut_keeps_the_working_directory():
    command = (
        "git status --short | head; git log --oneline -5; git branch --show-current; "
        "wc -l cli/src/archie_cli/tui/*.py"
    )
    tool = _tool(
        "shell",
        {"command": command, "cwd": "/workspace/archie-nexus"},
        "[exit: 0]\n" + _lines(45),
        ms=123,
    )
    _check(
        tool,
        "● Shell · 45 lines · 123ms\n"
        "╰ $ git status --short | head; git log --oneline -5; git branch --show-cur… · in archie-nexus",
        cut=True,
    )


def test_shell_strips_the_workspace_from_the_command():
    tool = _tool(
        "shell",
        {"command": "cd /workspace/archie-nexus && git log --oneline -3\necho done"},
        "[exit: 0]\na\nb\nc",
        ms=41,
    )
    _check(tool, "● Shell · 3 lines · 41ms\n╰ $ cd archie-nexus && git log --oneline -3")


def test_running_shell_shows_the_spinner_and_no_elapsed_time():
    tool = _tool(
        "shell",
        {"command": "scripts/dev test tests/test_tui_prototype.py", "cwd": "archie-nexus"},
    )
    assert tool_row(tool).glyph == "running"
    _check(tool, "⠋ Shell\n╰ $ scripts/dev test tests/test_tui_prototype.py · in archie-nexus")
    first, _ = row_lines(tool_row(tool), 80, spinner="⠙")
    assert first[0] == ("⠙", "glyph")


def test_failed_shell_shows_exit_code_and_last_output_line():
    tool = _tool(
        "shell",
        {
            "command": "scripts/dev test tests/test_tui_prototype.py",
            "cwd": "/workspace/archie-nexus",
        },
        "[exit: 1]\n" + _lines(30) + "\nFAILED tests/x.py::t\n1 failed, 10 passed in 8.91s\n",
        error=True,
        ms=9200,
    )
    _check(
        tool,
        "✕ Shell · exit 1 · 9.2s · 1 failed, 10 passed in 8.91s\n"
        "╰ $ scripts/dev test tests/test_tui_prototype.py · in archie-nexus",
    )


def test_failed_shell_is_failed_without_the_error_flag():
    tool = _tool("shell", {"command": "false"}, "[exit: 2]\nboom", ms=5)
    assert tool_row(tool).glyph == "error"
    _check(tool, "✕ Shell · exit 2 · 5ms · boom\n╰ $ false")


def test_shell_timeout():
    tool = _tool(
        "shell",
        {"command": "archie serve"},
        "$ archie serve\n[error: command timed out after 30s and was killed]",
        error=True,
        ms=30_012,
    )
    _check(tool, "✕ Shell · timed out after 30s\n╰ $ archie serve")


def test_shell_with_no_output_and_a_bad_working_directory():
    _check(
        _tool("shell", {"command": "true"}, "[exit: 0]", ms=3),
        "● Shell · no output · 3ms\n╰ $ true",
    )
    tool = _tool(
        "shell",
        {"command": "ls", "cwd": "/workspace/gone"},
        "ToolFileNotFoundError: Working directory not found: /workspace/gone",
        error=True,
        ms=1,
    )
    _check(tool, "✕ Shell · 1ms · Working directory not found: gone\n╰ $ ls · in gone")


def test_exec_counts_returned_lines_and_source_lines():
    tool = _tool("exec", {"source": _lines(9)}, "42", ms=273)
    _check(tool, "● Exec · returned 1 line · 273ms\n╰ python · 9 lines")


def test_exec_error_keeps_the_exception_class():
    result = "error: KeyError: 'team'\n\ntraceback:\nTraceback (most recent call last):\n  ..."
    tool = _tool("exec", {"source": _lines(14)}, result, error=True, ms=1100)
    _check(tool, "✕ Exec · 1.1s · KeyError: 'team'\n╰ python · 14 lines")


# --- web, brain, skill -----------------------------------------------------------------

URL = "https://kubernetes-sigs.github.io/aws-load-balancer-controller/latest/guide/ingress/ingress_class/"


def test_fetch_counts_lines():
    tool = _tool("web_fetch", {"url": URL}, _lines(97), ms=229)
    _check(tool, f"● Fetch · 97 lines · 229ms\n╰ {URL.removeprefix('https://')}")


def test_fetch_with_search_terms_keeps_them_when_the_url_is_cut():
    tool = _tool(
        "web_fetch",
        {"url": URL, "search_terms": "loadBalancerAttributes"},
        _lines(41),
        ms=229,
    )
    _check(
        tool,
        "● Fetch · 41 lines · 229ms\n"
        '╰ kubernetes-sigs.github.io/…/guide/ingress/ingress_class/ · "loadBalancerAttributes"',
        cut=True,
    )


def test_fetch_error_cuts_the_url_in_the_middle():
    url = "https://cdn.paralect.com/files/nx7d2f9a0c4e4b1f8e/1791486821317-herdr-agents-sidebar.jpg"
    tool = _tool(
        "web_fetch",
        {"url": url},
        "ValueError: Non-text content type: image/jpeg",
        error=True,
        ms=204,
    )
    _check(
        tool,
        "✕ Fetch · 204ms · Non-text content type: image/jpeg\n"
        "╰ cdn.paralect.com/…/1791486821317-herdr-agents-sidebar.jpg",
        cut=True,
    )


def test_web_search_results_and_none():
    results = "\n\n".join(f"{i}. Title {i}\n   https://x/{i}\n   snippet" for i in range(1, 9))
    tool = _tool("web_search", {"query": "textual command palette providers"}, results, ms=1400)
    _check(tool, '● Web search · 8 results · 1.4s\n╰ "textual command palette providers"')
    query = "kiro cli changelog ACP tool_call rawInput MCP deferred tools"
    tool = _tool("web_search", {"query": query}, "No results found.", ms=2700)
    _check(tool, f'● Web search · no results · 2.7s\n╰ "{query}"')


def test_brain_search_counts_entries():
    result = json.dumps([{"path": f"e{i}.md"} for i in range(10)])
    tool = _tool("brain_search", {"query": "tui sidebar"}, result, ms=108)
    _check(tool, '● Brain search · 10 results · 108ms\n╰ "tui sidebar"')


def test_skill_body_references_and_already_loaded():
    _check(
        _tool("skill", {"name": "notion"}, _lines(316), ms=23),
        "● Skill · 316 lines · 23ms\n╰ notion",
    )
    refs = ["references/plan-template.md", "references/review.md"]
    _check(
        _tool("skill", {"name": "build-it", "references": refs}, "<reference>…", ms=41),
        "● Skill · 2 references · 41ms\n"
        "╰ build-it · references/plan-template.md, references/review.md",
    )
    already = "Skill 'notion' content is already available in earlier tool results. Use it."
    _check(
        _tool("skill", {"name": "notion"}, already, ms=2),
        "● Skill · already loaded · 2ms\n╰ notion",
    )


# --- question and task -----------------------------------------------------------------

QUESTION = {"question": "What kind of demonstration would you prefer?", "options": []}


def test_question_waiting_answered_and_dismissed():
    waiting = _tool("question", QUESTION)
    assert tool_row(waiting).glyph == "waiting"
    _check(waiting, f"? Question · waiting for you\n╰ {QUESTION['question']}")
    answered = _tool(
        "question", QUESTION, f"{QUESTION_SELECTED_PREFIX}1: A coding choice", ms=15_000
    )
    _check(answered, f"● Question · → A coding choice · 15.0s\n╰ {QUESTION['question']}")
    dismissed = _tool("question", QUESTION, QUESTION_DISMISSED_RESULT, ms=15_000)
    _check(dismissed, f"● Question · dismissed · 15.0s\n╰ {QUESTION['question']}")


def _tasks(n: int, agent: str) -> dict:
    return {"tasks": [{"agent": agent, "prompt": f"p{i}"} for i in range(n)]}


def test_running_task_counts_finished_subagents():
    children = (
        SubagentNode(id="s0", scope="t", index=0, agent="archie-worker", status="complete"),
        SubagentNode(id="s1", scope="t", index=1, agent="archie-worker"),
    )
    tool = _tool("task", _tasks(2, "archie-worker"), children=children)
    _check(tool, "⠋ Task · 1 of 2 done\n╰ 2 × archie-worker")


def test_finished_and_cancelled_tasks():
    _check(
        _tool("task", _tasks(4, "archie-worker"), "all done", ms=155_000),
        "● Task · 4 agents · 2m35s\n╰ 4 × archie-worker",
    )
    _check(
        _tool("task", _tasks(2, "default"), "cancelled", error=True, ms=900),
        "✕ Task · cancelled\n╰ 2 × default",
    )


def test_any_interrupted_call_shows_cancelled():
    _check(
        _tool("read", {"path": "a.py"}, "cancelled", error=True, ms=4),
        "✕ Read · cancelled\n╰ a.py",
    )


# --- other tools --------------------------------------------------------------------------


def test_other_tools_show_scalar_inputs_and_result_size():
    tool = _tool(
        "notion_search",
        {"query": "roadmap", "limit": 10, "filter": {"a": 1}, "sort": "recent", "x": 1},
        "x" * 1204,
        ms=80,
    )
    _check(tool, '● notion_search · 1,204 chars · 80ms\n╰ query="roadmap", limit=10, sort="recent"')


def test_an_orphan_result_is_a_generic_tool():
    _check(_tool("tool", {}, "ok", ms=2), "● Tool · 2 chars · 2ms\n╰ ")


# --- layout and helpers ------------------------------------------------------------------


def test_every_row_is_two_lines_at_any_width():
    tool = _tool(
        "shell",
        {"command": "x" * 300, "cwd": "/workspace/archie-nexus"},
        "[exit: 1]\n" + "y" * 300,
        ms=5,
    )
    for width in (1, 5, 20, 60, 400):
        first, second = row_text(tool, width).split("\n")
        assert len(first) <= width and len(second) <= width


def test_line_one_is_cut_at_the_error():
    tool = _tool("shell", {"command": "make"}, "[exit: 2]\n" + "e" * 100, ms=5)
    first = row_text(tool, 40).split("\n")[0]
    assert first == "✕ Shell · exit 2 · 5ms · " + "e" * 14 + "…"
    assert len(first) == 40


@pytest.mark.parametrize(
    ("ms", "text"),
    [(6, "6ms"), (999, "999ms"), (1_700, "1.7s"), (59_000, "59.0s"), (155_000, "2m35s")],
)
def test_format_duration(ms: int, text: str):
    assert format_duration(ms) == text


def test_strip_workspace_matches_whole_components():
    assert (
        strip_workspace("cd /workspace/archie-nexus && ls /workspace") == "cd archie-nexus && ls ."
    )
    assert (
        strip_workspace("/srv/workspace/x and /workspaces/y")
        == "/srv/workspace/x and /workspaces/y"
    )


def test_display_path():
    assert display_path("/workspace") == ""
    assert display_path("/workspace/") == ""
    assert display_path("/workspace/archie-nexus/cli/") == "archie-nexus/cli"
    assert display_path("/home/archie/.nexus/x.md") == "~/.nexus/x.md"
    assert display_path("/home/archie") == "~"
    assert display_path("/tmp/a") == "/tmp/a"
    assert display_path(None) == ""


def test_elide_path_keeps_the_file_name():
    path = "archie-nexus/cli/src/archie_cli/tui/prototype/data.py"
    assert elide_path(path, 100) == path
    assert elide_path(path, 32) == "archie-nexus/…/prototype/data.py"
    assert elide_path(path, 12) == "…/data.py"
    assert elide_path(path, 6) == "ar….py"
    assert len(elide_path(path, 6)) == 6
