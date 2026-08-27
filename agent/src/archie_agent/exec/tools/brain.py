"""Native curated brain search tool."""

from __future__ import annotations

from archie_shared.brain import BrainError, search

from archie_agent.exec.tools import tool


@tool(
    guidelines=(
        "Use `brain_search` to find curated knowledge, then use regular `read`, `write`, or `edit` for exact entry access and changes.",
    )
)
async def brain_search(query: str, limit: int = 10) -> str:
    """Search curated brain entries by frontmatter and body text; use regular filesystem tools for exact access and changes.

    Args:
        query: Whitespace-separated search terms. Terms are matched literally and case-insensitively.
        limit: Maximum results (default 10, capped at 50; zero returns no results).
    """
    try:
        import json

        return json.dumps(search(query, limit), ensure_ascii=False)
    except BrainError as e:
        raise ValueError(str(e)) from e
