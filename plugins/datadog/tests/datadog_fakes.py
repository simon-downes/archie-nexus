"""Shared fake Datadog client for tool tests (no network)."""

from __future__ import annotations

import sys
from contextlib import asynccontextmanager

import pytest
from archie_agent.exec.plugin import install_context
from archie_agent.exec.plugin_testing import make_plugin_context
from archie_plugins.datadog.client import allowed
from archie_shared.credentials.models import CREDENTIAL_TYPES

API_KEY = "fake-api-key"
APP_KEY = "fake-app-key"


def fake_credential(api_key=API_KEY, app_key=APP_KEY):
    """A Datadog credential struct (complete by default)."""
    return CREDENTIAL_TYPES["datadog"](api_key=api_key, app_key=app_key)


def datadog_context(tmp_path, *, policy=None, settings=None, credential=None, **kwargs):
    """A Datadog ``PluginContext``; defaults: manifest policy/settings, a complete credential."""
    return make_plugin_context(
        "datadog",
        tmp_path=tmp_path,
        policy=policy,
        settings=settings,
        credential=fake_credential() if credential is None else credential,
        **kwargs,
    )


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
def datadog_context_installed(tmp_path_factory):
    """Install a default Datadog context (manifest policy/settings, fake complete credential).

    Installed and cleared by value, not by token, because async tests run in a copied context.
    """
    root = tmp_path_factory.mktemp("datadog-ctx")
    install_context(datadog_context(root))
    yield root
    install_context(None)


@pytest.fixture
def policy(datadog_context_installed):
    """``policy(snapshot)`` installs a Datadog context with that runner policy snapshot."""

    def install(snapshot):
        install_context(datadog_context(datadog_context_installed, policy=snapshot))

    return install


@pytest.fixture
def dd_fake(monkeypatch, policy):
    """Install a FakeClient as ``datadog_client`` in the named tool modules."""

    def install(responses, *modules):
        client = FakeClient(responses)

        @asynccontextmanager
        async def factory():
            yield client

        for module in modules:
            target = sys.modules[f"archie_plugins.datadog.{module}"]
            monkeypatch.setattr(target, "datadog_client", factory)
        return client

    return install
