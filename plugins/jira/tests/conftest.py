"""Jira plugin test fixtures (D16: fakes are a top-level module, not a pytest plugin)."""

from jira_fakes import jira_context_installed, policy  # noqa: F401
