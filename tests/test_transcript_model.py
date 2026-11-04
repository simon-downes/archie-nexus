"""Tests for the shared transcript model (archie_shared.transcript)."""

from archie_shared.events import (
    CompactionComplete,
    CompactionStart,
    IterationStart,
    TextDelta,
    TurnComplete,
    TurnError,
    TurnInterrupted,
)
from archie_shared.transcript import (
    CHILD_CONTENT_FILTERED_MESSAGE,
    CompactionNode,
    IterationNode,
    NoticeNode,
    StreamNode,
    SubagentNode,
    TextNode,
    ToolNode,
    TranscriptBuilder,
    build,
    source_preview,
    subagent_status_text,
    tool_detail_sections,
    tool_headline,
    tool_preview,
)


def _delta(text, *, event_id, scope=None, index=None, request_id="r1", iteration=1):
    return TextDelta(
        id=event_id,
        turn=1,
        iteration=iteration,
        scope=scope,
        request_id=request_id,
        text=text,
        subagent_index=index,
    )


def _iterations(items):
    return [item for item in items if isinstance(item, IterationNode)]


# -- iteration grouping ------------------------------------------------------


def test_parallel_tool_calls_share_one_iteration(session_logs):
    w = session_logs
    b = build(
        [
            w.user("go"),
            w.iteration(iteration=1),
            w.tool_call("read", {"path": "a"}, tool_use_id="t1", iteration=1),
            w.tool_call("read", {"path": "b"}, tool_use_id="t2", iteration=1),
            w.iteration(iteration=2),
            w.tool_call("read", {"path": "c"}, tool_use_id="t3", iteration=2),
        ]
    )
    groups = _iterations(b.turns[0].items)
    assert [[t.tool_use_id for t in g.tools] for g in groups] == [["t1", "t2"], ["t3"]]


def test_text_only_iteration_creates_no_group(session_logs):
    w = session_logs
    b = build([w.user("hi"), w.iteration(), w.assistant("hello")])
    assert _iterations(b.turns[0].items) == []


def test_first_tool_call_appends_group_with_tool_in_one_change(session_logs):
    w = session_logs
    b = TranscriptBuilder()
    b.apply(w.user("go"))
    changes = b.apply(w.tool_call("read", {"path": "a"}, tool_use_id="t1"))
    appends = [c for c in changes if c.kind == "append"]
    assert len(appends) == 1 and isinstance(appends[0].node, IterationNode)
    assert appends[0].parent_id == b.turns[0].items_id
    second = b.apply(w.tool_call("read", {"path": "b"}, tool_use_id="t2"))
    assert [(c.kind, c.parent_id) for c in second] == [("append", appends[0].node.tools_id)]


def test_orphan_tool_result_gets_a_group(session_logs):
    w = session_logs
    b = build([w.user("x"), w.tool_result("out", tool_use_id="tx", is_error=True)])
    (group,) = _iterations(b.turns[0].items)
    assert group.tools[0].status == "error" and group.tools[0].result == "out"


def test_tool_result_replaces_tool(session_logs):
    w = session_logs
    b = TranscriptBuilder()
    b.apply(w.user("go"))
    b.apply(w.tool_call("shell", {"command": "ls"}, tool_use_id="t1"))
    changes = b.apply(w.tool_result("[exit: 0]\na", tool_use_id="t1", duration_ms=5))
    assert [(c.kind, c.target_id) for c in changes] == [("replace", "tool-t1")]
    assert b.tools["t1"].status == "ok" and b.tools["t1"].duration_ms == 5


# -- indexes -----------------------------------------------------------------


def test_indexes_link_nodes_to_their_containers(session_logs):
    w = session_logs
    b = build(
        [
            w.user("go"),
            w.tool_call("task", {"tasks": [{"agent": "general"}]}, tool_use_id="tk"),
            w.tool_call("read", {"path": "a"}, tool_use_id="c1", scope="tk", index=0),
        ]
    )
    child_tool = b.tools["c1"]
    assert list(b.ancestors(child_tool.id)) == [
        "iter-tk-0-1-1",
        "sub-tk-0",
        "tool-tk",
        "iter-1-1",
        b.turns[0].id,
    ]
    assert b.nodes["sub-tk-0"] is b.subagents[("tk", 0)]


# -- streaming ---------------------------------------------------------------


def test_stream_then_final_message_replaces_stream(session_logs):
    w = session_logs
    b = TranscriptBuilder()
    b.apply(w.user("hi"))
    first = b.apply(_delta("Hel", event_id="d1"))
    assert [c.kind for c in first] == ["remove", "append"]
    more = b.apply(_delta("lo", event_id="d2"))
    assert [c.kind for c in more] == ["append_text"] and more[0].text == "lo"
    final = b.apply(w.assistant("Hello"))
    assert [(c.kind, c.target_id) for c in final] == [("replace", "stream-1-1-r1")]
    (item,) = b.turns[0].items
    assert isinstance(item, TextNode) and item.content == "Hello"
    assert b.apply(_delta("late", event_id="d3")) == []


def test_unfinished_stream_becomes_message_when_turn_ends(session_logs):
    w = session_logs
    b = TranscriptBuilder()
    b.apply(w.user("hi"))
    b.apply(_delta("partial", event_id="d1"))
    changes = b.apply(TurnInterrupted(id="ti", turn=1, scope=None))
    assert changes[0].kind == "replace" and changes[0].target_id == "stream-1-1-r1"
    kinds = [type(i) for i in b.turns[0].items]
    assert kinds == [TextNode, NoticeNode]
    assert not any(isinstance(i, StreamNode) for i in b.turns[0].items)


def test_duplicate_events_are_ignored(session_logs):
    w = session_logs
    b = TranscriptBuilder()
    user = w.user("hi")
    assert b.apply(user)
    assert b.apply(user) == []


# -- subagents ---------------------------------------------------------------


def test_subagent_nests_under_task_with_agent_status_and_cost(session_logs):
    w = session_logs
    b = TranscriptBuilder()
    b.apply(w.user("go"))
    b.apply(w.tool_call("task", {"tasks": [{"agent": "general", "prompt": "p"}]}, tool_use_id="tk"))
    first = b.apply(w.user("child prompt", scope="tk", index=0))
    # One append for the new group (holding the prompt), preceded by a remove.
    assert [(c.kind, c.parent_id) for c in first] == [
        ("remove", None),
        ("append", "tool-tk-children"),
    ]
    sub = b.subagents[("tk", 0)]
    assert sub.agent == "general" and sub.label == "general #0"
    assert isinstance(sub.items[0], TextNode)

    b.apply(w.llm(scope="tk", index=0, cost_usd=0.25, context_tokens=900))
    assert sub.cost_usd == 0.25 and sub.context_tokens == 900
    assert b.turns[0].usage.requests == 0  # child requests are not root-turn usage
    assert b.usage.requests == 1

    changes = b.apply(w.tool_call("read", {"path": "x"}, tool_use_id="c1", scope="tk", index=0))
    assert ("replace", sub.row_id) in [(c.kind, c.target_id) for c in changes]
    assert "x" in sub.activity

    b.apply(TurnComplete(id="tc", turn=1, scope="tk", subagent_index=0, stop_reason="end_turn"))
    assert sub.status == "complete" and subagent_status_text(sub) == "Completed"


def test_subagent_error_and_interrupt(session_logs):
    b = TranscriptBuilder()
    b.apply(session_logs.user("go"))
    b.apply(TurnError(id="e", turn=1, scope="s", subagent_index=0, message="boom\nmore"))
    sub = b.subagents[("s", 0)]
    assert sub.status == "error" and sub.error == "boom; more"
    b.apply(TurnInterrupted(id="i", turn=1, scope="s", subagent_index=1))
    assert b.subagents[("s", 1)].status == "interrupted"


def test_child_content_filter_marks_subagent_error(session_logs):
    b = TranscriptBuilder()
    b.apply(session_logs.user("go"))
    b.apply(
        TurnComplete(id="tc", turn=1, scope="s", subagent_index=0, stop_reason="content_filtered")
    )
    sub = b.subagents[("s", 0)]
    assert sub.status == "error" and sub.error == CHILD_CONTENT_FILTERED_MESSAGE
    assert any(isinstance(i, NoticeNode) for i in sub.items)


def test_root_content_filter_adds_error_notice(session_logs):
    b = TranscriptBuilder()
    b.apply(session_logs.user("go"))
    b.apply(TurnComplete(id="tc", turn=1, scope=None, stop_reason="content_filtered"))
    (notice,) = b.turns[0].items
    assert notice.variant == "turn-error" and "content filter" in notice.detail
    assert b.turns[0].stop_reason == "content_filtered"


def test_child_iteration_start_creates_subagent_without_items(session_logs):
    b = TranscriptBuilder()
    b.apply(session_logs.user("go"))
    b.apply(IterationStart(id="it", turn=1, iteration=1, scope="s", subagent_index=0))
    assert b.subagents[("s", 0)].items == []


# -- compaction ----------------------------------------------------------------


def test_compaction_start_then_complete_updates_one_node(session_logs):
    b = TranscriptBuilder()
    b.apply(session_logs.user("go"))
    b.apply(
        CompactionStart(
            id="cs",
            turn=1,
            iteration=1,
            scope=None,
            request_id="rq",
            reason="threshold",
            tokens_before=1000,
            estimated_next_request_cost_usd=1.5,
            model_key="m",
            sent_at="t",
        )
    )
    (node,) = b.turns[0].items
    assert isinstance(node, CompactionNode) and node.status == "pending"
    changes = b.apply(
        CompactionComplete(
            id="cc",
            turn=1,
            iteration=1,
            scope=None,
            request_id="rq",
            reason="threshold",
            status="success",
            tokens_before=1000,
            tokens_after=100,
            cost_saved_usd=0.5,
            duration_ms=9,
            error=None,
            summary="s",
        )
    )
    assert [(c.kind, c.target_id) for c in changes] == [("replace", node.id)]
    assert node.status == "success" and node.tokens_after == 100
    assert node.estimated_next_request_cost_usd == 1.5


# -- presentation helpers -----------------------------------------------------


def test_source_preview_caps_lines():
    lines, omitted = source_preview("\n".join(f"l{i}" for i in range(25)))
    assert lines == [f"l{i}" for i in range(10)] and omitted == 15
    assert source_preview("") == ([], 0)


def test_tool_preview_for_exec_and_shell():
    source = "\n".join(f"x = {i}" for i in range(25))
    exec_tool = ToolNode("tool-e", "e", "exec", {"source": source})
    lines, omitted = tool_preview(exec_tool)
    assert len(lines) == 10 and omitted == 15
    shell = ToolNode("tool-s", "s", "shell", {"command": "set -e\n" + "echo hi\n" * 12})
    lines, omitted = tool_preview(shell)
    assert len(lines) == 9 and omitted == 3  # first line is in the headline
    assert "set -e" in tool_headline(shell) and "echo" not in tool_headline(shell)
    assert tool_preview(ToolNode("tool-r", "r", "read", {"path": "a"})) == ([], 0)


def test_exec_headline_shows_metrics_or_error():
    tool = ToolNode("tool-e", "e", "exec", {"source": "x"})
    assert tool_headline(tool) == "[bold]Exec[/]"
    tool.result, tool.status, tool.duration_ms, tool.result_lines = "ok", "ok", 1500, 3
    assert tool_headline(tool) == "[bold]Exec[/] [dim]1.5s · 3 lines[/]"
    tool.status, tool.result = "error", "Traceback [x]\nmore"
    assert "Traceback \\[x\\]" in tool_headline(tool)


def test_detail_sections_choose_languages():
    exec_tool = ToolNode("a", "a", "exec", {"source": "print(1)"}, status="ok", result='{"a": 1}')
    sections = tool_detail_sections(exec_tool)
    assert [(s.title, s.language) for s in sections] == [("Source", "python"), ("Result", "json")]

    # exec's compact "return: {...}" is indented; trailing stdout gets its own block.
    exec_tool.result = 'return: {"a": [1, 2]}\n\nstdout:\nhi'
    sections = tool_detail_sections(exec_tool)
    assert [(s.title, s.language) for s in sections[1:]] == [("Result", "json"), ("Output", None)]
    assert sections[1].text == '{\n  "a": [\n    1,\n    2\n  ]\n}'
    assert sections[2].text == "stdout:\nhi"
    # A truncated return value is not valid JSON and stays as it was.
    exec_tool.result = 'return: {"a": [1, 2\n[…truncated: showed 5 of 9 characters.]'
    assert tool_detail_sections(exec_tool)[1].text == exec_tool.result

    shell = ToolNode("b", "b", "shell", {"command": "ls", "timeout": 5}, result="a\nb", status="ok")
    assert [(s.title, s.language) for s in tool_detail_sections(shell)] == [
        ("Command", "bash"),
        ("Options", "json"),
        ("Result", None),
    ]

    read = ToolNode("c", "c", "read", {"path": "src/app.py"}, result="1| code", status="ok")
    # read results carry a header and line-number prefixes, so they stay plain text
    assert [(s.language, s.filename) for s in tool_detail_sections(read)][1] == (None, None)
    write = ToolNode("w", "w", "write", {"path": "src/app.py", "content": "x = 1"})
    assert tool_detail_sections(write)[0].filename == "src/app.py"

    edit = ToolNode(
        "d", "d", "edit", {"path": "a.ts", "old": "x", "new": "y"}, result="@@", status="ok"
    )
    assert [s.title for s in tool_detail_sections(edit)] == ["Old", "New", "Result"]
    assert tool_detail_sections(edit)[2].language == "diff"

    pending = ToolNode("e", "e", "grep", {"pattern": "x"})
    assert [(s.title, s.language) for s in tool_detail_sections(pending)] == [("Input", "json")]


def test_detail_sections_cap_long_text():
    tool = ToolNode("a", "a", "read", {"path": "big"}, result="x" * 10_000, status="ok")
    result = tool_detail_sections(tool)[1]
    assert len(result.text) == 8000 and result.omitted == 2000


def test_subagent_status_text():
    sub = SubagentNode("s", "s", 0, activity="Reading a")
    assert subagent_status_text(sub) == "Reading a"
    sub.status = "interrupted"
    assert subagent_status_text(sub) == "Interrupted"
