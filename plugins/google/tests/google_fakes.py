"""Shared Google plugin test helpers: a default context with a fake complete credential."""

from __future__ import annotations

import pytest
from archie_agent.exec.plugin import install_context
from archie_agent.exec.plugin_testing import make_plugin_context
from archie_shared.credentials.models import OAuthCredential

ACCESS_TOKEN = "t"


def fake_credential(access_token=ACCESS_TOKEN):
    """A Google OAuth credential (complete by default)."""
    return OAuthCredential(access_token=access_token)


def google_context(tmp_path, *, policy=None, settings=None, credential=None, **kwargs):
    """A Google ``PluginContext``; defaults: manifest policy/settings, a complete credential."""
    return make_plugin_context(
        "google",
        tmp_path=tmp_path,
        policy=policy,
        settings=settings,
        credential=fake_credential() if credential is None else credential,
        **kwargs,
    )


@pytest.fixture(autouse=True)
def google_context_installed(tmp_path_factory):
    """Install a default Google context (manifest policy/settings, fake complete credential).

    Installed and cleared by value, not by token, because async tests run in a copied context.
    """
    root = tmp_path_factory.mktemp("google-ctx")
    install_context(google_context(root))
    yield root
    install_context(None)


@pytest.fixture
def policy(google_context_installed):
    """``policy(snapshot)`` installs a Google context with that runner policy snapshot."""

    def install(snapshot):
        install_context(google_context(google_context_installed, policy=snapshot))

    return install


@pytest.fixture
def google_settings(google_context_installed):
    """``google_settings(values)`` installs a Google context with those settings."""

    def install(values):
        install_context(google_context(google_context_installed, settings=values))

    return install
