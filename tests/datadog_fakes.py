"""Shared fake Datadog client for tool tests (no network)."""

from __future__ import annotations

import sys
from contextlib import asynccontextmanager

import pytest
from archie_agent.exec.tools.datadog.client import allowed
from archie_shared.tool_policy import set_policy_snapshot


class Seq(list):
    """Responses served in order for repeated calls; the last one repeats."""


class FakeClient:
    """Records calls and serves canned responses keyed by ``(method, path)``.

    A response may be an exception instance (raised) or a ``Seq`` (served in order).
    Every call is checked against the real client allowlist.
    """

    def __init__(self, responses):
        self.responses = dict(responses)
        self.calls = []

    async def request(self, method, path, *, params=None, json=None):
        assert allowed(method, path), f"{method} {path} is outside the allowlist"
        self.calls.append((method, path, params, json))
        value = self.responses[(method, path)]
        if isinstance(value, Seq):
            value = value.pop(0) if len(value) > 1 else value[0]
        if isinstance(value, BaseException):
            raise value
        return value


@pytest.fixture(autouse=True)
def datadog_home(monkeypatch, tmp_path_factory, request):
    """Point ARCHIE_HOME_DIR at an empty directory for Datadog tests.

    The real client reads the Datadog site from ``config.yaml``; without this a developer's own
    config could change the host the tests expect.
    """
    if "datadog" in request.node.nodeid:
        monkeypatch.setenv("ARCHIE_HOME_DIR", str(tmp_path_factory.mktemp("datadog-home")))


@pytest.fixture
def policy():
    """Install a tool-policy snapshot; an empty one is installed before and after each test.

    The snapshot is reset by value, not by token, because async tests run in a copied context.
    """
    set_policy_snapshot({})
    yield set_policy_snapshot
    set_policy_snapshot({})


@pytest.fixture
def dd_fake(monkeypatch, policy):
    """Install a FakeClient as ``datadog_client`` in the named tool modules."""

    def install(responses, *modules):
        client = FakeClient(responses)

        @asynccontextmanager
        async def factory():
            yield client

        for module in modules:
            target = sys.modules[f"archie_agent.exec.tools.datadog.{module}"]
            monkeypatch.setattr(target, "datadog_client", factory)
        return client

    return install
