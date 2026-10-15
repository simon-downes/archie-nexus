"""Tests for the session search query parser and matcher."""

from dataclasses import replace
from datetime import UTC, date, datetime

from archie_orchestrator.session_index import SessionSummary
from archie_orchestrator.session_search import matches, parse_query


def _summary(**kwargs) -> SessionSummary:
    base = SessionSummary(
        session_id="archie-01kx77jkz8",
        workspace="archie",
        started_at=datetime(2026, 7, 11, 0, 0, 1, tzinfo=UTC),
        models=(),
        input_tokens=0,
        cache_read_tokens=0,
        cache_write_tokens=0,
        output_tokens=0,
        cost_usd=0.0,
        search_text="archie-01kx77jkz8\narchie\nplease fix the bug",
        mtime=0.0,
    )
    return replace(base, **kwargs)


def test_parse_full_example():
    q = parse_query('"quoted phrase" word before:2026-10-11 after:2026-07-11 workspace:arch')
    assert q.phrases == ["quoted phrase"]
    assert q.words == ["word"]
    assert q.before == date(2026, 10, 11)
    assert q.after == date(2026, 7, 11)
    assert q.workspace == "arch"
    assert q.notices == []


def test_parse_lowercases_terms_and_keys():
    q = parse_query('"Fix The" BUG Workspace:Archie AFTER:2026-01-02')
    assert q.phrases == ["fix the"]
    assert q.words == ["bug"]
    assert q.workspace == "archie"
    assert q.after == date(2026, 1, 2)


def test_parse_unclosed_quote_runs_to_end():
    q = parse_query('word "open phrase here')
    assert q.words == ["word"]
    assert q.phrases == ["open phrase here"]


def test_parse_empty_and_blank():
    for text in ("", "   ", '""'):
        q = parse_query(text)
        assert q.phrases == [] and q.words == []
        assert q.before is None and q.after is None and q.workspace is None


def test_parse_unknown_key_is_a_word():
    assert parse_query("model:opus").words == ["model:opus"]


def test_parse_empty_workspace_is_a_word():
    q = parse_query("workspace:")
    assert q.workspace is None
    assert q.words == ["workspace:"]


def test_parse_invalid_date_is_ignored_with_notice():
    q = parse_query("before:2026-13-01 after:2026-1")
    assert q.before is None and q.after is None
    assert any("before:2026-13-01" in n for n in q.notices)
    assert any("after:2026-1" in n for n in q.notices)


def test_parse_quoted_text_is_never_a_filter():
    q = parse_query('"workspace:x"')
    assert q.workspace is None
    assert q.phrases == ["workspace:x"]


def test_phrase_matches_case_insensitively_and_exactly():
    q = parse_query('"fix the bug"')
    assert matches(_summary(search_text="please fix the bug"), q)
    assert not matches(_summary(search_text="please fix a bug"), q)


def test_all_terms_must_match():
    s = _summary(search_text="alpha beta")
    assert matches(s, parse_query("alpha beta"))
    assert not matches(s, parse_query("alpha gamma"))


def test_after_is_inclusive_before_is_exclusive():
    s = _summary(started_at=datetime(2026, 7, 11, 0, 0, 1, tzinfo=UTC))
    assert matches(s, parse_query("after:2026-07-11"))
    assert not matches(s, parse_query("after:2026-07-12"))
    assert not matches(s, parse_query("before:2026-07-11"))
    assert matches(s, parse_query("before:2026-07-12"))


def test_date_filter_excludes_sessions_without_start():
    s = _summary(started_at=None)
    assert matches(s, parse_query(""))
    assert not matches(s, parse_query("after:2020-01-01"))


def test_workspace_prefix_or_name():
    q = parse_query("workspace:arch")
    assert matches(_summary(workspace="archie"), q)
    assert matches(_summary(workspace="arch"), q)
    assert matches(_summary(workspace="Archie-Tools"), q)
    assert not matches(_summary(workspace="myarch"), q)
