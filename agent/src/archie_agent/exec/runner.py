"""Runner — trusted execution boundary for model-authored code.

This module runs as a subprocess inside the container. It is NOT model-authored.
Per invocation it:
1. Validates the contract (source defines `async def main()`)
2. Builds the exec tool namespace
3. Imports and awaits main()
4. Captures return value, stdout/stderr, exceptions, timing
5. Writes a structured result envelope to <run-dir>/result.json
6. On SIGTERM, flushes a partial envelope with captured-so-far output

Usage: python -m archie_agent.exec.runner <run-dir>
  <run-dir> must contain main.py (model source)
  Result written to <run-dir>/result.json
"""

import ast
import asyncio
import functools
import inspect
import io
import json
import signal
import sys
import time
import traceback
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from archie_agent.exec.envelope import CallRecord, Envelope, ErrorInfo

# Hard cap on individual fields to prevent OOM/pipe exhaustion.
_MAX_FIELD_BYTES = 10 * 1024 * 1024  # 10MB


def _cap(value: str) -> str:
    """Cap a string to the hard maximum."""
    if len(value) > _MAX_FIELD_BYTES:
        return value[:_MAX_FIELD_BYTES] + f"\n[...capped at {_MAX_FIELD_BYTES} bytes]"
    return value


def _validate_contract(source: str) -> str | None:
    """Validate that source defines `async def main()`.

    Returns None on success, or an error message string on failure.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        return f"SyntaxError: {e.msg} (line {e.lineno})"

    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "main":
            args = node.args
            required_count = len(args.args) - len(args.defaults)
            if required_count > 0:
                return "ContractError: main() must not require arguments"
            return None

    return "ContractError: source must define `async def main()`"


def _build_namespace(calls: list[CallRecord]) -> dict:
    """Build the namespace injected into model code.

    Imports exec tool functions and wraps each for audit logging.
    """
    namespace = {
        "asyncio": asyncio,
        "__builtins__": __builtins__,
    }

    try:
        from archie_agent.exec.tools import get_all_tools

        for name, func in get_all_tools().items():
            namespace[name] = _wrap_for_audit(func, name, calls)
    except ImportError:
        pass  # Should not happen in normal operation

    return namespace


def _wrap_for_audit(fn, name: str, calls: list[CallRecord]):
    """Wrap an exec tool function to record audit log entries."""

    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        args_summary = _summarise_args(args, kwargs)
        t0 = time.time()
        ok = True
        try:
            return await fn(*args, **kwargs)
        except Exception:
            ok = False
            raise
        finally:
            duration_ms = int((time.time() - t0) * 1000)
            calls.append(CallRecord(fn=name, args=args_summary, ms=duration_ms, ok=ok))

    return wrapper


def _summarise_args(args: tuple, kwargs: dict) -> str:
    """Build a short summary of function arguments for the audit log."""
    parts: list[str] = []
    for arg in args:
        parts.append(_truncate_value(arg))
    for key, val in kwargs.items():
        parts.append(f"{key}={_truncate_value(val)}")
    return ", ".join(parts) if parts else ""


def _truncate_value(val, max_len: int = 80) -> str:
    """Truncate a value representation for audit logging."""
    s = repr(val)
    if len(s) > max_len:
        return s[: max_len - 3] + "..."
    return s


def run(run_dir: Path) -> None:
    """Execute the model's source and write the result envelope."""
    source_path = run_dir / "main.py"

    if not source_path.exists():
        Envelope.error_envelope("ContractError", f"Source file not found: {source_path}").write(
            run_dir
        )
        return

    source = source_path.read_text(encoding="utf-8")

    # Validate contract
    error = _validate_contract(source)
    if error:
        error_type = "SyntaxError" if error.startswith("SyntaxError") else "ContractError"
        Envelope.error_envelope(error_type, error).write(run_dir)
        return

    # Build namespace and compile
    calls: list[CallRecord] = []
    namespace = _build_namespace(calls)

    try:
        code = compile(source, str(source_path), "exec")
    except SyntaxError as e:
        Envelope.error_envelope(
            "SyntaxError", f"{e.msg} (line {e.lineno})", tb=traceback.format_exc()
        ).write(run_dir)
        return

    # Execute the module (defines main and any helpers)
    try:
        exec(code, namespace)  # noqa: S102
    except Exception as e:
        Envelope.error_envelope(type(e).__name__, str(e), tb=traceback.format_exc()).write(run_dir)
        return

    # Verify main() exists and is async
    main_fn = namespace.get("main")
    if main_fn is None:
        Envelope.error_envelope("ContractError", "source must define `async def main()`").write(
            run_dir
        )
        return

    if not inspect.iscoroutinefunction(main_fn):
        Envelope.error_envelope(
            "ContractError", "main() must be an async function (async def main)"
        ).write(run_dir)
        return

    # Execute main() with stdout/stderr capture
    stdout_buf = io.StringIO()
    stderr_buf = io.StringIO()
    return_value = None
    return_repr = False
    error_info = None
    t0 = time.time()

    # Update partial state so SIGTERM handler can flush captured-so-far output
    _partial_state["stdout_buf"] = stdout_buf
    _partial_state["stderr_buf"] = stderr_buf
    _partial_state["t0"] = t0

    try:
        with redirect_stdout(stdout_buf), redirect_stderr(stderr_buf):
            return_value = asyncio.run(main_fn())
    except Exception as e:
        error_info = ErrorInfo(
            type=type(e).__name__,
            message=str(e),
            traceback=traceback.format_exc(),
        )

    duration_ms = int((time.time() - t0) * 1000)

    # Serialise return value
    if return_value is not None and error_info is None:
        try:
            json.dumps(return_value)
        except (TypeError, ValueError):
            return_value = repr(return_value)
            return_repr = True

    # Build and write envelope
    envelope = Envelope(
        ok=error_info is None,
        return_value=return_value,
        return_repr=return_repr,
        stdout=_cap(stdout_buf.getvalue()),
        stderr=_cap(stderr_buf.getvalue()),
        error=error_info,
        calls=calls,
        duration_ms=duration_ms,
    )
    envelope.write(run_dir)


# --- SIGTERM handling for cancellation ---

_partial_state: dict = {
    "run_dir": None,
    "stdout_buf": None,
    "stderr_buf": None,
    "t0": None,
}


def _sigterm_handler(signum, frame):
    """Flush a partial envelope on SIGTERM (cancellation)."""
    run_dir = _partial_state.get("run_dir")
    if run_dir is None:
        sys.exit(1)

    stdout = ""
    stderr = ""
    if _partial_state.get("stdout_buf"):
        stdout = _partial_state["stdout_buf"].getvalue()
    if _partial_state.get("stderr_buf"):
        stderr = _partial_state["stderr_buf"].getvalue()

    duration_ms = 0
    if _partial_state.get("t0"):
        duration_ms = int((time.time() - _partial_state["t0"]) * 1000)

    Envelope.error_envelope(
        "Cancelled",
        "Execution cancelled by user",
        stdout=stdout,
        stderr=stderr,
        duration_ms=duration_ms,
    ).write(Path(run_dir))
    sys.exit(1)


def main_cli() -> None:
    """CLI entry point: python -m archie_agent.exec.runner <run-dir>"""
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} <run-dir>", file=sys.stderr)
        sys.exit(2)

    run_dir = Path(sys.argv[1])
    if not run_dir.is_dir():
        print(f"Error: run directory does not exist: {run_dir}", file=sys.stderr)
        sys.exit(2)

    # Register SIGTERM handler for cancellation
    signal.signal(signal.SIGTERM, _sigterm_handler)
    _partial_state["run_dir"] = str(run_dir)

    run(run_dir)


if __name__ == "__main__":
    main_cli()
