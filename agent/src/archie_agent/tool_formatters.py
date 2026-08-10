"""UI summary formatters for tool calls.

Produces compact summaries with Rich markup for the TUI iteration block display.
Two phases per tool call:
- format_tool_pending: shown while tool is running (params only)
- format_tool_complete: shown after tool finishes (params + result metadata)

The harness computes these and sends them over the wire. The TUI renders
the markup directly via Textual's Static widget.
"""

from __future__ import annotations


def _esc(text: str) -> str:
    """Escape Rich markup characters in arbitrary text."""
    return text.replace("[", r"\[").replace("]", r"\]")


def _hi(text: str) -> str:
    """Highlight a key parameter — bold white."""
    return f"[bold]{_esc(text)}[/]"


def _dim(text: str) -> str:
    """Dim text for secondary info."""
    return f"[dim]{_esc(text)}[/]"


def _default(text: str) -> str:
    """Render a value that was NOT model-specified (a resolved default).

    Coloured distinctly (muted italic) so it's clear the model relied on the
    default rather than passing the value explicitly — handy for confirming
    which path/args are actually being used.
    """
    return f"[#676767 italic]{_esc(text)}[/]"


def _condense_lines(text: str, head: int = 3, tail: int = 3, threshold: int = 10) -> list[str]:
    """Return output lines, collapsing the middle when there are too many.

    <= `threshold` lines: returned as-is. Otherwise the first `head` and last
    `tail` lines are kept with a `… N lines hidden …` marker in between.
    """
    lines = text.rstrip("\n").split("\n")
    if len(lines) <= threshold:
        return lines
    hidden = len(lines) - head - tail
    return [*lines[:head], f"… {hidden} lines hidden …", *lines[-tail:]]


def _format_output_block(text: str, colour: str = "dim") -> str:
    """Render command/diff output as an indented, condensed multi-line block."""
    rendered = []
    for line in _condense_lines(text):
        if line.startswith("… ") and line.endswith(" …"):
            rendered.append(f"  [dim italic]{_esc(line)}[/]")
        else:
            rendered.append(f"  [{colour}]{_esc(line)}[/]")
    return "\n".join(rendered)


def _format_diff_block(diff: str) -> str:
    """Render a unified diff with per-line +/- colouring, condensed if long."""
    rendered = []
    for line in _condense_lines(diff, head=6, tail=6, threshold=40):
        if line.startswith("… ") and line.endswith(" …"):
            rendered.append(f"  [dim italic]{_esc(line)}[/]")
            continue
        if line.startswith(("+++", "---")):
            continue
        if line.startswith("@@"):
            rendered.append(f"  [cyan]{_esc(line)}[/]")
        elif line.startswith("+"):
            rendered.append(f"  [green]{_esc(line)}[/]")
        elif line.startswith("-"):
            rendered.append(f"  [red]{_esc(line)}[/]")
        else:
            rendered.append(f"  [dim]{_esc(line)}[/]")
    return "\n".join(rendered)



def format_tool_pending(name: str, input_dict: dict) -> str:
    """Produce the summary shown while the tool is running (Rich markup).

    Args:
        name: Tool name (e.g. 'read', 'grep', 'shell', 'exec').
        input_dict: The tool_use input parameters.

    Returns:
        Rich markup string for the pending state.
    """
    match name:
        case "read":
            path = input_dict.get("path", "")
            offset = input_dict.get("offset")
            limit = input_dict.get("limit")
            if offset and limit:
                return f"Read {_hi(path)} {_dim(f'(L{offset}–{offset + limit - 1})')}"
            elif offset:
                return f"Read {_hi(path)} {_dim(f'(L{offset}–?)')}"
            return f"Read {_hi(path)} {_default('(all lines)')}"

        case "write":
            path = input_dict.get("path", "")
            return f"Write {_hi(path)}"

        case "edit":
            path = input_dict.get("path", "")
            return f"Edit {_hi(path)}"

        case "glob":
            pattern = input_dict.get("pattern", "")
            path = input_dict.get("path")
            root = _hi(path.rstrip("/")) if path else _default("/workspace")
            return f"Glob {root}/{_hi(pattern)}"

        case "grep":
            pattern = input_dict.get("pattern", "")
            include = input_dict.get("include", "")
            path = input_dict.get("path")
            root = _hi(path.rstrip("/")) if path else _default("/workspace")
            target = f"{root}/{_hi(include)}" if include else root
            return f"Grep {_hi(pattern)} in {target}"

        case "shell":
            command = input_dict.get("command", "")
            return f"Shell {_hi(command)}"

        case "code":
            path = input_dict.get("path")
            root = _hi(path) if path else _default("/workspace")
            name_param = input_dict.get("name", "")
            if name_param:
                return f"Code search {_hi(name_param)} in {root}"
            return f"Code {root}"

        case "web_fetch":
            url = input_dict.get("url", "")
            mode = input_dict.get("mode")
            mode_tag = _hi(mode) if mode else _default("selective")
            return f"Fetch {_dim(url)} {mode_tag}"

        case "web_search":
            query = input_dict.get("query", "")
            return f"Web search {_hi(query)}"

        case "exec":
            # For exec the TUI renders the raw source as a collapsible code block,
            # so the pending summary IS the source (not a Rich one-liner).
            return input_dict.get("source", "")

        case "skill":
            skill_name = input_dict.get("name", "")
            file = input_dict.get("file")
            if file:
                return f"Skill {_hi(skill_name + '/' + file)}"
            return f"Skill {_hi(skill_name)}"

        case _:
            return _esc(name)


def format_tool_complete(
    name: str, input_dict: dict, result: str, is_error: bool
) -> str:
    """Produce the completed summary with result metadata (Rich markup).

    Args:
        name: Tool name.
        input_dict: The tool_use input parameters.
        result: The tool result content string.
        is_error: Whether the tool call failed.

    Returns:
        Rich markup string for the completed state.
    """
    if is_error:
        error_msg = result.split("\n")[0][:80]
        base = format_tool_pending(name, input_dict)
        return f"{base} — [red]{_esc(error_msg)}[/]"

    match name:
        case "read":
            path = input_dict.get("path", "")
            # Parse total lines from header "File: ... (N lines)"
            total = ""
            for line in result.split("\n")[:3]:
                if "lines)" in line:
                    try:
                        total = line.split("(")[1].split(" lines")[0]
                    except (IndexError, ValueError):
                        pass
                    break
            if total:
                return f"Read {_hi(path)} {_dim(f'({total} lines)')}"
            return f"Read {_hi(path)}"

        case "write":
            path = input_dict.get("path", "")
            # Result is "Written: path (N lines)"
            try:
                line_count = result.split("(")[1].split(" lines")[0]
                return f"Write {_hi(path)} {_dim(f'({line_count} lines)')}"
            except (IndexError, ValueError):
                return f"Write {_hi(path)}"

        case "edit":
            path = input_dict.get("path", "")
            # Count diff lines
            added = result.count("\n+") - result.count("\n+++")
            removed = result.count("\n-") - result.count("\n---")
            header = (
                f"Edit {_hi(path)} {_dim('(' + ', '.join([p for p in (f'+{added}' if added else '', f'-{removed}' if removed else '') if p]) + ')')}"
                if (added or removed)
                else f"Edit {_hi(path)}"
            )
            diff_body = _format_diff_block(result)
            return f"{header}\n{diff_body}" if diff_body else header

        case "glob":
            pattern = input_dict.get("pattern", "")
            path = input_dict.get("path")
            root = _hi(path.rstrip("/")) if path else _default("/workspace")
            target = f"{root}/{_hi(pattern)}"
            # First line is header "N files, most recent first"
            first_line = result.split("\n")[0] if result else ""
            try:
                count = int(first_line.split()[0])
            except (IndexError, ValueError):
                count = 0
            if "No files found" in result:
                return f"Glob {target} {_dim('(no files)')}"
            return f"Glob {target} {_dim(f'({count} files)')}"

        case "grep":
            pattern = input_dict.get("pattern", "")
            include = input_dict.get("include", "")
            path = input_dict.get("path")
            root = _hi(path.rstrip("/")) if path else _default("/workspace")
            target = f"{root}/{_hi(include)}" if include else root
            if "No matches found" in result:
                return f"Grep {_hi(pattern)} in {target} {_dim('(no matches)')}"
            # Count matches (lines with |) and files (lines ending with :)
            match_count = sum(1 for x in result.split("\n") if "|" in x[:8])
            file_count = sum(
                1 for x in result.split("\n")
                if x.rstrip().endswith(":") and not x.startswith(" ")
            )
            return f"Grep {_hi(pattern)} in {target} {_dim(f'({match_count} in {file_count} files)')}"

        case "shell":
            command = input_dict.get("command", "")
            # Parse exit code from "[exit: N]"
            exit_code = None
            for line in result.split("\n"):
                if "[exit:" in line:
                    try:
                        exit_code = int(line.split("[exit:")[1].split("]")[0].strip())
                    except (IndexError, ValueError):
                        pass
                    break
            if exit_code and exit_code != 0:
                header = f"Shell {_hi(command)} {_dim(f'(exit {exit_code})')}"
                # Strip the trailing "[exit: N]" marker line from displayed output
                body_text = "\n".join(
                    ln for ln in result.split("\n") if "[exit:" not in ln
                ).strip("\n")
                body = _format_output_block(body_text, colour="red") if body_text else ""
                return f"{header}\n{body}" if body else header
            return f"Shell {_hi(command)}"

        case "code":
            path = input_dict.get("path")
            root = _hi(path) if path else _default("/workspace")
            name_param = input_dict.get("name", "")
            if "No symbols found" in result:
                if name_param:
                    return f"Code search {_hi(name_param)} in {root} {_dim('(no results)')}"
                return f"Code {root} {_dim('(no symbols)')}"
            # Count symbol lines. Overview mode lines end with "[{kind}, line N-M]";
            # search mode lines look like "{path}:N-M — {signature}".
            symbol_lines = [
                x
                for x in result.split("\n")
                if x.strip()
                and (("[" in x and "line " in x) or " — " in x)
                and not x.startswith(("Showing ", "…", "No symbols"))
            ]
            count = len(symbol_lines)
            if name_param:
                return f"Code search {_hi(name_param)} in {root} {_dim(f'({count} symbols)')}"
            return f"Code {root} {_dim(f'({count} symbols)')}"

        case "web_fetch":
            url = input_dict.get("url", "")
            line_count = len(result.splitlines())
            return f"Fetch {_dim(url)} {_dim(f'({line_count} lines)')}"

        case "web_search":
            query = input_dict.get("query", "")
            if "No results found" in result:
                return f"Web search {_hi(query)} {_dim('(no results)')}"
            # Count result blocks (separated by double newlines)
            count = len([b for b in result.split("\n\n") if b.strip()])
            return f"Web search {_hi(query)} {_dim(f'({count} results)')}"

        case "exec":
            # Exec completion is handled by ToolEntry's own complete() method
            return "Exec"

        case "skill":
            skill_name = input_dict.get("name", "")
            file = input_dict.get("file")
            if file:
                return f"Skill {_hi(skill_name + '/' + file)}"
            if "already loaded" in result:
                return f"Skill {_hi(skill_name)} {_dim('(already loaded)')}"
            return f"Skill {_hi(skill_name)} {_dim('(loaded)')}"

        case _:
            size = len(result)
            return f"{_esc(name)} {_dim(f'({size} chars)')}"
