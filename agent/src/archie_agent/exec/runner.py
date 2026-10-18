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
import contextlib
import functools
import inspect
import io
import json
import os
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


def _parse_context(raw: object) -> tuple[dict, dict, list[str] | None]:
    """Split a ``context.json`` payload into ``(policy, settings, enabled)`` (D11).

    Any other shape (including the pre-D11 bare policy snapshot) yields
    ``({}, {}, None)``: empty policy, no settings, every plugin enabled.
    """
    if not isinstance(raw, dict) or "policy" not in raw:
        return {}, {}, None
    policy = raw.get("policy")
    settings = raw.get("settings")
    enabled = raw.get("enabled")
    return (
        policy if isinstance(policy, dict) else {},
        settings if isinstance(settings, dict) else {},
        [str(pid) for pid in enabled] if isinstance(enabled, list) else None,
    )


def _build_namespace(calls: list[CallRecord], context: dict | None = None) -> dict:
    """Build the namespace injected into model code.

    Imports exec tool functions and wraps each for audit logging. ``context``
    is the runner context (``{"policy", "settings", "enabled"}``); plugin
    entries are imported only for ``enabled`` ids (None = all).
    """
    policy, settings, enabled = _parse_context(context)
    import json as _json
    import os as _os
    import re as _re
    from pathlib import Path as _Path

    namespace = {
        "asyncio": asyncio,
        "os": _os,
        "json": _json,
        "re": _re,
        "Path": _Path,
        "__builtins__": __builtins__,
    }

    try:
        import types as _types

        from archie_shared.tool_policy import set_policy_snapshot

        from archie_agent.exec.tools import get_all_tools

        set_policy_snapshot(policy)
        service_namespaces: dict[str, object] = {}
        for key, func in get_all_tools().items():
            name = key.rsplit(".", 1)[-1]
            namespace_name = getattr(func, "_namespace", None)
            wrapped = _wrap_for_audit(func, key, calls)
            if namespace_name:
                service = service_namespaces.setdefault(namespace_name, _types.SimpleNamespace())
                setattr(service, name, wrapped)
            else:
                namespace[name] = wrapped
        _add_plugin_tools(service_namespaces, calls, enabled, policy=policy, settings=settings)
        namespace.update(service_namespaces)
        namespace["_tool_policy"] = policy
    except ImportError as exc:
        raise RuntimeError("Failed to load exec tools") from exc

    return namespace


def _add_plugin_tools(
    service_namespaces: dict[str, object],
    calls: list[CallRecord],
    enabled: list[str] | None = None,
    *,
    policy: dict | None = None,
    settings: dict | None = None,
) -> None:
    """Load enabled plugins' tools into their own namespaces; failures name the plugin.

    Each plugin gets one ``PluginContext`` built from the runner context's policy and
    settings; each of its tools runs inside that context (D12). Settings missing from
    the runner context fall back to the manifest defaults.
    """
    import types as _types

    from archie_shared.plugins import PluginManifestError, load_manifests, resolved_settings

    from archie_agent.exec.plugin import build_context
    from archie_agent.exec.plugin_loader import PluginLoadError, load_plugins
    from archie_agent.exec.tools import _subprocess

    policy = policy or {}
    settings = settings or {}
    try:
        manifests = load_manifests()
        loaded = load_plugins(manifests, enabled=enabled)
    except (PluginManifestError, PluginLoadError) as exc:
        raise RuntimeError(str(exc)) from exc

    core_namespaces = set(service_namespaces)
    for plugin_id, tools in loaded.items():
        plugin_settings = settings.get(plugin_id)
        if not isinstance(plugin_settings, dict):
            plugin_settings = resolved_settings(plugin_id, {}, manifests)
        ctx = build_context(
            plugin_id,
            manifest=manifests[plugin_id],
            policy_snapshot=policy,
            settings=plugin_settings,
            workspace=_subprocess.WORKSPACE,
        )
        for key, func in tools.items():
            namespace_name, name = key.rsplit(".", 1)
            if namespace_name in core_namespaces:
                raise RuntimeError(
                    f"Plugin '{plugin_id}' namespace '{namespace_name}' collides with a core "
                    "exec namespace"
                )
            service = service_namespaces.setdefault(namespace_name, _types.SimpleNamespace())
            setattr(service, name, _wrap_for_audit(func, key, calls, plugin_context=ctx))


def _wrap_for_audit(fn, name: str, calls: list[CallRecord], plugin_context=None):
    """Wrap an exec tool function to record audit log entries.

    With ``plugin_context`` the call runs with that context installed (D12); the
    token is set and reset inside this coroutine, so concurrent calls into
    different plugins each see their own context.
    """

    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        args_summary = _summarise_args(args, kwargs)
        t0 = time.time()
        ok = True
        scope = contextlib.nullcontext()
        if plugin_context is not None:
            from archie_agent.exec.plugin import use_context

            scope = use_context(plugin_context)
        try:
            with scope:
                result = await fn(*args, **kwargs)
        except Exception:
            ok = False
            raise
        finally:
            duration_ms = int((time.time() - t0) * 1000)
            calls.append(CallRecord(fn=name, args=args_summary, ms=duration_ms, ok=ok))
        # Exec has no image channel: tools returning a ToolImage degrade to its
        # text caption so model code keeps getting a str.
        from archie_agent.exec.tools.images import ToolImage

        if isinstance(result, ToolImage):
            return (
                f"{result.caption}\n[Image content is not available inside exec; "
                "call the native `read` tool to view it.]"
            )
        return result

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
    context = None
    context_path = run_dir / "context.json"
    if context_path.exists():
        try:
            context = json.loads(context_path.read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError):
            context = None
    try:
        namespace = _build_namespace(calls, context)
    except Exception as e:
        Envelope.error_envelope(type(e).__name__, str(e), tb=traceback.format_exc()).write(run_dir)
        return

    # Run model code from the workspace root so plain open()/Path resolve
    # relative paths the same way the helpers do. Done here (not at spawn) so
    # workspace files can't shadow modules imported by the runner itself.
    from archie_agent.exec.tools._subprocess import _resolve_cwd

    workspace = _resolve_cwd(None)
    if workspace:
        os.chdir(workspace)

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
        from archie_agent.exec.tools import ToolError

        error_info = ErrorInfo(
            type=type(e).__name__,
            message=str(e),
            traceback="" if isinstance(e, ToolError) else traceback.format_exc(),
        )

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
    )
    envelope.write(run_dir)

    # Clean up tree-sitter parser cache to prevent segfault during interpreter
    # shutdown. The C-level Parser/Language destructor ordering is non-deterministic
    # during GC, causing double-free crashes. Clearing while the interpreter is
    # still alive gives deterministic destruction.
    try:
        from archie_agent.exec.tools.code import _parsers

        _parsers.clear()
    except (ImportError, AttributeError):
        pass


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

    Envelope.error_envelope(
        "Cancelled",
        "Execution cancelled by user",
        stdout=stdout,
        stderr=stderr,
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
