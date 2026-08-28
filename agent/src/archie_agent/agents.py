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
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
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

        return ToolResultBlock(
            tool_use_id=block.tool_use_id,
            content=content,
            is_error=is_error,
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


def _resolve_model(entry: AgentEntry, catalog: dict[str, Any], active_model: Any) -> tuple[str, Any]:
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
    live_children: dict[tuple[str, int], tuple[Any, asyncio.Event, Callable[[], None]]] | None = None,
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

            try:
                current_model = active_model() if callable(active_model) else active_model
                current_model_key = active_model_key() if callable(active_model_key) else active_model_key
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
                    messages=[Turn(role="user", content=[TextBlock(text=prompt)], turn_index=parent_turn)],
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
                            turn_iteration=f"{parent_turn}.{current_iteration}",
                            index=current_iteration,
                        )
                        await broadcast_raw(broadcast, serialized)
                    elif isinstance(event, TextDelta):
                        text_parts.append(event.text)
                        iter_text += event.text
                        delta, serialized = factory.text_delta(
                            turn_iteration=f"{parent_turn}.{current_iteration}",
                            request_id=current_request_id,
                            text=event.text,
                        )
                        await broadcast_raw(broadcast, serialized)
                    elif isinstance(event, RequestFinished):
                        request, serialized = factory.request(
                            turn_iteration=f"{parent_turn}.{current_iteration}",
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
                                turn_iteration=f"{parent_turn}.{current_iteration}",
                                request_ids=request_ids.copy(),
                                content=iter_text,
                                interrupted=False,
                            )
                            await broadcast_raw(broadcast, serialized)
                            assistant_event_logged = True
                        _, serialized = factory.tool_call(
                            turn_iteration=f"{parent_turn}.{current_iteration}",
                            request_id=current_request_id,
                            tool_use_id=event.tool_use_id,
                            name=event.name,
                            input=event.input,
                        )
                        await broadcast_raw(broadcast, serialized)
                    elif isinstance(event, ToolResult):
                        _, serialized = factory.tool_result(
                            turn_iteration=f"{parent_turn}.{current_iteration}",
                            request_id=current_request_id,
                            tool_use_id=event.tool_use_id,
                            content=event.content,
                            is_error=event.is_error,
                            duration_ms=0,
                            result_bytes=len(event.content.encode()),
                        )
                        await broadcast_raw(broadcast, serialized)
                    elif isinstance(event, TurnComplete):
                        if iter_text and not assistant_event_logged:
                            _, serialized = factory.assistant_message(
                                turn=parent_turn,
                                turn_iteration=f"{parent_turn}.{current_iteration}",
                                request_ids=request_ids.copy(),
                                content=iter_text,
                                interrupted=False,
                            )
                            await broadcast_raw(broadcast, serialized)
                        _, serialized = factory.turn_complete(
                            turn=parent_turn, stop_reason=event.stop_reason
                        )
                        await broadcast_raw(broadcast, serialized)
                    elif isinstance(event, TurnError):
                        _, serialized = factory.turn_error(turn=parent_turn, message=event.error)
                        await broadcast_raw(broadcast, serialized)
                        return f"[{index}] {agent_name}: {warning}Error: {event.error}"
                    elif isinstance(event, TurnInterrupted):
                        _, serialized = factory.turn_interrupted(turn=parent_turn)
                        await broadcast_raw(broadcast, serialized)
                        return f"[{index}] {agent_name}: {warning}interrupted"
                result = "".join(text_parts)
                return f"[{index}] {agent_name}: {warning}{result}"
            except Exception as error:  # noqa: BLE001 - per-child isolation
                log.exception("Child agent %s failed", agent_name)
                return f"[{index}] {agent_name}: {warning}{type(error).__name__}: {error}"
            finally:
                if live_children is not None:
                    live_children.pop((launch_scope, index), None)

    async def handler(tasks=None, _launch_scope: str | None = None, _parent_turn: int | None = None, **kwargs) -> str:
        if not isinstance(tasks, list) or not tasks:
            return "Error: task requires one or more tasks"
        if not _launch_scope:
            return "Error: task launch context is missing"
        semaphore = asyncio.Semaphore(max_concurrent)
        results = await asyncio.gather(
            *(run_child(item, index=index, launch_scope=_launch_scope, parent_turn=_parent_turn or session.turn_index, semaphore=semaphore) for index, item in enumerate(tasks)),
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
