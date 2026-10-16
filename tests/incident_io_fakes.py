"""Shared fake incident.io client for tool tests (no network)."""

from __future__ import annotations

import sys
from contextlib import asynccontextmanager

import pytest
from archie_shared.tool_policy import reset_policy_snapshot, set_policy_snapshot


class FakeClient:
    """Records calls and serves canned responses keyed by API path."""

    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    async def get(self, path, params=None):
        self.calls.append(("get", path, dict(params or {})))
        return self.responses[path]

    async def items(self, path, key, params=None):
        self.calls.append(("items", path, dict(params or {})))
        return self.responses[path][key]

    async def paginate(self, path, key, limit, params=None, *, max_page_size=100, keep=None):
        self.calls.append(("paginate", path, dict(params or {}), limit, max_page_size))
        items = self.responses[path][key]
        if keep:
            items = [item for item in items if keep(item)]
        return items[:limit]


@pytest.fixture
def fake(monkeypatch):
    """Install a FakeClient as ``incident_io_client`` in the named tool modules."""
    token = set_policy_snapshot({})

    def install(responses, *modules):
        client = FakeClient(responses)

        @asynccontextmanager
        async def factory():
            yield client

        for module in modules:
            target = sys.modules[f"archie_agent.exec.tools.incident_io.{module}"]
            monkeypatch.setattr(target, "incident_io_client", factory)
        return client

    yield install
    reset_policy_snapshot(token)
