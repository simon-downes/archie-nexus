"""Out-of-memory handling for tool processes and the Kiro backend (094).

Tool processes raise their own ``oom_score_adj`` to :data:`TOOL_OOM_SCORE_ADJ`, so when the
VM or container runs out of memory the kernel kills a tool before the agent or Kiro.
Raising one's own score needs no privilege, and descendants inherit it. The child shell
writes the score before it runs the command, so there is no window in which the command
could fork with the old score.

The container's cgroup counts the processes the OOM killer killed in it
(``oom_kill`` in ``memory.events``, including VM-wide kills). Comparing that count before
and after a tool call, or a Kiro process's life, tells the agent the OOM killer ran.
"""

from __future__ import annotations

import shlex
import shutil
from collections.abc import Sequence
from pathlib import Path

TOOL_OOM_SCORE_ADJ = 1000
"""The score tool processes give themselves: the kernel's "kill this first" value."""

OOM_SCORE_ADJ_PATH = "/proc/self/oom_score_adj"
MEMORY_EVENTS = Path("/sys/fs/cgroup/memory.events")


def _set_score() -> str:
    # A group, so a failed redirection is silenced too: in ``echo >x 2>/dev/null`` the
    # error for ``>x`` is printed before ``2>/dev/null`` applies.
    return f"{{ echo {TOOL_OOM_SCORE_ADJ} >{shlex.quote(OOM_SCORE_ADJ_PATH)}; }} 2>/dev/null"


def shell_prefix(command: str) -> str:
    """``command`` run by a shell that has first raised its own OOM score.

    The prefix ends with a newline, so ``command`` is parsed exactly as it would be on its
    own (a leading comment or heredoc still works).
    """
    return f"{_set_score()}\n{command}"


def argv_wrapper(args: Sequence[str]) -> list[str]:
    """``args`` run through ``sh``, which raises its OOM score and then ``exec``s them.

    ``args`` is returned unchanged when its executable can't be found, so spawning it raises
    the same ``OSError`` as before rather than ``sh`` exiting 127.
    """
    if not args or shutil.which(args[0]) is None:
        return list(args)
    return ["/bin/sh", "-c", f'{_set_score()}; exec "$@"', "sh", *args]


def oom_kill_count() -> int | None:
    """Processes the OOM killer has killed in this container's cgroup, or None if unknown."""
    try:
        text = MEMORY_EVENTS.read_text(encoding="ascii")
    except (OSError, UnicodeDecodeError):
        return None
    for line in text.splitlines():
        key, _, value = line.partition(" ")
        if key == "oom_kill":
            try:
                return int(value)
            except ValueError:
                return None
    return None


def kills_since(before: int | None) -> int:
    """OOM kills in this container since ``before`` was read; 0 when either count is unknown."""
    after = oom_kill_count()
    if before is None or after is None:
        return 0
    return max(after - before, 0)


def tool_warning(kills: int) -> str:
    """The line added to a tool result when the OOM killer ran while the tool did."""
    return (
        f"[archie warning: the out-of-memory killer killed {kills} process(es) in this "
        "container while this ran; the output may be incomplete. Run memory-heavy commands "
        "one at a time.]"
    )
