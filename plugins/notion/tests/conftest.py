"""Notion plugin test fixtures (D16: fakes are a top-level module, not a pytest plugin)."""

from notion_fakes import notion_context_installed, policy  # noqa: F401
