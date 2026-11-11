"""Mock sessions for the TUI prototype, built from historical session logs.

The sidebar list comes from the metrics index (``metrics.db``), opened read-only, as a
stand-in for the planned ``/api/sessions`` feed. When the index is missing or
unreadable, the newest session logs are scanned instead. A session's transcript is
loaded on first view by replaying its log through ``TranscriptBuilder``.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from archie_shared import events as ce
from archie_shared.events import PendingQuestion, QuestionOption, decode_event
from archie_shared.models import load_models
from archie_shared.transcript import TranscriptBuilder

type MockState = Literal["working", "blocked", "idle", "error", "ended"]

# Mock states handed out in recency order, so a default run shows every case.
# Sessions past the end of the script are idle (ready). Ended sessions aren't mocked:
# the sidebar shows live sessions only, and finding old ones is a session browser's
# job (out of scope here).
STATE_SCRIPT: tuple[tuple[MockState, bool], ...] = (
    ("working", False),
    ("blocked", True),
    ("idle", True),
    ("working", False),
    ("error", True),
    ("idle", False),
    ("idle", False),
)

# Sessions with fewer requests than this are skipped by default: they make dull mocks.
MIN_REQUESTS = 10


@dataclass
class Accounting:
    """Session totals from ``llm_request`` events, as the status bar shows them."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read: int = 0
    cache_write: int = 0
    context_tokens: int = 0
    context_pct: float = 0.0
    cost_usd: float = 0.0
    credits: float | None = None


@dataclass
class MockSession:
    """One sidebar entry: index metadata, a mock live state, and the loaded transcript."""

    session_id: str
    workspace: str
    title: str
    log_path: Path | None
    started_at: datetime | None
    last_activity: datetime | None
    cost_usd: float
    subagent_count: int
    model_key: str
    state: MockState = "ended"
    unseen: bool = False
    question: PendingQuestion | None = None
    transcript: TranscriptBuilder | None = None
    accounting: Accounting = field(default_factory=Accounting)
    last_question: PendingQuestion | None = None  # the log's last root question, if any
    name: str = ""  # set by the user; shown instead of the session id

    @property
    def display_name(self) -> str:
        """The user's name for the session, or its id. Never the first prompt: too long."""
        return self.name or self.session_id

    @property
    def needs_attention(self) -> bool:
        return self.state in ("blocked", "error") or (self.state == "idle" and self.unseen)

    @property
    def read_only(self) -> bool:
        return self.state == "ended"

    @property
    def started_key(self) -> float:
        """Sort key for oldest-first order; sessions without a start time go last."""
        return self.started_at.timestamp() if self.started_at else float("inf")

    @property
    def activity_key(self) -> float:
        return self.last_activity.timestamp() if self.last_activity else 0.0

    @property
    def loaded(self) -> bool:
        return self.transcript is not None


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _title(text: str) -> str:
    line = next((part.strip() for part in text.splitlines() if part.strip()), "")
    return line or "(untitled)"


def _index_sessions(db_path: Path, sessions_dir: Path) -> list[MockSession] | None:
    """Every indexed session with its request count, or None if the index can't be read."""
    if not db_path.is_file():
        return None
    query = """
        SELECT s.session_id, s.workspace, s.title, s.started_at, s.last_activity_at,
               s.initial_model,
               COALESCE((SELECT SUM(r.cost_usd) FROM requests r
                         WHERE r.session_id = s.session_id), 0) AS cost,
               (SELECT COUNT(*) FROM requests r WHERE r.session_id = s.session_id) AS requests,
               (SELECT COUNT(*) FROM subagents a WHERE a.session_id = s.session_id) AS subagents
        FROM sessions s
        ORDER BY s.last_activity_at DESC
    """
    try:
        conn = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True, timeout=2.0)
        try:
            rows = conn.execute(query).fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return None
    sessions = []
    for sid, workspace, title, started, last, model, cost, requests, subagents in rows:
        if requests < MIN_REQUESTS:
            continue
        sessions.append(
            MockSession(
                session_id=sid,
                workspace=workspace,
                title=_title(title or ""),
                log_path=sessions_dir / f"{sid}.jsonl",
                started_at=_parse_time(started),
                last_activity=_parse_time(last),
                cost_usd=float(cost or 0.0),
                subagent_count=int(subagents or 0),
                model_key=model or "",
            )
        )
    return sessions


def _scan_log(path: Path) -> MockSession | None:
    """Header fields from the start of one log (fallback when there is no index)."""
    started = model = title = None
    try:
        with path.open(encoding="utf-8") as fh:
            for n, line in enumerate(fh):
                if n > 200 or title is not None:
                    break
                try:
                    event = decode_event(line, persisted=True)
                except Exception:  # noqa: BLE001 — legacy or malformed lines
                    continue
                if isinstance(event, ce.SessionStarted):
                    started, model = event.sent_at, event.model_key
                elif isinstance(event, ce.UserMessage) and event.scope is None:
                    title = _title(event.content)
    except OSError:
        return None
    if title is None:
        return None
    sid = path.stem
    return MockSession(
        session_id=sid,
        workspace=sid.rsplit("-", 1)[0],
        title=title,
        log_path=path,
        started_at=_parse_time(started),
        last_activity=datetime.fromtimestamp(path.stat().st_mtime, tz=UTC),
        cost_usd=0.0,
        subagent_count=0,
        model_key=model or "",
    )


def _log_sessions(sessions_dir: Path, limit: int) -> list[MockSession]:
    paths = sorted(sessions_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    found: list[MockSession] = []
    for path in paths:
        session = _scan_log(path)
        if session is not None:
            found.append(session)
        if len(found) >= limit:
            break
    return found


def discover(home: Path, *, limit: int, session_ids: tuple[str, ...] = ()) -> list[MockSession]:
    """Pick the mock sessions and give each one a scripted state.

    With ``session_ids`` (exact ids or unique prefixes), those sessions are used in the
    given order; otherwise the ``limit`` most recently active sessions with at least
    ``MIN_REQUESTS`` requests.
    """
    sessions_dir = home / "sessions"
    indexed = _index_sessions(home / "metrics.db", sessions_dir)
    if session_ids:
        pool = indexed if indexed is not None else _log_sessions(sessions_dir, 10_000)
        chosen = []
        for wanted in session_ids:
            match = next((s for s in pool if s.session_id.startswith(wanted)), None)
            if match is None:
                path = sessions_dir / f"{wanted}.jsonl"
                match = _scan_log(path) if path.is_file() else None
            if match is not None and match not in chosen:
                chosen.append(match)
    elif indexed is not None:
        chosen = indexed[:limit]
    else:
        chosen = _log_sessions(sessions_dir, limit)
    chosen = [s for s in chosen if s.log_path is not None and s.log_path.is_file()]
    now = datetime.now(UTC)
    for n, session in enumerate(chosen):
        session.state, session.unseen = (
            STATE_SCRIPT[n] if n < len(STATE_SCRIPT) else ("idle", False)
        )
        # Live sessions were active recently, whatever the log's real age.
        session.last_activity = now - timedelta(minutes=3 * n)
    return chosen


def load(session: MockSession) -> None:
    """Replay the session log into a transcript and status-bar totals.

    Blocking: the app runs it in a thread. Mirrors the attach TUI's replay path
    (``ArchieApp._accumulate_ledger``) without the network or the widgets.
    """
    builder = TranscriptBuilder()
    acct = Accounting()
    model_key = session.model_key
    seen: set[str] = set()
    question: PendingQuestion | None = None
    if session.log_path is not None:
        with session.log_path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = decode_event(line, persisted=True)
                except Exception:  # noqa: BLE001 — skip legacy or malformed lines
                    continue
                if event.id in seen:
                    continue
                seen.add(event.id)
                if isinstance(event, ce.SessionStarted):
                    model_key = event.model_key
                elif isinstance(event, ce.LLMRequest):
                    model_key = _accumulate(acct, event, model_key)
                elif isinstance(event, ce.ToolCall) and event.name == "question":
                    question = _question_from_input(event.tool_use_id, event.input) or question
                builder.apply(event)
    model = load_models().get(model_key)
    if model is not None and model.context > 0:
        acct.context_pct = acct.context_tokens / model.context * 100
    session.model_key = model_key
    session.accounting = acct
    session.cost_usd = acct.cost_usd or session.cost_usd
    session.subagent_count = len(builder.subagents)
    session.last_question = question
    session.transcript = builder


def _accumulate(acct: Accounting, event: ce.LLMRequest, model_key: str) -> str:
    acct.input_tokens += event.input_tokens
    acct.output_tokens += event.output_tokens
    acct.cache_read += event.cache_read_tokens
    acct.cache_write += event.cache_write_tokens
    acct.cost_usd += event.cost_usd
    if isinstance(event.credits, int | float):
        acct.credits = (acct.credits or 0.0) + event.credits
    if event.scope is None and event.subagent_index is None and event.purpose != "compaction":
        acct.context_tokens = event.context_tokens
        return event.model_key
    return model_key


def _question_from_input(tool_use_id: str, data: dict) -> PendingQuestion | None:
    """The ``question`` tool's input as a ``PendingQuestion``, or None if it doesn't fit."""
    try:
        options = [
            QuestionOption(label=str(o["label"]), description=str(o.get("description", "")))
            for o in data["options"]
        ]
        recommended = data.get("recommended")
        return PendingQuestion(
            id=tool_use_id,
            question=str(data["question"]),
            options=options,
            recommended=recommended if isinstance(recommended, int) else None,
        )
    except (KeyError, TypeError):
        return None


def mock_question(session: MockSession) -> PendingQuestion:
    """The question a blocked mock session waits on: the log's last one, or a stock one."""
    if session.last_question is not None:
        return session.last_question
    return PendingQuestion(
        id=f"mock-{session.session_id}",
        question="I've finished investigating. How do you want me to continue?",
        options=[
            QuestionOption("Apply the changes", "Edit the files, then run the checks."),
            QuestionOption("Write a plan first", "Put the approach under plans/ for review."),
            QuestionOption("Stop here", "Leave the findings in the transcript."),
        ],
        recommended=1,
    )


def age(when: datetime | None, now: datetime | None = None) -> str:
    """Compact age: ``now``, ``45s``, ``12m``, ``3h``, ``5d``."""
    if when is None:
        return "—"
    seconds = int(((now or datetime.now(UTC)) - when).total_seconds())
    if seconds < 10:
        return "now"
    for size, unit in ((86_400, "d"), (3_600, "h"), (60, "m")):
        if seconds >= size:
            return f"{seconds // size}{unit}"
    return f"{seconds}s"
