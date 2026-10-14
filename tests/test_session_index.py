"""Tests for the orchestrator session-log index (models, usage, search text)."""

from datetime import UTC, datetime

from archie_orchestrator.session_index import SessionIndex, session_start, summarize_log
from archie_shared.session.log import session_accounting


def test_session_start_decodes_ulid_timestamp():
    assert session_start("archie-01kx77jkz8") == datetime(2026, 7, 11, 0, 0, 1, tzinfo=UTC)
    assert session_start("my-work-space-01m4mzkm00") == datetime(2026, 10, 11, 8, 0, tzinfo=UTC)


def test_session_start_rejects_ids_without_timestamp():
    assert session_start("nohyphen") is None
    assert session_start("ws-notaulid!") is None
    assert session_start("ws-01kx77jk") is None  # too short


def test_models_ordered_by_turn_count(session_logs):
    w = session_logs
    events = [w.user("one", turn=1)]
    for turn in (1, 2, 3):
        events += [w.llm(model_key="m1", turn=turn), w.llm(model_key="m1", turn=turn, iteration=2)]
    events += [w.llm(model_key="m2", turn=4)]
    # Compaction requests are not turns.
    events += [w.llm(model_key="m3", turn=4, purpose="compaction")]
    path = w.write("proj-01kx77jkz8", events)

    summary = summarize_log(path)

    assert summary.models == (("m1", 3), ("m2", 1))


def test_models_count_child_turns_separately(session_logs):
    w = session_logs
    events = [
        w.llm(model_key="root", turn=1),
        w.llm(model_key="child", turn=1, scope="tu-task", index=0),
        w.llm(model_key="child", turn=1, scope="tu-task", index=1),
    ]
    summary = summarize_log(w.write("proj-01kx77jkz8", events))
    assert summary.models == (("child", 2), ("root", 1))


def test_usage_matches_shared_accounting(session_logs):
    w = session_logs
    events = [
        w.user("hello"),
        w.llm(
            input_tokens=100,
            output_tokens=7,
            cache_read_tokens=30,
            cache_write_tokens=4,
            cost_usd=0.0125,
        ),
        w.llm(
            scope="tu-task",
            index=0,
            input_tokens=50,
            output_tokens=3,
            cache_read_tokens=1,
            cache_write_tokens=2,
            cost_usd=0.002,
        ),
        w.llm(purpose="compaction", input_tokens=9, output_tokens=1, cost_usd=0.0001),
    ]
    path = w.write("proj-01kx77jkz8", events)

    summary = summarize_log(path)
    expected = session_accounting(path)

    assert summary.input_tokens == expected["total_input_tokens"] == 159
    assert summary.output_tokens == expected["total_output_tokens"] == 11
    assert summary.cache_read_tokens == expected["total_cache_read_tokens"] == 31
    assert summary.cache_write_tokens == expected["total_cache_write_tokens"] == 6
    assert summary.cost_usd == expected["total_cost"] == 0.0146


def test_search_text_holds_messages_not_tool_output(session_logs):
    w = session_logs
    events = [
        w.user("Please Fix the bug"),
        w.tool_call("shell", {"command": "grep needle"}, tool_use_id="t1"),
        w.tool_result("haystack only in tool output", tool_use_id="t1"),
        w.assistant("Done, it was a Typo"),
        w.user("child prompt text", scope="t1", index=0),
    ]
    summary = summarize_log(w.write("archie-01kx77jkz8", events))

    assert "please fix the bug" in summary.search_text
    assert "done, it was a typo" in summary.search_text
    assert "child prompt text" in summary.search_text
    assert "archie-01kx77jkz8" in summary.search_text
    assert "haystack" not in summary.search_text
    assert "needle" not in summary.search_text


def test_invalid_lines_are_skipped(session_logs):
    w = session_logs
    path = w.write("proj-01kx77jkz8", [w.llm(input_tokens=42)])
    with path.open("a") as f:
        f.write("{not json\n")
    summary = summarize_log(path)
    assert summary.error is None
    assert summary.input_tokens == 42


def test_conflicting_duplicates_mark_summary_unreadable(session_logs):
    w = session_logs
    path = w.dir / "proj-01kx77jkz8.jsonl"
    path.write_text(
        '{"type":"user_message","id":"x","turn":1,"scope":null,"content":"a"}\n'
        '{"type":"user_message","id":"x","turn":1,"scope":null,"content":"b"}\n'
    )
    summary = summarize_log(path)
    assert summary.error is not None
    assert summary.session_id == "proj-01kx77jkz8"
    assert summary.workspace == "proj"


def test_index_rereads_only_changed_files(session_logs, monkeypatch):
    import archie_orchestrator.session_index as module

    w = session_logs
    w.write("a-01kx5yc7g0", [w.llm(input_tokens=1)])
    path_b = w.write("b-01kx77jkz8", [w.llm(input_tokens=2)])
    index = SessionIndex()
    index.summaries(w.dir)

    calls = []
    real = module.summarize_log
    monkeypatch.setattr(module, "summarize_log", lambda p: calls.append(p.name) or real(p))

    from archie_shared.session.log import SessionLog

    SessionLog(path_b).append(w.llm(input_tokens=5))
    by_id = {s.session_id: s for s in index.summaries(w.dir)}

    assert calls == ["b-01kx77jkz8.jsonl"]
    assert by_id["b-01kx77jkz8"].input_tokens == 7
    assert by_id["a-01kx5yc7g0"].input_tokens == 1


def test_index_drops_deleted_files(session_logs):
    w = session_logs
    path = w.write("a-01kx5yc7g0", [w.llm()])
    index = SessionIndex()
    assert len(index.summaries(w.dir)) == 1
    path.unlink()
    assert index.summaries(w.dir) == []


def test_index_missing_directory(tmp_path):
    assert SessionIndex().summaries(tmp_path / "absent") == []
