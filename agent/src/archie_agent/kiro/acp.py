"""Minimal asyncio JSON-RPC client for ``kiro-cli acp`` (newline-delimited JSON on stdio).

Follows the spike client in ``/tmp/kiro-spike/acp_client.py``: every frame is logged to
a JSONL file, agent→client requests are answered here, and notifications are handed to
a callback together with their receive time.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import signal
import time
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

PROTOCOL_VERSION = 1
CLIENT_INFO = {"name": "archie", "version": "1"}
_STDERR_IGNORE = ("ExperimentalWarning: SQLite is an experimental feature",)
_READ_LIMIT = 1 << 25

Notification = Callable[[dict, float], Awaitable[None] | None]


class AcpError(Exception):
    """A JSON-RPC error response, or the agent exiting with a request outstanding."""

    def __init__(self, message: str, *, code: int | None = None, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.data = data


class AcpClient:
    """Spawn the ACP agent and exchange JSON-RPC messages with it."""

    def __init__(
        self,
        command: Sequence[str],
        *,
        env: dict[str, str],
        cwd: str | None,
        frame_log: Path | None,
        on_notification: Notification,
        on_exit: Callable[[int | None], None] | None = None,
        redact: Sequence[str] = (),
    ) -> None:
        self._command = list(command)
        self._env = env
        self._cwd = cwd
        self._frame_log_path = frame_log
        self._frame_log = None
        self._on_notification = on_notification
        self._on_exit = on_exit
        self._redact = [r for r in redact if r]
        self._next_id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._proc: asyncio.subprocess.Process | None = None
        self._tasks: list[asyncio.Task] = []
        self.permission_requests: list[dict] = []

    @property
    def pid(self) -> int | None:
        return self._proc.pid if self._proc else None

    @property
    def alive(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    @property
    def returncode(self) -> int | None:
        return self._proc.returncode if self._proc else None

    async def start(self) -> None:
        if self._frame_log_path is not None:
            self._frame_log_path.parent.mkdir(parents=True, exist_ok=True)
            self._frame_log = self._frame_log_path.open("a", encoding="utf-8")
        self._proc = await asyncio.create_subprocess_exec(
            *self._command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self._env,
            cwd=self._cwd,
            start_new_session=True,
            limit=_READ_LIMIT,
        )
        self._tasks = [
            asyncio.create_task(self._read_stdout()),
            asyncio.create_task(self._read_stderr()),
        ]

    # -- frames ---------------------------------------------------------------

    def _log(self, direction: str, msg: Any) -> None:
        if self._frame_log is None:
            return
        line = json.dumps({"t": time.time(), "dir": direction, "msg": msg}, ensure_ascii=False)
        for secret in self._redact:
            line = line.replace(secret, "[redacted]")
        try:
            self._frame_log.write(line + "\n")
            self._frame_log.flush()
        except (OSError, ValueError):
            log.warning("Cannot write Kiro frame log", exc_info=True)

    async def _send(self, msg: dict) -> None:
        if not self.alive or self._proc.stdin is None:
            raise AcpError("Kiro backend is not running")
        self._log("out", msg)
        self._proc.stdin.write((json.dumps(msg) + "\n").encode())
        try:
            await self._proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as exc:
            raise AcpError(f"Kiro backend stopped: {exc}") from exc

    async def _read_stdout(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        try:
            while True:
                line = await self._proc.stdout.readline()
                if not line:
                    break
                received = time.time()
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    self._log("in-raw", line.decode(errors="replace"))
                    continue
                self._log("in", msg)
                if not isinstance(msg, dict):
                    continue
                try:
                    await self._dispatch(msg, received)
                except Exception:  # noqa: BLE001 — a bad frame must not stop the reader
                    log.exception("Failed to handle Kiro frame")
        finally:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._proc.wait(), 5)
            code = self._proc.returncode
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(AcpError(f"Kiro backend stopped (exit code {code})"))
            self._pending.clear()
            if self._on_exit is not None:
                self._on_exit(code)

    async def _dispatch(self, msg: dict, received: float) -> None:
        if "id" in msg and "method" not in msg:
            fut = self._pending.pop(msg["id"], None)
            if fut is None or fut.done():
                return
            if "error" in msg:
                err = msg["error"] or {}
                fut.set_exception(
                    AcpError(
                        str(err.get("message", err)), code=err.get("code"), data=err.get("data")
                    )
                )
            else:
                fut.set_result(msg.get("result"))
        elif "method" in msg and "id" in msg:
            await self._handle_request(msg)
        elif "method" in msg:
            result = self._on_notification(msg, received)
            if asyncio.iscoroutine(result):
                await result

    async def _handle_request(self, msg: dict) -> None:
        method = msg["method"]
        if method == "session/request_permission":
            params = msg.get("params") or {}
            self.permission_requests.append(params)
            tool = (params.get("toolCall") or {}).get("title") or "unknown tool"
            log.warning("Kiro asked permission for %s; answering cancelled", tool)
            await self._send(
                {"jsonrpc": "2.0", "id": msg["id"], "result": {"outcome": {"outcome": "cancelled"}}}
            )
            return
        await self._send(
            {
                "jsonrpc": "2.0",
                "id": msg["id"],
                "error": {"code": -32601, "message": f"Method not supported: {method}"},
            }
        )

    async def _read_stderr(self) -> None:
        assert self._proc is not None and self._proc.stderr is not None
        while True:
            line = await self._proc.stderr.readline()
            if not line:
                return
            text = line.decode(errors="replace").rstrip()
            if not text or any(marker in text for marker in _STDERR_IGNORE):
                continue
            for secret in self._redact:
                text = text.replace(secret, "[redacted]")
            # Kiro writes its routine progress as "[INFO] …" lines; keep those out of
            # the warning stream and log everything else at WARNING.
            if text.startswith(("[INFO]", "[DEBUG]")):
                log.debug("kiro-cli stderr: %s", text)
            else:
                log.warning("kiro-cli stderr: %s", text)

    # -- API ------------------------------------------------------------------

    async def request(self, method: str, params: dict, timeout: float | None = None) -> Any:
        self._next_id += 1
        rid = self._next_id
        fut = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        try:
            await self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        except AcpError:
            self._pending.pop(rid, None)
            raise
        if timeout is None:
            return await fut
        try:
            return await asyncio.wait_for(fut, timeout)
        except TimeoutError:
            self._pending.pop(rid, None)
            raise

    async def notify(self, method: str, params: dict) -> None:
        await self._send({"jsonrpc": "2.0", "method": method, "params": params})

    async def close(self, grace: float = 2.0) -> None:
        """Close stdin, then SIGTERM and SIGKILL Kiro's process group."""
        proc = self._proc
        if proc is not None:
            with contextlib.suppress(Exception):
                if proc.stdin is not None:
                    proc.stdin.close()
            if proc.returncode is None:
                _signal_group(proc.pid, signal.SIGTERM)
                try:
                    await asyncio.wait_for(proc.wait(), grace)
                except TimeoutError:
                    _signal_group(proc.pid, signal.SIGKILL)
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(proc.wait(), grace)
            else:
                # The leader is gone; descendants may still hold the group.
                _signal_group(proc.pid, signal.SIGKILL)
        for task in self._tasks:
            if not task.done():
                task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        if self._frame_log is not None:
            with contextlib.suppress(Exception):
                self._frame_log.close()
            self._frame_log = None


def _signal_group(pid: int, sig: int) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pid, sig)
