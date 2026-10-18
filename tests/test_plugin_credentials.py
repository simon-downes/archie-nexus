"""Tests for manifest → credential provider/struct generation (plan 070 M3)."""

from pathlib import Path

import msgspec
import pytest
from archie_shared.credentials.models import OAuthCredential
from archie_shared.credentials.providers import OAuthProvider, StaticProvider
from archie_shared.plugins import load_manifest
from archie_shared.plugins.credentials import (
    credential_type_from_manifest,
    provider_from_manifest,
)


def _plugin(root: Path, name: str, body: str):
    d = root / name
    d.mkdir(parents=True)
    (d / "plugin.yaml").write_text(body)
    return load_manifest(d)


def test_static_provider_and_struct(tmp_path):
    loaded = _plugin(
        tmp_path,
        "demo_svc",
        "name: demo_svc\n"
        "credential:\n"
        "  kind: static\n"
        "  fields: [api_key, region]\n"
        "  env: {api_key: DEMO_API_KEY}\n"
        "  set_env: true\n"
        "  login: aws_chain\n",
    )
    provider = provider_from_manifest(loaded)
    assert provider == StaticProvider(
        name="demo_svc",
        fields=["api_key", "region"],
        env={"api_key": "DEMO_API_KEY"},
        set_env=True,
        login="aws_chain",
    )
    assert provider.can_refresh_noninteractive is False

    cls = credential_type_from_manifest(loaded)
    assert cls.__name__ == "DemoSvcCredential"
    assert cls.__struct_fields__ == ("api_key", "region")
    empty = cls()
    assert empty.api_key is None and empty.region is None
    value = msgspec.convert({"api_key": "k"}, cls)
    assert value.api_key == "k" and value.region is None
    with pytest.raises(msgspec.ValidationError):
        msgspec.convert({"api_key": "k", "stale": "x"}, cls)


def test_static_site_setting_default_becomes_provider_site(tmp_path):
    loaded = _plugin(
        tmp_path,
        "demo",
        "name: demo\n"
        "credential: {kind: static, fields: [api_key]}\n"
        "settings:\n"
        "  site: {type: str, default: example.eu}\n",
    )
    assert provider_from_manifest(loaded).site == "example.eu"


def test_static_without_site_setting_has_no_site(tmp_path):
    loaded = _plugin(
        tmp_path,
        "demo",
        "name: demo\ncredential: {kind: static, fields: [api_key]}\n"
        "settings:\n  region: {type: str, default: eu}\n",
    )
    assert provider_from_manifest(loaded).site is None


def test_oauth_provider_and_struct(tmp_path):
    loaded = _plugin(
        tmp_path,
        "demo",
        "name: demo\n"
        "credential:\n"
        "  kind: oauth\n"
        "  authorization_endpoint: https://a.example/auth\n"
        "  token_endpoint: https://a.example/token\n"
        "  scopes: [one, two]\n"
        "  token_path: user.access_token\n"
        "  extra_params: {prompt: consent}\n",
    )
    provider = provider_from_manifest(loaded)
    assert provider == OAuthProvider(
        name="demo",
        authorization_endpoint="https://a.example/auth",
        token_endpoint="https://a.example/token",
        scopes=["one", "two"],
        token_path="user.access_token",
        extra_params={"prompt": "consent"},
    )
    assert provider.can_refresh_noninteractive is True
    assert credential_type_from_manifest(loaded) is OAuthCredential


def test_oauth_defaults(tmp_path):
    loaded = _plugin(tmp_path, "demo", "name: demo\ncredential: {kind: oauth, server_url: s}\n")
    provider = provider_from_manifest(loaded)
    assert provider.scopes is None and provider.extra_params is None
    assert provider.token_path == "access_token"
    assert provider.refresh_token_path == "refresh_token"
    assert provider.expires_in_path == "expires_in"


def test_plugin_without_credential_is_rejected(tmp_path):
    loaded = _plugin(tmp_path, "demo", "name: demo\n")
    with pytest.raises(ValueError, match="no credential"):
        provider_from_manifest(loaded)
    with pytest.raises(ValueError, match="no credential"):
        credential_type_from_manifest(loaded)
