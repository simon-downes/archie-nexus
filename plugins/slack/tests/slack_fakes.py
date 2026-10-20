"""Shared Slack test context helpers (no network, no credential store)."""

from __future__ import annotations

import pytest
from archie_agent.exec.plugin import install_context
from archie_agent.exec.plugin_testing import make_plugin_context
from archie_shared.credentials.models import OAuthCredential

ACCESS_TOKEN = "fake-access-token"


def fake_credential(access_token=ACCESS_TOKEN):
    """A Slack OAuth user-token credential (complete by default)."""
    return OAuthCredential(access_token=access_token)


_DEFAULT = object()


def slack_context(tmp_path, *, policy=None, settings=None, credential=_DEFAULT, **kwargs):
    """A Slack ``PluginContext``; defaults: manifest policy, a complete fake credential.

    Pass ``credential=None`` for a context with no stored credential. The cache lives
    under ``tmp_path/cache/slack``.
    """
    return make_plugin_context(
        "slack",
        tmp_path=tmp_path,
        policy=policy,
        settings=settings,
        credential=fake_credential() if credential is _DEFAULT else credential,
        **kwargs,
    )


@pytest.fixture(autouse=True)
def slack_context_installed(tmp_path_factory):
    """Install a default Slack context (manifest policy, fake complete credential).

    Installed and cleared by value, not by token, because async tests run in a copied context.
    Yields the context's home: its cache is ``<home>/cache/slack``.
    """
    root = tmp_path_factory.mktemp("slack-ctx")
    install_context(slack_context(root))
    yield root
    install_context(None)


@pytest.fixture
def policy(slack_context_installed):
    """``policy(snapshot, **kwargs)`` installs a Slack context with that runner policy snapshot."""

    def install(snapshot, **kwargs):
        install_context(slack_context(slack_context_installed, policy=snapshot, **kwargs))

    return install
