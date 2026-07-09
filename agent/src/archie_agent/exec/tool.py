"""Host-side exec tool handler.

Manages run directories, spawns the runner subprocess, reads the result envelope,
and formats the model-facing result string. Also builds the auto-generated `exec`
tool description from exec tool signatures.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

from archie_agent.exec.envelope import Envelope
from archie_agent.tools import ToolRegistry, ToolSpec

log = logging.getLogger(__name__)

_RUNS_ROOT = Path("/tmp/archie-runs")
_PYTHON = "/opt/archie/venv/bin/python"
_MAX_RESULT_CHARS = 16_000


async def run_exec(
    source: str,
    *,
    run_root: Path = _RUNS_ROOT,
    python: str = _PYTHON,
    on_start: Callable[[asyncio.subprocess.Process], Any] | None = None,
) -> Envelope:
    """Execute model-authored Python via the runner subprocess.

    Args:
        source: Python source code (must define async def main()).
        run_root: Base directory for run dirs (default /tmp/archie-runs).
        python: Path to Python interpreter.
        on_start: Optional callback invoked with the Process after spawn,
            used by the harness to capture the handle for cancellation.

    Returns:
        Result Envelope (structured dataclass).
    """
    from ulid import ULID

    run_dir = run_root / str(ULID())
    run_dir.mkdir(parents=True, exist_ok=True)

    # Write model source
    (run_dir / "main.py").write_text(source, encoding="utf-8")

    # Spawn the runner subprocess
    try:
        proc = await asyncio.create_subprocess_exec(
            python,
            "-m",
            "archie_agent.exec.runner",
            str(run_dir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as e:
        return Envelope.error_envelope("RunnerCrash", f"Failed to spawn runner: {e}")

    if on_start:
        on_start(proc)

    try:
        _, stderr_bytes = await proc.communicate()
    finally:
        # Ensure proc is waited on even if we're cancelled
        if proc.returncode is None:
            try:
                proc.kill()
                await proc.wait()
            except ProcessLookupError:
                pass

    # Read result envelope
    result_path = run_dir / "result.json"
    if not result_path.exists():
        stderr_text = stderr_bytes.decode("utf-8", errors="replace").strip() if stderr_bytes else ""
        return Envelope.error_envelope(
            "RunnerCrash",
            f"Runner exited (code={proc.returncode}) without result.json. stderr: {stderr_text}",
        )

    try:
        envelope = Envelope.read(run_dir)
    except (json.JSONDecodeError, OSError, TypeError) as e:
        return Envelope.error_envelope("RunnerCrash", f"Failed to read result.json: {e}")

    # Clean up ephemeral run directory
    try:
        shutil.rmtree(run_dir)
    except OSError:
        pass  # Best-effort cleanup

    return envelope


def format_result(envelope: Envelope) -> str:
    """Format a result envelope into the model-facing string.

    Truncates at _MAX_RESULT_CHARS with an elision marker.
    """
    parts: list[str] = []

    if envelope.ok:
        if envelope.return_value is not None:
            if envelope.return_repr:
                parts.append(f"return (repr): {envelope.return_value}")
            else:
                parts.append(
                    f"return: {json.dumps(envelope.return_value, ensure_ascii=False, default=str)}"
                )
    else:
        if envelope.error:
            parts.append(f"error: {envelope.error.type}: {envelope.error.message}")
            if envelope.error.traceback:
                parts.append(f"traceback:\n{envelope.error.traceback}")

    if envelope.stdout:
        parts.append(f"stdout:\n{envelope.stdout}")

    if envelope.stderr:
        parts.append(f"stderr:\n{envelope.stderr}")

    if envelope.duration_ms:
        parts.append(f"duration: {envelope.duration_ms}ms")

    result = "\n\n".join(parts)

    if len(result) > _MAX_RESULT_CHARS:
        result = result[:_MAX_RESULT_CHARS] + "\n[…truncated]"

    return result


def _generate_tool_docs() -> str:
    """Generate exec tool docs from introspection.

    Returns a concise signature list for inclusion in the exec tool description.
    """
    from archie_agent.exec.tools import get_all_tools

    tools = get_all_tools()
    if not tools:
        return ""

    lines: list[str] = []
    for name in sorted(tools):
        fn = tools[name]
        sig = inspect.signature(fn)
        doc = inspect.getdoc(fn) or ""
        first_line = doc.split("\n")[0].strip() if doc else ""
        lines.append(f"  {name}{sig} — {first_line}")

    return "\n".join(lines)


def _build_exec_description() -> str:
    """Build the full exec tool description with auto-generated docs."""
    header = (
        "Execute Python code inside the container. The source must define "
        "`async def main()` which will be awaited. The return value, stdout, "
        "stderr, and any exceptions are captured and returned.\n\n"
        "Available functions in the exec namespace:\n"
    )

    tool_docs = _generate_tool_docs()
    if tool_docs:
        header += tool_docs
    else:
        header += "  (none available)"

    header += (
        "\n\n"
        "Example:\n"
        "```python\n"
        "async def main():\n"
        '    results = await grep(pattern="TODO", include="*.py")\n'
        "    return results\n"
        "```"
    )

    return header


async def _exec_handler(source: str, **kwargs) -> str:
    """Handler for the exec ToolSpec — runs source and formats result."""
    envelope = await run_exec(source, **kwargs)
    return format_result(envelope)


def create_registry() -> ToolRegistry:
    """Create a ToolRegistry with the exec tool registered."""
    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            name="exec",
            description=_build_exec_description(),
            schema={
                "type": "object",
                "properties": {"source": {"type": "string", "description": "Python source code"}},
                "required": ["source"],
            },
            handler=_exec_handler,
        )
    )
    return registry
