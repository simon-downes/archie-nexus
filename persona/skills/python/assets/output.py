"""
Output primitives template for CLI tools using Rich.

Copy and adapt this file as output.py in your project. Adjust colours,
width, and primitives to suit your needs.
"""

from datetime import UTC, datetime, timedelta

from rich.console import Console
from rich.rule import Rule
from rich.table import Table

WIDTH = 80

console = Console(width=WIDTH)
console_err = Console(stderr=True, width=WIDTH)

# --- Semantic colour palette ---
# Adjust these to suit your project. Keep the semantic roles stable.
C_HEADING = "bright_magenta"  # section titles
C_KEY = "bright_blue"         # resource names, identifiers
C_CMD = "bright_cyan"         # runnable commands
C_VAL = "#5f87d7"             # paths, filenames, data values
C_PLAIN = "white"             # default body text
C_MUTED = "bright_black"      # secondary info, types, timestamps
C_CHROME = "dim"              # rules, bullets, separators

C_OK = "#50c878"              # success icon only
C_WARN = "#f0c050"            # warning icon only
C_ERR = "bright_red"          # error icon only


def _icon(ok: bool | None) -> str:
    if ok is True:
        return f"[{C_OK}]✓[/]"
    if ok is False:
        return f"[{C_ERR}]✗[/]"
    return f"[{C_WARN}]![/]"


# --- Primitives ---


def section(title: str) -> None:
    """Section header with left-aligned rule."""
    console.print()
    console.print(Rule(f"[{C_HEADING}]{title}[/]", style=C_CHROME, align="left"))
    console.print()


def status_table(*rows: tuple) -> None:
    """Aligned status table. Rows: (ok, label, detail) or (ok, label, sublabel, detail)."""
    t = Table(show_header=False, box=None, padding=(0, 2), pad_edge=True, expand=False)
    t.add_column(width=3)
    t.add_column(style=C_KEY)
    if any(len(r) > 3 for r in rows):
        t.add_column(style=C_MUTED)
    t.add_column(style=C_MUTED)
    for r in rows:
        t.add_row(_icon(r[0]), *[str(x) for x in r[1:]])
    console.print(t)


def kv_table(*rows: tuple[str, str]) -> None:
    """Key-value pairs."""
    t = Table(show_header=False, box=None, padding=(0, 2), pad_edge=True, expand=False)
    t.add_column(style=C_MUTED)
    t.add_column(style=C_VAL)
    for k, v in rows:
        t.add_row(k, v)
    console.print(t)


def data_table(*rows: tuple, styles: list[str] | None = None) -> None:
    """Generic aligned table with custom column styles."""
    if not rows:
        return
    t = Table(show_header=False, box=None, padding=(0, 2), pad_edge=True, expand=False)
    col_styles = styles or [C_PLAIN] * len(rows[0])
    for s in col_styles:
        t.add_column(style=s)
    for r in rows:
        t.add_row(*[str(x) for x in r])
    console.print(t)


def print_success(message: str) -> None:
    """Success message. Accepts Rich markup for highlighting."""
    console.print(f"  [{C_OK}]✓[/]  {message}")


def print_error(message: str) -> None:
    """Error message. Accepts Rich markup for highlighting."""
    console_err.print(f"  [{C_ERR}]✗[/]  {message}")


def print_warning(message: str) -> None:
    """Warning message. Accepts Rich markup for highlighting."""
    console.print(f"  [{C_WARN}]![/]  {message}")


def print_info(message: str) -> None:
    """Info message. Accepts Rich markup for highlighting."""
    console.print(f"  [{C_KEY}]→[/]  {message}")


def bullet_list(items: list[str]) -> None:
    """Indented bullet list. Items accept Rich markup."""
    for item in items:
        console.print(f"    [{C_CHROME}]•[/]  {item}")


def empty_state(text: str) -> None:
    """Placeholder for empty results."""
    console.print(f"  [{C_MUTED}]{text}[/]")


def cmd(text: str) -> None:
    """Display a runnable command."""
    console.print(f"    [{C_CMD}]{text}[/]")


def human_time(iso_timestamp: str) -> str:
    """Convert ISO timestamp to human-friendly relative time."""
    try:
        target = datetime.fromisoformat(iso_timestamp)
        now = datetime.now(target.tzinfo or UTC)
        delta = target - now
        if delta.total_seconds() < 0:
            return _format_delta(-delta) + " ago"
        return "in " + _format_delta(delta)
    except (ValueError, TypeError):
        return iso_timestamp


def _format_delta(delta: timedelta) -> str:
    seconds = int(delta.total_seconds())
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        h, m = seconds // 3600, (seconds % 3600) // 60
        return f"{h}h {m}m" if m else f"{h}h"
    return f"{seconds // 86400}d"
