"""Shared Jira test context helpers (no network, no credential store)."""

from __future__ import annotations

import pytest
from archie_agent.exec.plugin import install_context
from archie_agent.exec.plugin_testing import make_plugin_context
from archie_shared.credentials.models import CREDENTIAL_TYPES

EMAIL = "a@example.com"
TOKEN = "fake-token"
CLOUD_ID = "cloud"


def fake_credential(email=EMAIL, token=TOKEN, cloud_id=CLOUD_ID):
    """A Jira credential struct (complete by default)."""
    return CREDENTIAL_TYPES["jira"](email=email, token=token, cloud_id=cloud_id)


_DEFAULT = object()


def jira_context(tmp_path, *, policy=None, settings=None, credential=_DEFAULT, **kwargs):
    """A Jira ``PluginContext``; defaults: manifest policy, a complete fake credential.

    Pass ``credential=None`` for a context with no stored credential.
    """
    return make_plugin_context(
        "jira",
        tmp_path=tmp_path,
        policy=policy,
        settings=settings,
        credential=fake_credential() if credential is _DEFAULT else credential,
        **kwargs,
    )


@pytest.fixture(autouse=True)
def jira_context_installed(tmp_path_factory):
    """Install a default Jira context (manifest policy, fake complete credential).

    Installed and cleared by value, not by token, because async tests run in a copied context.
    """
    root = tmp_path_factory.mktemp("jira-ctx")
    install_context(jira_context(root))
    yield root
    install_context(None)


@pytest.fixture
def policy(jira_context_installed):
    """``policy(snapshot, **kwargs)`` installs a Jira context with that runner policy snapshot."""

    def install(snapshot, **kwargs):
        install_context(jira_context(jira_context_installed, policy=snapshot, **kwargs))

    return install
