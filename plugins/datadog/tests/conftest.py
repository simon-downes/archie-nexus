"""Datadog plugin test fixtures (D16: fakes are a top-level module, not a pytest plugin)."""

from datadog_fakes import datadog_context_installed, dd_fake, policy  # noqa: F401
