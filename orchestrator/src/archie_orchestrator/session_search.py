"""Parse and apply the session log viewer's search query.

Syntax (all terms AND, case-insensitive):

- ``"quoted phrase"`` and ``word`` — substrings of user/assistant message text,
  session ID, or workspace. An unclosed quote runs to the end of the query.
- ``after:YYYY-MM-DD`` — session started on or after that UTC day.
- ``before:YYYY-MM-DD`` — session started before that UTC day.
- ``workspace:value`` — workspace equals or starts with ``value``.

Unknown ``key:value`` terms are plain words. An invalid date is ignored and
reported in ``Query.notices`` so typing an incomplete date never empties the list.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

from archie_orchestrator.session_index import SessionSummary

_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_FILTER_KEYS = ("before", "after", "workspace")


@dataclass
class Query:
    phrases: list[str] = field(default_factory=list)
    words: list[str] = field(default_factory=list)
    before: date | None = None
    after: date | None = None
    workspace: str | None = None
    notices: list[str] = field(default_factory=list)


def _tokens(text: str) -> list[tuple[str, bool]]:
    """Split into (token, quoted) pairs."""
    tokens: list[tuple[str, bool]] = []
    i, n = 0, len(text)
    while i < n:
        if text[i].isspace():
            i += 1
        elif text[i] == '"':
            end = text.find('"', i + 1)
            if end < 0:
                end = n
            tokens.append((text[i + 1 : end], True))
            i = end + 1
        else:
            start = i
            while i < n and not text[i].isspace() and text[i] != '"':
                i += 1
            tokens.append((text[start:i], False))
    return tokens


def _parse_date(value: str) -> date | None:
    if not _DATE.match(value):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def parse_query(text: str) -> Query:
    query = Query()
    for raw, quoted in _tokens(text):
        token = raw.strip().lower()
        if not token:
            continue
        if quoted:
            query.phrases.append(token)
            continue
        key, sep, value = token.partition(":")
        if not sep or key not in _FILTER_KEYS or not value:
            query.words.append(token)
            continue
        if key == "workspace":
            query.workspace = value
            continue
        parsed = _parse_date(value)
        if parsed is None:
            query.notices.append(f"Ignored {raw}: dates must be YYYY-MM-DD.")
        elif key == "before":
            query.before = parsed
        else:
            query.after = parsed
    return query


def matches(summary: SessionSummary, query: Query) -> bool:
    if query.workspace is not None and not summary.workspace.lower().startswith(query.workspace):
        return False
    if query.before is not None or query.after is not None:
        if summary.started_at is None:
            return False
        day = summary.started_at.date()
        if query.after is not None and day < query.after:
            return False
        if query.before is not None and day >= query.before:
            return False
    text = summary.search_text
    return all(term in text for term in (*query.phrases, *query.words))
