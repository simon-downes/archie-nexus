"""Agent definition discovery and native subagent tool helpers.

Agent definitions are Markdown files under ``persona/agents``.  The execution
factory is added here as the task implementation grows; keeping discovery in
this module mirrors the structure of ``skills.py``.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import signal
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from archie_shared.canonical_events import ErrorNotice, encode_event
from archie_shared.canonical_events import TurnError as CanonicalTurnError
from archie_shared.config import persona_dir
from archie_shared.types import ToolResultBlock, ToolUseBlock
from ulid import ULID

from archie_agent.event_log import EventFactory, now_utc
from archie_agent.events import (
    IterationStart,
    TextDelta,
    ToolCall,
    ToolResult,
    TurnComplete,
    TurnError,
    TurnInterrupted,
)
from archie_agent.exec.tool import create_registry, format_result, run_exec
from archie_agent.exec.tools._subprocess import kill_process_group
from archie_agent.llm import create_llm_client
from archie_agent.loop import RequestContext, RequestFinished, run_loop
from archie_agent.prompt import build_subagent_prompt
from archie_agent.session import Session, Turn
from archie_agent.session_bus import LogAppendError
from archie_agent.skills import SkillEntry, create_skill_tool
from archie_agent.tools import ToolRegistry, ToolSpec

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class AgentEntry:
    """A discovered child-agent definition."""

    name: str
    description: str
    provider: str | None
    model: str | None
    skills: list[str]
    body: str
    path: Path


DEFAULT_AGENT = AgentEntry(
    name="default",
    description="General-purpose child agent for delegated tasks.",
    provider=None,
    model=None,
    skills=[],
    body=(
        "You are a general-purpose delegated agent. Complete the assigned task "
        "carefully, inspect the relevant files, and report concrete findings."
    ),
    path=Path("<built-in default agent>"),
)


def discover_agents() -> dict[str, AgentEntry]:
    """Discover agent definitions from the session persona directory."""
    catalog: dict[str, AgentEntry] = {}
    agents_dir = persona_dir() / "agents"
    if not agents_dir.is_dir():
        return catalog

    try:
        paths = sorted(agents_dir.glob("*.md"))
    except OSError as error:
        log.warning("Failed to read agent directory %s: %s", agents_dir, error)
        return catalog

    for path in paths:
        entry = _parse_agent_file(path)
        if entry is None:
            continue
        previous = catalog.get(entry.name)
        if previous is not None:
            log.warning(
                "Duplicate agent name %r: replacing %s with %s",
                entry.name,
                previous.path,
                path,
            )
        catalog[entry.name] = entry
    return catalog


def _parse_agent_file(path: Path) -> AgentEntry | None:
    """Parse one Markdown agent definition, warning and skipping failures."""
    try:
        content = path.read_text(encoding="utf-8")
    except OSError as error:
        log.warning("Failed to read agent file %s: %s", path, error)
        return None

    if not content.startswith("---"):
        log.warning("Agent file missing frontmatter delimiters: %s", path)
        return None

    parts = content.split("---", 2)
    if len(parts) < 3:
        log.warning("Agent file missing closing frontmatter delimiter: %s", path)
        return None

    try:
        frontmatter = yaml.safe_load(parts[1])
    except yaml.YAMLError as error:
        log.warning("YAML parse error in agent file %s: %s", path, error)
        return None

    if not isinstance(frontmatter, dict):
        log.warning("Frontmatter is not a mapping in agent file: %s", path)
        return None

    name = frontmatter.get("name")
    description = frontmatter.get("description")
    if not name or not description:
        log.warning("Agent file missing required frontmatter (name/description): %s", path)
        return None

    skills = frontmatter.get("skills", [])
    if skills is None:
        skills = []
    if not isinstance(skills, list) or any(not isinstance(skill, str) for skill in skills):
        log.warning("Agent file has invalid skills list: %s", path)
        return None

    return AgentEntry(
        name=str(name),
        description=str(description),
        provider=_optional_string(frontmatter.get("provider"), path, "provider"),
        model=_optional_string(frontmatter.get("model"), path, "model"),
        skills=list(skills),
        body=parts[2].strip(),
        path=path,
    )


def _optional_string(value: object, path: Path, field: str) -> str | None:
    """Normalize an optional string field, rejecting non-string values."""
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        log.warning("Agent file has invalid %s field: %s", field, path)
        return None
    return value


class ChildDispatch:
    """Tool executor with process and pending state isolated to one child."""

    def __init__(
        self,
        registry: ToolRegistry,
        *,
        exec_python: str | None = None,
        exec_run_root: Path | None = None,
    ) -> None:
        self.registry = registry
        self.exec_python = exec_python
        self.exec_run_root = exec_run_root
        self.active_proc: asyncio.subprocess.Process | None = None
        self.pending_tools: dict[str, object] = {}

    async def execute(self, block: ToolUseBlock) -> ToolResultBlock:
        spec = self.registry.get(block.name)
        if spec is None:
            return ToolResultBlock(
                tool_use_id=block.tool_use_id,
                content=f"Unknown tool: {block.name}",
                is_error=True,
            )

        started = time.perf_counter()
        try:
            if block.name == "exec":
                kwargs: dict[str, Any] = {"on_start": self.on_process_start}
                if self.exec_python is not None:
                    kwargs["python"] = self.exec_python
                if self.exec_run_root is not None:
                    kwargs["run_root"] = self.exec_run_root
                envelope = await run_exec(str(block.input.get("source", "")), **kwargs)
                content = format_result(envelope)
                is_error = not envelope.ok
            else:
                kwargs = dict(block.input)
                if "on_start" in inspect.signature(spec.handler).parameters:
                    kwargs["on_start"] = self.on_process_start
                content = str(await spec.handler(**kwargs))
                is_error = False
        except Exception as error:  # noqa: BLE001 - normalize tool failures
            content = f"{type(error).__name__}: {error}"
            is_error = True
        finally:
            self.active_proc = None

        duration_ms = round((time.perf_counter() - started) * 1000)
        return ToolResultBlock(
            tool_use_id=block.tool_use_id,
            content=content,
            is_error=is_error,
            duration_ms=duration_ms,
            result_lines=len(content.splitlines()),
            result_bytes=len(content.encode("utf-8")),
        )

    def on_process_start(self, proc: asyncio.subprocess.Process) -> None:
        self.active_proc = proc

    def cancel_process(self) -> None:
        proc = self.active_proc
        if proc is None or proc.returncode is not None:
            return
        kill_process_group(proc, signal.SIGTERM)
        try:
            loop = asyncio.get_running_loop()
            loop.call_later(2.0, self._kill_process, proc)
        except RuntimeError:
            pass

    @staticmethod
    def _kill_process(proc: asyncio.subprocess.Process) -> None:
        if proc.returncode is None:
            kill_process_group(proc, signal.SIGKILL)


def _scoped_skills(
    catalog: dict[str, SkillEntry],
    names: list[str],
    agent_name: str,
) -> dict[str, SkillEntry]:
    """Filter skills for a child and warn for unknown declarations."""
    result: dict[str, SkillEntry] = {}
    for name in names:
        entry = catalog.get(name)
        if entry is None:
            log.warning("Unknown skill %r declared by agent %r", name, agent_name)
            continue
        result[name] = entry
    return result


def _resolve_model(
    entry: AgentEntry, catalog: dict[str, Any], active_model: Any
) -> tuple[str, Any]:
    """Resolve an agent override as a catalog key, falling back to the active model."""
    if entry.model is None:
        return "", active_model
    model = catalog.get(entry.model)
    if model is None:
        raise KeyError(f"unknown model '{entry.model}'")
    return entry.model, model


def create_task_tool(
    *,
    agent_catalog: dict[str, AgentEntry],
    skill_catalog: dict[str, SkillEntry],
    session: Session,
    model_catalog: dict[str, Any],
    active_model: Any | Callable[[], Any],
    active_model_key: str | Callable[[], str],
    region: str,
    log_path: Path,
    broadcast: Any,
    exec_python: str | None = None,
    exec_run_root: Path | None = None,
    max_concurrent: int = 3,
    live_children: dict[tuple[str, int], tuple[Any, asyncio.Event, Callable[[], None]]]
    | None = None,
) -> ToolSpec:
    """Create the root task tool; launch context is injected internally."""
    if max_concurrent <= 0:
        raise ValueError("max_concurrent must be positive")

    async def run_child(
        task: dict[str, Any],
        *,
        index: int,
        launch_scope: str,
        parent_turn: int,
        semaphore: asyncio.Semaphore,
    ) -> str:
        async with semaphore:
            terminal_emitted = False
            agent_name = task.get("agent")
            prompt = task.get("prompt")
            if not isinstance(agent_name, str) or not isinstance(prompt, str) or not prompt.strip():
                return f"[{index}] Error: task requires non-empty string agent and prompt"
            entry = agent_catalog.get(agent_name)
            warning = ""
            if entry is None:
                available = ", ".join(sorted(agent_catalog)) or "(none)"
                warning = (
                    f"Warning: unknown agent '{agent_name}' (available: {available}); "
                    "using default agent. "
                )
                log.warning("%s", warning.rstrip())
                entry = DEFAULT_AGENT

            async def _broadcast_child_error(kind: str, message: str) -> None:
                try:
                    await broadcast_raw(
                        broadcast,
                        encode_event(ErrorNotice(id=str(ULID()), kind=kind, message=message)),
                    )
                except Exception:  # noqa: BLE001 — best effort only
                    log.warning("Failed to broadcast child %s", kind, exc_info=True)

            factory: EventFactory | None = None
            storage_failed = False
            fallback_attempted = False

            async def _publish_child_terminal(serialized: str) -> None:
                nonlocal terminal_emitted
                terminal_emitted = True
                try:
                    await asyncio.shield(broadcast_raw(broadcast, serialized))
                except asyncio.CancelledError:
                    raise
                except Exception:
                    terminal_emitted = False
                    raise

            async def _publish_child_fallback(message: str) -> None:
                nonlocal terminal_emitted, storage_failed, fallback_attempted
                if terminal_emitted or storage_failed or fallback_attempted:
                    return
                fallback_attempted = True
                try:
                    if factory is None:
                        serialized = encode_event(
                            CanonicalTurnError(
                                id=str(ULID()),
                                turn=parent_turn,
                                scope=launch_scope,
                                message=message,
                                subagent_index=index,
                            )
                        )
                    else:
                        try:
                            _, serialized = factory.turn_error(turn=parent_turn, message=message)
                        except Exception:
                            serialized = encode_event(
                                CanonicalTurnError(
                                    id=str(ULID()),
                                    turn=parent_turn,
                                    scope=launch_scope,
                                    message=message,
                                    subagent_index=index,
                                )
                            )
                    await _publish_child_terminal(serialized)
                except LogAppendError as error:
                    storage_failed = True
                    await _broadcast_child_error("storage_error", str(error))
                except Exception:
                    log.warning("Failed to publish child terminal event", exc_info=True)

            try:
                current_model = active_model() if callable(active_model) else active_model
                current_model_key = (
                    active_model_key() if callable(active_model_key) else active_model_key
                )
                model_key, child_model = _resolve_model(entry, model_catalog, current_model)
                if not model_key:
                    model_key = current_model_key
                child_llm = create_llm_client(child_model, region)
                child_skills = _scoped_skills(skill_catalog, entry.skills, entry.name)
                loaded_skills: list[tuple[str, str]] = []
                child_prompt = build_subagent_prompt(
                    child_model.name,
                    entry.body,
                    catalog=child_skills or None,
                    loaded_skills=loaded_skills,
                    agents_context="",
                )
                child_registry = create_registry()
                child_registry.register(create_skill_tool(child_skills, loaded_skills))
                # The child registry deliberately has no task registration.
                dispatch = ChildDispatch(
                    child_registry,
                    exec_python=exec_python,
                    exec_run_root=exec_run_root,
                )
                import threading

                interrupt = threading.Event()
                interrupt_async = asyncio.Event()
                child_key = (launch_scope, index)
                if live_children is not None:
                    live_children[child_key] = (interrupt, interrupt_async, dispatch.cancel_process)
                factory = EventFactory(
                    log_path,
                    model_key,
                    child_model,
                    scope=launch_scope,
                    subagent_index=index,
                )
                from archie_shared.types import TextBlock

                text_parts: list[str] = []
                iter_text = ""
                assistant_event_logged = False
                request_ids: list[str] = []
                current_iteration = 0
                current_request_id = ""

                async for event in run_loop(
                    messages=[
                        Turn(role="user", content=[TextBlock(text=prompt)], turn_index=parent_turn)
                    ],
                    system=child_prompt,
                    llm=child_llm,
                    interrupt=interrupt,
                    interrupt_async=interrupt_async,
                    tool_config=child_registry.to_tool_config(),
                    execute_tool=dispatch.execute,
                    request_context_factory=lambda: RequestContext(
                        request_id=str(ULID()), sent_at=now_utc()
                    ),
                ):
                    if isinstance(event, IterationStart):
                        iter_text = ""
                        assistant_event_logged = False
                        current_iteration = event.index
                        _, serialized = factory.iteration_start(
                            turn=parent_turn,
                            iteration=current_iteration,
                        )
                        await broadcast_raw(broadcast, serialized)
                    elif isinstance(event, TextDelta):
                        text_parts.append(event.text)
                        iter_text += event.text
                        delta, serialized = factory.text_delta(
                            turn=parent_turn,
                            iteration=current_iteration,
                            request_id=current_request_id,
                            text=event.text,
                        )
                        await broadcast_raw(broadcast, serialized)
                    elif isinstance(event, RequestFinished):
                        request, serialized = factory.request(
                            turn=parent_turn,
                            iteration=current_iteration,
                            sent_at=event.context.sent_at,
                            duration_ms=event.duration_ms,
                            status=event.status,
                            usage=event.usage,
                            stop_reason=event.stop_reason,
                            error=event.error,
                            request_id=event.context.request_id,
                        )
                        request_ids.append(request.id)
                        current_request_id = request.id
                        await broadcast_raw(broadcast, serialized)
                    elif isinstance(event, ToolCall):
                        if iter_text and not assistant_event_logged:
                            _, serialized = factory.assistant_message(
                                turn=parent_turn,
                                request_ids=request_ids.copy(),
                                content=iter_text,
                                interrupted=False,
                            )
                            await broadcast_raw(broadcast, serialized)
                            assistant_event_logged = True
                        _, serialized = factory.tool_call(
                            turn=parent_turn,
                            iteration=current_iteration,
                            request_id=current_request_id,
                            tool_use_id=event.tool_use_id,
                            name=event.name,
                            input=event.input,
                        )
                        await broadcast_raw(broadcast, serialized)
                    elif isinstance(event, ToolResult):
                        _, serialized = factory.tool_result(
                            turn=parent_turn,
                            iteration=current_iteration,
                            request_id=current_request_id,
                            tool_use_id=event.tool_use_id,
                            content=event.content,
                            is_error=event.is_error,
                            duration_ms=event.duration_ms,
                            result_bytes=event.result_bytes,
                            result_lines=event.result_lines,
                        )
                        await broadcast_raw(broadcast, serialized)
                    elif isinstance(event, TurnComplete):
                        if iter_text and not assistant_event_logged:
                            _, serialized = factory.assistant_message(
                                turn=parent_turn,
                                request_ids=request_ids.copy(),
                                content=iter_text,
                                interrupted=False,
                            )
                            await broadcast_raw(broadcast, serialized)
                        _, serialized = factory.turn_complete(
                            turn=parent_turn, stop_reason=event.stop_reason
                        )
                        await _publish_child_terminal(serialized)
                    elif isinstance(event, TurnError):
                        _, serialized = factory.turn_error(turn=parent_turn, message=event.error)
                        await _publish_child_terminal(serialized)
                        return f"[{index}] {agent_name}: {warning}Error: {event.error}"
                    elif isinstance(event, TurnInterrupted):
                        _, serialized = factory.turn_interrupted(turn=parent_turn)
                        await _publish_child_terminal(serialized)
                        return f"[{index}] {agent_name}: {warning}interrupted"
                result = "".join(text_parts)
                return f"[{index}] {agent_name}: {warning}{result}"
            except LogAppendError as error:
                storage_failed = True
                await _broadcast_child_error("storage_error", str(error))
                return f"[{index}] {agent_name}: {warning}{type(error).__name__}: {error}"
            except Exception as error:  # noqa: BLE001 - per-child isolation
                log.exception("Child agent %s failed", agent_name)
                await _publish_child_fallback(str(error))
                return f"[{index}] {agent_name}: {warning}{type(error).__name__}: {error}"
            finally:
                if not terminal_emitted and not storage_failed and not fallback_attempted:
                    await _publish_child_fallback("Child ended without a terminal event")
                if live_children is not None:
                    live_children.pop((launch_scope, index), None)

    async def handler(
        tasks=None, _launch_scope: str | None = None, _parent_turn: int | None = None, **kwargs
    ) -> str:
        if not isinstance(tasks, list) or not tasks:
            return "Error: task requires one or more tasks"
        if not _launch_scope:
            return "Error: task launch context is missing"
        semaphore = asyncio.Semaphore(max_concurrent)
        results = await asyncio.gather(
            *(
                run_child(
                    item,
                    index=index,
                    launch_scope=_launch_scope,
                    parent_turn=_parent_turn or session.turn_index,
                    semaphore=semaphore,
                )
                for index, item in enumerate(tasks)
            ),
        )
        return "\n".join(results)

    return ToolSpec(
        name="task",
        description="Delegate one or more tasks to specialised child agents.",
        schema={
            "type": "object",
            "properties": {
                "tasks": {
                    "type": "array",
                    "minItems": 1,
                    "items": {
                        "type": "object",
                        "properties": {
                            "agent": {"type": "string"},
                            "prompt": {"type": "string"},
                        },
                        "required": ["agent", "prompt"],
                    },
                }
            },
            "required": ["tasks"],
        },
        handler=handler,
    )


async def broadcast_raw(broadcast: Any, serialized: str) -> None:
    """Broadcast canonical serialized data through the supplied callback."""
    await broadcast(serialized)
