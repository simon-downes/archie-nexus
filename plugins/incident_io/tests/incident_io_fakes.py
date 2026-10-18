"""Shared fake incident.io client for tool tests (no network)."""

from __future__ import annotations

import sys
from contextlib import asynccontextmanager

import pytest
from archie_agent.exec.plugin import install_context
from archie_agent.exec.plugin_testing import make_plugin_context
from archie_shared.credentials.models import CREDENTIAL_TYPES

API_KEY = "fake-api-key"


def fake_credential(api_key=API_KEY):
    """An incident.io credential struct (complete by default)."""
    return CREDENTIAL_TYPES["incident_io"](api_key=api_key)


def incident_io_context(tmp_path, *, policy=None, settings=None, credential=None, **kwargs):
    """An incident.io ``PluginContext``; defaults: manifest policy, a complete credential."""
    return make_plugin_context(
        "incident_io",
        tmp_path=tmp_path,
        policy=policy,
        settings=settings,
        credential=fake_credential() if credential is None else credential,
        **kwargs,
    )


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


@pytest.fixture(autouse=True)
def incident_io_context_installed(tmp_path_factory):
    """Install a default incident.io context (manifest policy, fake complete credential).

    Installed and cleared by value, not by token, because async tests run in a copied context.
    """
    root = tmp_path_factory.mktemp("incident-io-ctx")
    install_context(incident_io_context(root))
    yield root
    install_context(None)


@pytest.fixture
def policy(incident_io_context_installed):
    """``policy(snapshot)`` installs an incident.io context with that runner policy snapshot."""

    def install(snapshot):
        install_context(incident_io_context(incident_io_context_installed, policy=snapshot))

    return install


@pytest.fixture
def fake(monkeypatch):
    """Install a FakeClient as ``incident_io_client`` in the named tool modules."""

    def install(responses, *modules):
        client = FakeClient(responses)

        @asynccontextmanager
        async def factory():
            yield client

        for module in modules:
            target = sys.modules[f"archie_plugins.incident_io.{module}"]
            monkeypatch.setattr(target, "incident_io_client", factory)
        return client

    return install
