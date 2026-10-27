"""Kiro backend harness: runs a session's turns through ``kiro-cli acp`` (077).

``KiroHarness`` has the surface ``app.py`` uses on ``AgentHarness``: ``session``,
``event_bus``, ``log_path``, ``turn_active``, ``try_begin_turn``, ``handle_message``,
``interrupt`` and ``run_manual_compaction``, plus ``start``, ``close`` and
``switch_model``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from archie_shared.config import home_dir
from archie_shared.events import ErrorNotice, SessionStatus, UserMessage
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
from archie_agent.exec.tool import create_registry
from archie_agent.kiro import agent_files
from archie_agent.kiro.acp import CLIENT_INFO, PROTOCOL_VERSION, AcpClient, AcpError
from archie_agent.kiro.mcp_server import ArchieMcpServer
from archie_agent.kiro.translate import TurnTranslator
from archie_agent.prompt import build_kiro_prompt, build_kiro_worker_prompt
from archie_agent.session_bus import SessionEventBus
from archie_agent.skills import discover_skills

if TYPE_CHECKING:
    from archie_shared.schemas import NexusConfig

    from archie_agent.tool_policy_live import LiveToolPolicy

log = logging.getLogger(__name__)

KIRO_COMMAND = ("kiro-cli", "acp", "--agent-engine=v3")
STARTUP_TIMEOUT_S = 30.0
DEAD_MESSAGE = "Kiro backend stopped; start a new session"


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
    """``credentials.yaml`` ``kiro.api_key``, falling back to ``KIRO_API_KEY``."""
    try:
        from archie_shared.credentials import get_credential

        credential = get_credential("kiro")
    except (KeyError, ValueError) as exc:
        log.warning("Cannot read the kiro credential: %s", exc)
        credential = None
    key = getattr(credential, "api_key", None) if credential is not None else None
    return key or os.environ.get("KIRO_API_KEY") or None


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
        self._mcp = ArchieMcpServer(
            create_registry(), self._current_runner_context, exec_kwargs=exec_kwargs
        )
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
        self._exit_code: int | None = None
        self._ledger = CreditLedger(self._kiro.credits)
        self._tasks: set[asyncio.Task] = set()

    # -- surface used by app.py ---------------------------------------------------

    @property
    def log_path(self) -> Path:
        return self._log_dir / f"{self.session.session_id}.jsonl"

    @property
    def event_bus(self) -> SessionEventBus:
        return self._event_bus

    @property
    def clients(self):
        return self._event_bus.clients

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
        """Start the MCP server and Kiro, and configure the ACP session (D18 deadline)."""
        deadline = time.monotonic() + self._startup_timeout

        def remaining(step: str) -> float:
            left = deadline - time.monotonic()
            if left <= 0:
                raise KiroStartupError(f"Kiro startup timed out before {step}")
            return left

        api_key = resolve_api_key()
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
        )

        env = dict(os.environ)
        env["KIRO_API_KEY"] = api_key
        self._acp = AcpClient(
            self._command,
            env=env,
            cwd=self._workspace if os.path.isdir(self._workspace) else None,
            frame_log=frame_log_path(home_dir(), self.session.session_id),
            on_notification=self._on_notification,
            on_exit=self._on_exit,
            redact=[api_key],
        )
        try:
            await self._acp.start()
        except OSError as exc:
            raise KiroStartupError(f"Cannot start {self._command[0]}: {exc}") from exc

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
            result = await self._acp.request(
                "session/new",
                {
                    "cwd": self._workspace,
                    "mcpServers": [],
                    "_meta": {"kiro": {"modeId": agent_files.ROOT_AGENT}},
                },
                timeout=remaining("session/new"),
            )
            self._kiro_session_id = (result or {}).get("sessionId")
            if not self._kiro_session_id:
                raise KiroStartupError("Kiro session/new returned no sessionId")
            self.config_options = (result or {}).get("configOptions") or self.config_options
            await self._set_config("contentCollection", "disabled", remaining)
            await self._set_config("autopilot", "on", remaining)
            await self._set_config("model", self._kiro.default_model, remaining)
            await self._apply_effort(self._kiro.default_model, remaining)
        except TimeoutError as exc:
            raise KiroStartupError("Kiro startup timed out") from exc
        except AcpError as exc:
            raise KiroStartupError(f"Kiro startup failed: {exc}") from exc
        log.info(
            "Kiro session ready",
            extra={"kiro_session": self._kiro_session_id, "model": self.session.model_id},
        )

    async def _set_config(self, config_id: str, value: str, remaining) -> None:
        assert self._acp is not None
        result = await self._acp.request(
            "session/set_config_option",
            {"sessionId": self._kiro_session_id, "configId": config_id, "value": value},
            timeout=remaining(f"setting {config_id}"),
        )
        if isinstance(result, dict) and result.get("configOptions"):
            self.config_options = result["configOptions"]

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

    async def switch_model(self, model_key: str) -> str | None:
        """Switch to an allowlisted ``kiro/<id>`` model; return error text or None."""
        model_id = kiro_model_id(model_key)
        if (
            model_id is None
            or model_id not in self._kiro.models
            or model_id not in self.model_options()
        ):
            return f"Unknown model: {model_key}"
        if self._dead or self._acp is None:
            return DEAD_MESSAGE
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
        self.session.model_id = kiro_model_key(model_id)
        return None

    # -- ACP callbacks ---------------------------------------------------------------

    async def _on_notification(self, msg: dict, received: float) -> None:
        method = msg.get("method")
        params = msg.get("params") or {}
        if method == "session/update":
            update = params.get("update") or {}
            if update.get("sessionUpdate") == "config_option_update":
                self.config_options = update.get("configOptions") or self.config_options
            translator = self._translator
            if translator is not None:
                for event in translator.feed(update, received):
                    await self._event_bus.emit(event)
        elif method == "_kiro/mcp/status":
            for server in params.get("servers") or ():
                if isinstance(server, dict) and server.get("name") == agent_files.MCP_SERVER:
                    self.mcp_status = server
        elif method == "_kiro/tools/didChange":
            self.tool_tags = params.get("tags") or []
        elif method == "_kiro/customAgent/config_error":
            self.config_errors.append(params)
            log.error("Kiro agent config error: %s", params.get("error") or params)

    def _on_exit(self, code: int | None) -> None:
        self._exit_code = code
        if not self._dead:
            self._dead = True
            if self._closing:
                log.info("Kiro backend stopped (exit code %s)", code)
            else:
                log.error("Kiro backend stopped (exit code %s)", code)

    # -- turns -------------------------------------------------------------------

    async def handle_message(self, content: str) -> None:
        """Run one turn on Kiro and guarantee a terminal event and status refresh."""
        self._turn_active = True
        try:
            if self._dead or self._acp is None:
                await self._event_bus.emit(
                    ErrorNotice(id=str(ULID()), kind="turn_error", message=DEAD_MESSAGE)
                )
                return
            await self._run_turn(content)
        except LogAppendError as exc:
            await self._notice("storage_error", str(exc))
        finally:
            self._translator = None
            self._turn_active = False
            await self._emit_status()

    async def _run_turn(self, content: str) -> None:
        assert self._acp is not None
        turn = self.session.next_turn_index()
        await self._event_bus.emit(
            UserMessage(id=str(ULID()), turn=turn, scope=None, content=content)
        )
        translator = TurnTranslator(self.log_path, self.session.model_id, turn)
        self._translator = translator
        self._mcp.reset()
        sent_at = time.time()
        stop_reason: str | None = None
        error: str | None = None
        terminal_done = False
        try:
            result = await self._acp.request(
                "session/prompt",
                {
                    "sessionId": self._kiro_session_id,
                    "prompt": [{"type": "text", "text": content}],
                },
            )
            stop_reason = (result or {}).get("stopReason") or "end_turn"
        except AcpError as exc:
            error = str(exc)
        except Exception as exc:  # noqa: BLE001 — every turn ends with a terminal event
            log.exception("Kiro turn failed")
            error = f"{type(exc).__name__}: {exc}"
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
        self._mcp.cancel_all()
        task = asyncio.get_running_loop().create_task(self._send_cancel())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _send_cancel(self) -> None:
        assert self._acp is not None
        try:
            await self._acp.notify("session/cancel", {"sessionId": self._kiro_session_id})
        except AcpError as exc:
            log.warning("Cannot cancel the Kiro turn: %s", exc)

    async def run_manual_compaction(self) -> None:
        """Kiro compacts automatically; ``/compact`` is not available (D21)."""
        try:
            await self._notice(
                "manual_compaction_failed",
                "Kiro compacts automatically; /compact is not available",
            )
        finally:
            self._turn_active = False

    # -- helpers -------------------------------------------------------------------

    async def _notice(self, kind: str, message: str, target: Any = None) -> None:
        try:
            await self._event_bus.emit(
                ErrorNotice(id=str(ULID()), kind=kind, message=message), target=target
            )
        except Exception:  # noqa: BLE001 — notices are best effort
            log.warning("Failed to send %s notice", kind, exc_info=True)

    async def _emit_status(self) -> None:
        from archie_agent.app import _read_git_branch

        try:
            await self._event_bus.emit(
                SessionStatus(
                    id=str(ULID()), model_key=self.session.model_id, git_branch=_read_git_branch()
                )
            )
        except Exception:  # noqa: BLE001 — status refresh is best effort
            log.warning("Failed to broadcast final turn status", exc_info=True)

    async def _current_runner_context(self) -> dict:
        snapshot, notice = self._tool_policy.refresh()
        if notice is not None:
            with contextlib.suppress(Exception):
                await self._event_bus.emit(notice)
        return self._tool_policy.runner_context(snapshot)

    async def close(self) -> None:
        """Kill Kiro's process group and stop the MCP server."""
        self._closing = True
        if self._acp is not None:
            await self._acp.close()
        await self._mcp.stop()
        for task in list(self._tasks):
            task.cancel()

    def model_options(self) -> list[str]:
        option = config_option(self.config_options, "model") or {}
        return [o.get("value") for o in option.get("options") or () if isinstance(o, dict)]

    def current_model(self) -> str | None:
        return kiro_model_id(self.session.model_id)
