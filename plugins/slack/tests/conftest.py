"""Slack plugin test fixtures (D16: fakes are a top-level module, not a pytest plugin)."""

from slack_fakes import policy, slack_context_installed  # noqa: F401
