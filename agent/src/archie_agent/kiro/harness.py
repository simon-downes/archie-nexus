"""Kiro backend harness: runs a session's turns through ``kiro-cli acp`` (077).

``KiroHarness`` has the surface ``app.py`` uses on ``AgentHarness``: ``session``,
``event_bus``, ``log_path``, ``turn_active``, ``try_begin_turn``, ``handle_message``,
``interrupt`` and ``run_manual_compaction``, plus ``start``, ``close`` and
``switch_model``.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import logging
import os
import time
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import msgspec
from archie_shared.config import home_dir
from archie_shared.events import (
    CompactionComplete,
    CompactionStart,
    ErrorNotice,
    SessionStatus,
    ToolResult,
    UserMessage,
)
from archie_shared.events import TurnError as CanonicalTurnError
from archie_shared.kiro import (
    CreditLedger,
    credits_used_since,
    frame_log_path,
    kiro_model_id,
    kiro_model_key,
    period_start,
    startup_error_path,
)
from archie_shared.session.log import LogAppendError, SessionLog
from ulid import ULID

from archie_agent.agents import DEFAULT_AGENT
from archie_agent.event_factory import now_utc
from archie_agent.exec.tool import create_registry
from archie_agent.kiro import agent_files
from archie_agent.kiro.acp import CLIENT_INFO, PROTOCOL_VERSION, AcpClient, AcpError
from archie_agent.kiro.mcp_server import ArchieMcpServer
from archie_agent.kiro.translate import RootContext, TurnTranslator, summarization
from archie_agent.oom import kills_since, oom_kill_count
from archie_agent.prompt import build_kiro_prompt, build_kiro_worker_prompt
from archie_agent.question import Answer, QuestionBroker, QuestionError, create_question_tool
from archie_agent.session_bus import SessionEventBus
from archie_agent.skills import discover_skills

if TYPE_CHECKING:
    from archie_shared.events import PendingQuestion
    from archie_shared.schemas import NexusConfig

    from archie_agent.tool_policy_live import LiveToolPolicy

log = logging.getLogger(__name__)

KIRO_COMMAND = ("kiro-cli", "acp", "--agent-engine=v3")
# Built-in Kiro tool tags the archie agent may advertise: ``read`` is the
# ``disclose_context`` (skill) category. Anything else means Kiro fell back to built-ins.
ALLOWED_BUILTIN_TAGS = frozenset({"read"})
STARTUP_TIMEOUT_S = 30.0
DEAD_MESSAGE = "Kiro backend stopped; start a new session"
# Restart Kiro after an unexpected exit, up to RESTART_LIMIT times in RESTART_WINDOW_S,
# reloading the conversation with ACP session/load (094 D7, D8).
RESTART_LIMIT = 3
RESTART_WINDOW_S = 1800.0
RESTART_TIMEOUT_S = 60.0
# Kiro caps its own summary call at 60 s (091 D4).
COMPACT_TIMEOUT_S = 120.0
COMPACT_BUSY_MESSAGE = "Kiro did not compact: a turn or compaction is in progress"
COMPACT_NOT_CANCELLABLE = "Kiro can't cancel a compaction; it finishes on its own"
QUESTION_TOOL = "question"
QUESTION_ABANDONED = "Kiro ended the question tool call before an answer"


class KiroStartupError(RuntimeError):
    """Kiro could not be started or configured; the session must not run (D17)."""


@dataclass
class KiroSessionState:
    """The parts of ``Session`` that ``app.py`` reads."""

    model_id: str
    session_id: str
    turn_index: int = 0

    def next_turn_index(self) -> int:
        self.turn_index += 1
        return self.turn_index


def write_startup_error(session_id: str, reason: str) -> Path | None:
    """Record why startup failed where the orchestrator can read it (D17)."""
    path = startup_error_path(home_dir(), session_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(reason.rstrip() + "\n", encoding="utf-8")
    except OSError:
        log.warning("Cannot write the Kiro startup-error file %s", path, exc_info=True)
        return None
    return path


def resolve_api_key() -> str | None:
    """The stored ``kiro.api_key`` (from the orchestrator), falling back to ``KIRO_API_KEY``."""
    try:
        from archie_agent.host_config import get_credential

        credential = get_credential("kiro")
    except (KeyError, ValueError) as exc:
        log.warning("Cannot read the kiro credential: %s", exc)
        credential = None
    key = getattr(credential, "api_key", None) if credential is not None else None
    return key or os.environ.get("KIRO_API_KEY") or None


def kiro_question_tool(broker: QuestionBroker):
    """The ``question`` tool for Kiro's MCP server, with a fresh id per call (091 D9).

    The MCP handler never sees ACP's ``toolCallId``; the TUI treats the id as opaque.
    """
    base = create_question_tool(broker)

    async def handler(**kwargs):
        kwargs.pop("_tool_use_id", None)
        return await base.handler(**kwargs, _tool_use_id=str(ULID()))

    return dataclasses.replace(base, handler=handler)


def config_value(options: Any, config_id: str) -> Any:
    """``currentValue`` of one ACP config option, or None."""
    for option in options or ():
        if isinstance(option, dict) and option.get("id") == config_id:
            return option.get("currentValue")
    return None


def config_option(options: Any, config_id: str) -> dict | None:
    for option in options or ():
        if isinstance(option, dict) and option.get("id") == config_id:
            return option
    return None


class KiroHarness:
    """Run Archie turns on Kiro over ACP and write the canonical session log."""

    def __init__(
        self,
        config: NexusConfig,
        *,
        session_id: str,
        log_dir: Path,
        tool_policy: LiveToolPolicy,
        kiro_home: Path | None = None,
        command: Sequence[str] | None = None,
        workspace: str = "/workspace",
        exec_python: str | None = None,
        exec_run_root: Path | None = None,
        startup_timeout: float = STARTUP_TIMEOUT_S,
    ) -> None:
        self._config = config
        self._kiro = config.kiro
        self._log_dir = log_dir
        self._tool_policy = tool_policy
        self._kiro_home = kiro_home or Path.home() / ".kiro"
        self._command = list(command or KIRO_COMMAND)
        self._workspace = workspace
        self._startup_timeout = startup_timeout
        self.session = KiroSessionState(kiro_model_key(self._kiro.default_model), session_id)
        self._event_bus = SessionEventBus(SessionLog(self.log_path))
        exec_kwargs: dict[str, Any] = {}
        if exec_python:
            exec_kwargs["python"] = exec_python
        if exec_run_root:
            exec_kwargs["run_root"] = exec_run_root
        # Root-only, like native sessions: the worker's agent file leaves it out (091 D11).
        self._questions = QuestionBroker(self._emit_status)
        registry = create_registry()
        registry.register(kiro_question_tool(self._questions))
        self._mcp = ArchieMcpServer(registry, self._current_runner_context, exec_kwargs=exec_kwargs)
        self._acp: AcpClient | None = None
        self._kiro_session_id: str | None = None
        self.config_options: list = []
        self.mcp_status: dict | None = None
        self.tool_tags: list | None = None
        self.config_errors: list[dict] = []
        self._turn_active = False
        self._translator: TurnTranslator | None = None
        self._dead = False
        self._closing = False
        self._started = False
        self._changed = asyncio.Event()
        self._exit_code: int | None = None
        # Why the last Kiro process stopped, e.g. "Kiro backend stopped (exit code 1; ...)".
        self._exit_reason: str | None = None
        # The container's OOM-kill count and when it was read: when Kiro started, and at
        # each turn's start and end, so an exit is only blamed on recent kills (094 D16).
        self._oom_baseline: tuple[int | None, datetime] = (None, datetime.now(UTC))
        self._ledger = CreditLedger(self._kiro.credits)
        self._context_pct: float | None = None  # Kiro's latest root context usage (089)
        self._root_context = RootContext()  # live figure without placeholders (091 D8)
        self._compacting = False  # a manual ``_kiro/session/compact`` is in flight (091)
        self._compaction_summary: str | None = None
        self._tasks: set[asyncio.Task] = set()
        self._restoring = False  # a restart's ``_launch`` is replaying history (094)
        self._restart_task: asyncio.Task | None = None
        self._restarts: deque[float] = deque()  # monotonic times of recent restarts (D8)
        self._resume_note: str | None = None  # prefix for the next prompt after a cut-off turn
        self._dead_reason: str | None = None  # why the backend is dead, for the user
        self._interrupted_wait = False  # ESC dropped a message waiting for a restart (D17)

    # -- surface used by app.py ---------------------------------------------------

    @property
    def log_path(self) -> Path:
        return self._log_dir / f"{self.session.session_id}.jsonl"

    @property
    def event_bus(self) -> SessionEventBus:
        return self._event_bus

    @property
    def turn_active(self) -> bool:
        return self._turn_active

    @property
    def dead(self) -> bool:
        return self._dead

    @property
    def mcp_url(self) -> str:
        return self._mcp.url

    @property
    def pid(self) -> int | None:
        return self._acp.pid if self._acp else None

    def try_begin_turn(self) -> bool:
        if self._turn_active:
            return False
        self._turn_active = True
        return True

    # -- startup -------------------------------------------------------------------

    async def start(self) -> None:
        """Do the one-time setup, then launch Kiro and configure a new ACP session."""
        api_key = resolve_api_key()
        self._api_key = api_key
        if not api_key:
            raise KiroStartupError(
                "No Kiro API key: set kiro.api_key in credentials.yaml (archie auth login kiro)"
            )
        agent_files.merge_settings(self._kiro_home)
        start = period_start(datetime.now(UTC), self._kiro.credits.resets_on)
        self._ledger.start = start
        self._ledger.used = credits_used_since(self._log_dir, start)

        mcp_url = await self._mcp.start()
        skills = discover_skills(self._tool_policy.enabled)
        agent_files.write_agent_files(
            self._kiro_home,
            mcp_url=mcp_url,
            root_prompt=build_kiro_prompt(self._workspace),
            worker_prompt=build_kiro_worker_prompt(DEFAULT_AGENT.body, self._workspace),
            skill_paths=[entry.path for entry in skills.values()],
            worker_tools=[
                f"@{agent_files.MCP_SERVER}/{name}"
                for name in self._mcp.tool_names()
                if name != QUESTION_TOOL
            ],
        )
        await self._launch(
            load_session_id=None,
            model_id=self._kiro.default_model,
            timeout=self._startup_timeout,
        )
        self._started = True
        log.info(
            "Kiro session ready",
            extra={"kiro_session": self._kiro_session_id, "model": self.session.model_id},
        )

    async def _launch(self, *, load_session_id: str | None, model_id: str, timeout: float) -> None:
        """Spawn ``kiro-cli``, open or reload the ACP session, and apply the settings.

        ``load_session_id`` is ``None`` for a fresh ``session/new`` (startup) or a Kiro session
        id to reload with ``session/load`` (a restart, 094). One ``timeout`` covers every step.
        """
        deadline = time.monotonic() + timeout

        def remaining(step: str) -> float:
            left = deadline - time.monotonic()
            if left <= 0:
                raise KiroStartupError(f"Kiro startup timed out before {step}")
            return left

        env = dict(os.environ)
        env["KIRO_API_KEY"] = self._api_key
        self._acp = AcpClient(
            self._command,
            env=env,
            cwd=self._workspace if os.path.isdir(self._workspace) else None,
            frame_log=frame_log_path(home_dir(), self.session.session_id),
            on_notification=self._on_notification,
            on_exit=self._on_exit,
            redact=[self._api_key],
        )
        try:
            await self._acp.start()
        except OSError as exc:
            raise KiroStartupError(f"Cannot start {self._command[0]}: {exc}") from exc
        self._mark_oom_baseline()

        try:
            await self._acp.request(
                "initialize",
                {
                    "protocolVersion": PROTOCOL_VERSION,
                    "clientCapabilities": {
                        "fs": {"readTextFile": False, "writeTextFile": False},
                        "terminal": False,
                    },
                    "clientInfo": CLIENT_INFO,
                },
                timeout=remaining("initialize"),
            )
            meta = {"kiro": {"modeId": agent_files.ROOT_AGENT}}
            if load_session_id is None:
                result = await self._acp.request(
                    "session/new",
                    {"cwd": self._workspace, "mcpServers": [], "_meta": meta},
                    timeout=remaining("session/new"),
                )
                self._kiro_session_id = (result or {}).get("sessionId")
                if not self._kiro_session_id:
                    raise KiroStartupError("Kiro session/new returned no sessionId")
            else:
                result = await self._acp.request(
                    "session/load",
                    {
                        "sessionId": load_session_id,
                        "cwd": self._workspace,
                        "mcpServers": [],
                        "_meta": meta,
                    },
                    timeout=remaining("session/load"),
                )
                self._kiro_session_id = load_session_id
            self.config_options = (result or {}).get("configOptions") or self.config_options
            self._check_agent()
            mode = config_value(self.config_options, "mode")
            if mode != agent_files.ROOT_AGENT:
                raise KiroStartupError(
                    f"Kiro mode is {mode!r}, not {agent_files.ROOT_AGENT!r}: "
                    "the archie agent did not load"
                )
            await self._set_config("contentCollection", "disabled", remaining)
            await self._set_config("autopilot", "on", remaining)
            await self._wait_for_models(remaining)
            if model_id not in self.model_options():
                raise KiroStartupError(f"Kiro does not offer model {model_id!r}")
            await self._set_config("model", model_id, remaining)
            await self._apply_effort(model_id, remaining)
            await self._wait_for_tools(remaining)
        except TimeoutError as exc:
            raise KiroStartupError(f"Kiro startup timed out: {exc or 'no response'}") from exc
        except AcpError as exc:
            raise KiroStartupError(f"Kiro startup failed: {exc}") from exc

    async def _set_config(self, config_id: str, value: str, remaining) -> None:
        """Set one config option and verify Kiro reports the new value."""
        assert self._acp is not None
        result = await self._acp.request(
            "session/set_config_option",
            {"sessionId": self._kiro_session_id, "configId": config_id, "value": value},
            timeout=remaining(f"setting {config_id}"),
        )
        if isinstance(result, dict) and result.get("configOptions"):
            self.config_options = result["configOptions"]
        actual = config_value(self.config_options, config_id)
        if actual != value:
            raise KiroStartupError(f"Kiro {config_id} is {actual!r} after setting {value!r}")

    async def _wait_for_models(self, remaining) -> None:
        """Wait for the ``model`` option: Kiro advertises it after ``session/new``."""
        while not self.model_options():
            self._changed.clear()
            try:
                await asyncio.wait_for(self._changed.wait(), remaining("the model list"))
            except TimeoutError:
                raise KiroStartupError("Kiro did not advertise its models") from None

    def _agent_problem(self) -> str | None:
        """Reason the archie agent config is unusable, or None."""
        if self._started:
            # Startup set and verified it; Kiro must never turn content collection back on.
            collection = config_value(self.config_options, "contentCollection")
            if collection not in (None, "disabled"):
                return f"Kiro contentCollection changed to {collection!r}"
        if self.config_errors:
            err = self.config_errors[0]
            return f"Kiro agent config error in {err.get('path', '?')}: {err.get('error', err)}"
        if self.tool_tags is not None:
            bad = sorted(
                str(t.get("tag"))
                for t in self.tool_tags
                if isinstance(t, dict)
                and t.get("source") == "builtin"
                and t.get("tag") not in ALLOWED_BUILTIN_TAGS
            )
            if bad:
                return f"Kiro advertised built-in tools {', '.join(bad)}; only Archie tools are allowed"
        return None

    def _check_agent(self) -> None:
        problem = self._agent_problem()
        if problem:
            raise KiroStartupError(problem)

    async def _wait_for_tools(self, remaining) -> None:
        """Wait until the archie MCP server is connected and Archie tools are advertised."""
        while True:
            self._check_agent()
            status = (self.mcp_status or {}).get("status")
            tags = self.tool_tags or []
            has_archie = any(
                isinstance(t, dict) and str(t.get("tag", "")).startswith("@archie/") for t in tags
            )
            if status == "connected" and has_archie:
                return
            self._changed.clear()
            try:
                await asyncio.wait_for(self._changed.wait(), remaining("the archie MCP server"))
            except TimeoutError:
                raise KiroStartupError(
                    f"Kiro's archie MCP server is {status or 'not reported'}"
                    + ("" if has_archie else "; no Archie tools advertised")
                ) from None

    async def _apply_effort(self, model_id: str, remaining=None) -> None:
        """Set ``kiro.effort`` when the model offers it; unset means the model default."""
        effort = self._kiro.effort
        if effort is None:
            return
        levels: list = []
        for option in (config_option(self.config_options, "model") or {}).get("options") or ():
            if isinstance(option, dict) and option.get("value") == model_id:
                meta = (option.get("_meta") or {}).get("kiro") or {}
                levels = meta.get("effortLevels") or []
        if effort not in levels:
            log.warning(
                "Kiro model %s does not offer effort %r; using the model default", model_id, effort
            )
            return
        assert self._acp is not None
        result = await self._acp.request(
            "session/set_config_option",
            {"sessionId": self._kiro_session_id, "configId": "effortLevel", "value": effort},
            timeout=remaining("setting effortLevel") if remaining else 30.0,
        )
        if isinstance(result, dict) and result.get("configOptions"):
            self.config_options = result["configOptions"]
        actual = config_value(self.config_options, "effortLevel")
        if actual != effort:
            log.warning("Kiro effortLevel is %r after setting %r", actual, effort)

    async def switch_model(self, model_key: str) -> str | None:
        """Switch to an allowlisted ``kiro/<id>`` model; return error text or None."""
        model_id = kiro_model_id(model_key)
        if model_id is None or model_id not in self._kiro.models:
            return f"Unknown model: {model_key}"
        await self._await_restart()
        if self._dead or self._acp is None:
            return self._dead_message()
        if model_id not in self.model_options():
            return f"Unknown model: {model_key}"
        try:
            result = await self._acp.request(
                "session/set_config_option",
                {"sessionId": self._kiro_session_id, "configId": "model", "value": model_id},
                timeout=30.0,
            )
            if isinstance(result, dict) and result.get("configOptions"):
                self.config_options = result["configOptions"]
            await self._apply_effort(model_id)
        except (AcpError, TimeoutError) as exc:
            return f"Cannot switch Kiro model to {model_id}: {exc}"
        problem = self._agent_problem()
        if problem:
            self._fail(problem)
            return self._dead_message()
        self.session.model_id = kiro_model_key(model_id)
        return None

    # -- ACP callbacks ---------------------------------------------------------------

    async def _on_notification(self, msg: dict, received: float) -> None:
        method = msg.get("method")
        params = msg.get("params") or {}
        if method == "session/update":
            update = params.get("update") or {}
            if self._restoring:
                # A restart's session/load replays the history; keep only the config so
                # _wait_for_models and _wait_for_tools can finish (094).
                if update.get("sessionUpdate") == "config_option_update":
                    self.config_options = update.get("configOptions") or self.config_options
                    self._changed.set()
                return
            if self._compacting and self._translator is None:
                info = summarization(update)
                if info is not None and info.kind == "completed" and info.subtask is None:
                    self._compaction_summary = info.summary
            if update.get("sessionUpdate") == "config_option_update":
                self.config_options = update.get("configOptions") or self.config_options
                self._changed.set()
                if self._started and not self._dead:
                    problem = self._agent_problem()
                    if problem:
                        self._fail(problem)
            translator = self._translator
            if translator is not None:
                for event in translator.feed(update, received):
                    await self._event_bus.emit(event)
                    if (
                        isinstance(event, ToolResult)
                        and event.is_error
                        and translator.tool_name(event.tool_use_id) == QUESTION_TOOL
                    ):
                        # Kiro failed the call (or timed it out) while it still waits. A
                        # second question's own error result comes back ``completed``.
                        self._questions.cancel(QUESTION_ABANDONED)
            pct = self._root_context.feed(update)
            if pct is not None and pct != self._context_pct:
                self._context_pct = pct
                # A turn that has finished translating but not yet sent its closing status
                # (``_turn_active`` still set) leaves the new value to that status, so the
                # TUI never sees ``working`` after ``turn_complete`` (089 D7).
                closing = self._turn_active and self._translator is None
                if self._started and not self._dead and not closing:
                    # Awaited inline, like the translator's events, so it stays in order.
                    await self._emit_status()
        elif method == "_kiro/mcp/status":
            for server in params.get("servers") or ():
                if isinstance(server, dict) and server.get("name") == agent_files.MCP_SERVER:
                    self.mcp_status = server
        elif method == "_kiro/tools/didChange":
            self.tool_tags = params.get("tags") or []
        elif method == "_kiro/customAgent/config_error":
            self.config_errors.append(params)
            log.error("Kiro agent config error: %s", params.get("error") or params)
        else:
            return
        self._changed.set()
        if self._started and not self._dead:
            problem = self._agent_problem()
            if problem:
                self._fail(problem)

    def _fail(self, reason: str) -> None:
        """Fail closed after startup: mark the backend dead and kill Kiro (D17)."""
        log.error("Stopping the Kiro backend: %s", reason)
        self._dead = True
        self._dead_reason = reason
        if self._acp is not None:
            task = asyncio.get_running_loop().create_task(self._acp.close())
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    def _mark_oom_baseline(self) -> None:
        self._oom_baseline = (oom_kill_count(), datetime.now(UTC))

    def _reason(self, code: int | None, kills: int, since: datetime) -> str:
        detail = f"exit code {code}"
        if kills:
            detail += (
                f"; the out-of-memory killer killed {kills} process(es) in this container "
                f"since {since:%H:%M:%S} UTC"
            )
        return f"Kiro backend stopped ({detail})"

    def _can_restart(self) -> bool:
        cutoff = time.monotonic() - RESTART_WINDOW_S
        while self._restarts and self._restarts[0] < cutoff:
            self._restarts.popleft()
        return len(self._restarts) < RESTART_LIMIT

    def _on_exit(self, code: int | None) -> None:
        """Handle ``kiro-cli`` exiting: record it, then restart or mark the backend dead."""
        self._exit_code = code
        before, since = self._oom_baseline
        kills = kills_since(before)
        reason = self._reason(code, kills, since)
        if self._acp is not None:
            self._acp.record("exit", {"code": code, "oom_kills": kills})
        if self._closing:
            self._dead = True
            log.info("%s", reason)
            return
        if self._dead:
            return
        self._exit_reason = reason
        if self._restoring or not self._started:
            # Exit during startup or a restart: the start/_restart path reports it and
            # marks the backend dead; keep the reason for its turn_error.
            self._dead = True
            log.error("%s", reason)
            return
        # Unexpected exit after a working session: recover if we can (094).
        log.error("%s", reason)
        self._questions.cancel()
        self._mcp.cancel_all()
        if not self._can_restart():
            minutes = int(RESTART_WINDOW_S // 60)
            self._mark_dead(
                f"{reason}; stopped restarting after {RESTART_LIMIT} tries in {minutes} minutes"
            )
            return
        self._restarts.append(time.monotonic())
        self._resume_note = reason if (self._turn_active and not self._compacting) else None
        self._restart_task = asyncio.get_running_loop().create_task(self._restart(reason))
        self._tasks.add(self._restart_task)
        self._restart_task.add_done_callback(self._tasks.discard)

    def _mark_dead(self, reason: str) -> None:
        """Mark the backend dead and tell the user, from a sync context (094)."""
        self._dead = True
        self._dead_reason = reason
        self._spawn(self._notice("backend_stopped", f"{reason}. Start a new session."))
        self._spawn(self._emit_status())

    async def _restart(self, reason: str) -> None:
        """Relaunch ``kiro-cli`` and reload the Kiro session after an unexpected exit (094)."""
        old = self._acp
        if old is not None:
            with contextlib.suppress(Exception):
                await old.close()
        if self._closing:
            return
        self.config_options = []
        self.mcp_status = None
        self.tool_tags = None
        self.config_errors = []
        self._started = False
        self._restoring = True
        try:
            await self._launch(
                load_session_id=self._kiro_session_id,
                model_id=kiro_model_id(self.session.model_id) or self._kiro.default_model,
                timeout=RESTART_TIMEOUT_S,
            )
        except (KiroStartupError, AcpError, TimeoutError) as exc:
            self._restoring = False
            if self._acp is not None and self._acp.alive:
                with contextlib.suppress(Exception):
                    await self._acp.close()
            if self._closing:
                return
            self._dead = True
            self._dead_reason = f"{reason}; restart failed: {exc}"
            await self._notice("backend_stopped", f"{self._dead_reason}. Start a new session.")
            await self._emit_status()
            return
        self._restoring = False
        self._started = True
        if self._closing:
            return
        await self._notice(
            "backend_restarted", f"{reason}. Kiro was restarted and the conversation reloaded."
        )
        await self._emit_status()

    async def _await_restart(self) -> None:
        """Block until any in-flight restart finishes (shielded from the caller's cancel)."""
        task = self._restart_task
        if task is not None and not task.done():
            await asyncio.shield(task)

    # -- turns -------------------------------------------------------------------

    async def handle_message(self, content: str) -> None:
        """Run one turn on Kiro and guarantee a terminal event and status refresh."""
        self._turn_active = True
        try:
            await self._await_restart()
            if self._interrupted_wait:
                self._interrupted_wait = False
                await self._event_bus.emit(
                    ErrorNotice(
                        id=str(ULID()),
                        kind="turn_error",
                        message="Interrupted before Kiro finished restarting; the message "
                        "wasn't sent",
                    )
                )
                return
            if self._dead or self._acp is None:
                await self._event_bus.emit(
                    ErrorNotice(id=str(ULID()), kind="turn_error", message=self._dead_message())
                )
                return
            await self._run_turn(content)
        except LogAppendError as exc:
            await self._notice("storage_error", str(exc))
        finally:
            self._translator = None
            # Kiro doesn't cancel an MCP call when the turn ends (091 D10).
            self._questions.cancel()
            self._turn_active = False
            await self._emit_status()

    async def _run_turn(self, content: str) -> None:
        acp = self._acp
        assert acp is not None
        self._mark_oom_baseline()
        turn = self.session.next_turn_index()
        await self._event_bus.emit(
            UserMessage(id=str(ULID()), turn=turn, scope=None, content=content)
        )
        prompt_text = content
        if self._resume_note is not None:
            # Tell Kiro the previous turn was cut off; the logged user_message stays as typed.
            prompt_text = f"{self._note_text(self._resume_note)}\n\n{content}"
            self._resume_note = None
        translator = TurnTranslator(self.log_path, self.session.model_id, turn)
        self._translator = translator
        self._mcp.reset()
        sent_at = time.time()
        stop_reason: str | None = None
        error: str | None = None
        terminal_done = False
        try:
            result = await acp.request(
                "session/prompt",
                {
                    "sessionId": self._kiro_session_id,
                    "prompt": [{"type": "text", "text": prompt_text}],
                },
            )
            stop_reason = (result or {}).get("stopReason") or "end_turn"
        except AcpError as exc:
            # Kiro died: say why, with the OOM kills seen during the turn (094).
            error = self._exit_reason if not acp.alive and self._exit_reason else str(exc)
            if not acp.alive and self._restart_task is not None and not self._restart_task.done():
                error = f"{error}; restarting Kiro"
        except Exception as exc:  # noqa: BLE001 — every turn ends with a terminal event
            log.exception("Kiro turn failed")
            error = f"{type(exc).__name__}: {exc}"
        if acp.alive:
            self._mark_oom_baseline()
        try:
            self._translator = None
            now = datetime.now(UTC)
            events = translator.finish(
                stop_reason=stop_reason,
                error=error,
                sent_at=sent_at,
                at=time.time(),
                price=lambda credits: self._ledger.charge(credits, now),
            )
            for event in events:
                await self._event_bus.emit(event)
            terminal_done = True
        finally:
            if not terminal_done:
                with contextlib.suppress(Exception):
                    await self._event_bus.emit(
                        CanonicalTurnError(
                            id=str(ULID()),
                            turn=turn,
                            scope=None,
                            message=error or "Kiro turn ended without a terminal event",
                        )
                    )

    def interrupt(self, target: tuple[str, int] | None = None) -> None:
        """Cancel the whole Kiro turn and kill running Archie tool processes (D22)."""
        if not self._turn_active or self._acp is None or self._kiro_session_id is None:
            return
        if self._restart_task is not None and not self._restart_task.done():
            # A message is waiting for the restart; drop it without touching Kiro (D17).
            self._interrupted_wait = True
            return
        if self._compacting:
            # Kiro's compaction has no cancel; say so rather than ignore the key (091 D5).
            self._spawn(self._notice("compaction_not_cancellable", COMPACT_NOT_CANCELLABLE))
            return
        # Kiro doesn't cancel the MCP call, so end the wait here (091 D10).
        self._questions.cancel()
        self._mcp.cancel_all()
        self._spawn(self._send_cancel())

    def _spawn(self, coro) -> None:
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _send_cancel(self) -> None:
        assert self._acp is not None
        try:
            await self._acp.notify("session/cancel", {"sessionId": self._kiro_session_id})
        except AcpError as exc:
            log.warning("Cannot cancel the Kiro turn: %s", exc)

    async def run_manual_compaction(self) -> None:
        """Compact Kiro's context with ``_kiro/session/compact`` (091 D4).

        Runs with the turn flag taken by ``try_begin_turn`` and always releases it.
        """
        try:
            await self._await_restart()
            if self._dead or self._acp is None:
                await self._notice("manual_compaction_failed", self._dead_message())
                return
            if self.session.turn_index == 0:
                await self._notice("manual_compaction_failed", "Nothing to compact")
                return
            # Set before the first await, so an interrupt never sends session/cancel (D5).
            self._compacting = True
            self._compaction_summary = None
            await self._emit_status()
            await self._compact()
        except LogAppendError as exc:
            await self._notice("storage_error", str(exc))
        finally:
            self._compacting = False
            self._turn_active = False
            await self._emit_status()

    async def _compact(self) -> None:
        """Ask Kiro to compact and record it as a manual compaction (091 D2)."""
        assert self._acp is not None
        request_id = str(ULID())
        turn = self.session.turn_index
        await self._event_bus.emit(
            CompactionStart(
                id=str(ULID()),
                turn=turn,
                iteration=0,
                scope=None,
                request_id=request_id,
                reason="manual",
                tokens_before=0,  # Kiro reports no tokens
                estimated_next_request_cost_usd=None,
                model_key=self.session.model_id,
                sent_at=now_utc(),
            )
        )
        started = time.monotonic()

        def complete(status: str, error: str | None) -> CompactionComplete:
            return CompactionComplete(
                id=str(ULID()),
                turn=turn,
                iteration=0,
                scope=None,
                request_id=request_id,
                reason="manual",
                status=status,
                tokens_before=0,
                tokens_after=None,
                cost_saved_usd=0.0,
                duration_ms=round((time.monotonic() - started) * 1000),
                error=error,
                summary=self._compaction_summary if status == "success" else None,
            )

        error: str | None = None
        try:
            result = await self._acp.request(
                "_kiro/session/compact",
                {"sessionId": self._kiro_session_id},
                timeout=COMPACT_TIMEOUT_S,
            )
            if not (isinstance(result, dict) and result.get("success") is True):
                error = COMPACT_BUSY_MESSAGE
        except AcpError as exc:
            error = f"Kiro compaction failed: {exc}"
        except TimeoutError:
            error = f"Kiro compaction timed out after {COMPACT_TIMEOUT_S:.0f}s"
        except asyncio.CancelledError:
            # Shutdown cancelled the wait: every compaction_start still gets its completion.
            event = complete("interrupted", "The session stopped during the compaction")
            with contextlib.suppress(Exception):
                await asyncio.shield(self._event_bus.emit(event))
            raise
        await self._event_bus.emit(complete("failure" if error else "success", error))

    # -- helpers -------------------------------------------------------------------

    def _dead_message(self) -> str:
        return f"{self._dead_reason}; start a new session" if self._dead_reason else DEAD_MESSAGE

    def _note_text(self, reason: str) -> str:
        """The resume note prefixed to the first prompt after a cut-off turn (094)."""
        start, end = reason.find("("), reason.rfind(")")
        detail = reason[start + 1 : end] if 0 <= start < end else reason
        return (
            "[Archie note: the Kiro backend stopped during your previous turn "
            f"({detail}) and was restarted with this conversation reloaded. That turn was "
            "cut off: its running tool calls and subagents were stopped and their results "
            "lost. Check the current state before you continue that work.]"
        )

    async def _notice(self, kind: str, message: str, target: Any = None) -> None:
        try:
            await self._event_bus.emit(
                ErrorNotice(id=str(ULID()), kind=kind, message=message), target=target
            )
        except Exception:  # noqa: BLE001 — notices are best effort
            log.warning("Failed to send %s notice", kind, exc_info=True)

    @property
    def pending_question(self) -> PendingQuestion | None:
        """The question the root turn is waiting on, or ``None``."""
        return self._questions.pending

    def status_event(self) -> SessionStatus:
        """Build the live ``session_status`` frame (state, pending question, context %)."""
        from archie_agent.app import _read_git_branch

        question = self._questions.pending
        if question is not None:
            state = "blocked"
        elif self._turn_active:
            state = "working"
        else:
            state = "idle"
        return SessionStatus(
            id=str(ULID()),
            model_key=self.session.model_id,
            git_branch=_read_git_branch(),
            state=state,
            question=question,
            context_pct=msgspec.UNSET if self._context_pct is None else self._context_pct,
        )

    def answer_question(self, command) -> ErrorNotice | None:
        """Route an ``answer`` command; return a targeted notice when it doesn't fit."""
        try:
            self._questions.answer(
                command.question_id,
                Answer(option=command.option, text=command.text, dismiss=command.dismiss),
            )
        except QuestionError as exc:
            return ErrorNotice(id=str(ULID()), kind=exc.kind, message=str(exc))
        return None

    async def _emit_status(self) -> None:
        try:
            await self._event_bus.emit(self.status_event())
        except Exception:  # noqa: BLE001 — status refresh is best effort
            log.warning("Failed to broadcast session status", exc_info=True)

    async def _current_runner_context(self) -> dict:
        snapshot, notice = self._tool_policy.refresh()
        if notice is not None:
            with contextlib.suppress(Exception):
                await self._event_bus.emit(notice)
        return self._tool_policy.runner_context(snapshot)

    def redact(self, text: str) -> str:
        """``text`` without the API key (for startup-error text and logs)."""
        key = getattr(self, "_api_key", None)
        return text.replace(key, "[redacted]") if key else text

    async def close(self) -> None:
        """Kill Kiro's process group and stop the MCP server."""
        self._closing = True
        task = self._restart_task
        if task is not None and not task.done():
            # Cancel and await before closing _acp, so a restart can't spawn kiro-cli after.
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        if self._acp is not None:
            await self._acp.close()
        await self._mcp.stop()
        for task in list(self._tasks):
            task.cancel()

    def model_options(self) -> list[str]:
        option = config_option(self.config_options, "model") or {}
        return [o.get("value") for o in option.get("options") or () if isinstance(o, dict)]
