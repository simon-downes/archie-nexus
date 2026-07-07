"""Tests for the credential store (store.py)."""

import os
import stat

import pytest
from archie_shared.credentials.models import BedrockCredential, OAuthCredential
from archie_shared.credentials.store import (
    get_credential,
    load_store,
    save_store,
    set_credential,
    store_path,
)

# --- Tests: store_path ---


def test_store_path_default(monkeypatch, tmp_path):
    """Default store path is ~/.nexus/credentials.yaml."""
    monkeypatch.setenv("ARCHIE_HOME_DIR", str(tmp_path))
    assert store_path() == tmp_path / "credentials.yaml"


# --- Tests: load_store ---


def test_load_store_missing_file(monkeypatch, tmp_path):
    """Missing file → empty dict."""
    monkeypatch.setenv("ARCHIE_HOME_DIR", str(tmp_path))
    assert load_store() == {}


def test_load_store_empty_file(monkeypatch, tmp_path):
    """Empty YAML → empty dict."""
    monkeypatch.setenv("ARCHIE_HOME_DIR", str(tmp_path))
    (tmp_path / "credentials.yaml").write_text("")
    assert load_store() == {}


def test_load_store_valid(monkeypatch, tmp_path):
    """Valid YAML loads correctly."""
    monkeypatch.setenv("ARCHIE_HOME_DIR", str(tmp_path))
    (tmp_path / "credentials.yaml").write_text(
        "bedrock:\n  aws_access_key_id: AKIA123\n  aws_secret_access_key: secret\n"
    )
    result = load_store()
    assert result["bedrock"]["aws_access_key_id"] == "AKIA123"


def test_load_store_permission_warning(monkeypatch, tmp_path, capsys):
    """Warns to stderr if permissions are too permissive."""
    monkeypatch.setenv("ARCHIE_HOME_DIR", str(tmp_path))
    cred_file = tmp_path / "credentials.yaml"
    cred_file.write_text("bedrock:\n  aws_access_key_id: test\n")
    os.chmod(cred_file, 0o644)
    load_store()
    captured = capsys.readouterr()
    assert "Warning" in captured.err
    assert "0644" in captured.err or "0o644" in captured.err


# --- Tests: save_store ---


def test_save_store_creates_file(monkeypatch, tmp_path):
    """save_store creates the file with 0600 permissions."""
    monkeypatch.setenv("ARCHIE_HOME_DIR", str(tmp_path))
    save_store({"bedrock": {"aws_access_key_id": "AKIA"}})
    path = tmp_path / "credentials.yaml"
    assert path.exists()
    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == 0o600


def test_save_store_atomic_roundtrip(monkeypatch, tmp_path):
    """save then load preserves data."""
    monkeypatch.setenv("ARCHIE_HOME_DIR", str(tmp_path))
    data = {"bedrock": {"aws_access_key_id": "AKIA", "aws_secret_access_key": "secret"}}
    save_store(data)
    assert load_store() == data


def test_save_store_preserves_unknown_services(monkeypatch, tmp_path):
    """Unknown service keys are preserved through save/load cycle."""
    monkeypatch.setenv("ARCHIE_HOME_DIR", str(tmp_path))
    data = {"bedrock": {"aws_access_key_id": "X"}, "future_service": {"api_key": "abc"}}
    save_store(data)
    result = load_store()
    assert result["future_service"]["api_key"] == "abc"


# --- Tests: get_credential ---


def test_get_credential_bedrock(monkeypatch, tmp_path):
    """get_credential returns typed BedrockCredential."""
    monkeypatch.setenv("ARCHIE_HOME_DIR", str(tmp_path))
    (tmp_path / "credentials.yaml").write_text(
        "bedrock:\n"
        "  aws_access_key_id: AKIA123\n"
        "  aws_secret_access_key: secret123\n"
        "  aws_session_token: tok\n"
    )
    cred = get_credential("bedrock")
    assert isinstance(cred, BedrockCredential)
    assert cred.aws_access_key_id == "AKIA123"
    assert cred.aws_secret_access_key == "secret123"
    assert cred.aws_session_token == "tok"


def test_get_credential_partial(monkeypatch, tmp_path):
    """Partial entry loads with None for missing fields."""
    monkeypatch.setenv("ARCHIE_HOME_DIR", str(tmp_path))
    (tmp_path / "credentials.yaml").write_text("bedrock:\n  aws_access_key_id: AKIA\n")
    cred = get_credential("bedrock")
    assert cred.aws_access_key_id == "AKIA"
    assert cred.aws_secret_access_key is None
    assert cred.aws_session_token is None


def test_get_credential_missing_service(monkeypatch, tmp_path):
    """Missing service returns None."""
    monkeypatch.setenv("ARCHIE_HOME_DIR", str(tmp_path))
    (tmp_path / "credentials.yaml").write_text("bedrock:\n  aws_access_key_id: X\n")
    assert get_credential("notion") is None


def test_get_credential_unknown_field_rejected(monkeypatch, tmp_path):
    """Unknown field in a known service → ValueError."""
    monkeypatch.setenv("ARCHIE_HOME_DIR", str(tmp_path))
    (tmp_path / "credentials.yaml").write_text(
        "bedrock:\n  aws_access_key_id: X\n  unknown_field: oops\n"
    )
    with pytest.raises(ValueError, match="Validation error"):
        get_credential("bedrock")


def test_get_credential_unknown_service_key_error(monkeypatch, tmp_path):
    """Unknown service not in CREDENTIAL_TYPES → KeyError."""
    monkeypatch.setenv("ARCHIE_HOME_DIR", str(tmp_path))
    (tmp_path / "credentials.yaml").write_text("mystery:\n  key: val\n")
    with pytest.raises(KeyError, match="Unknown service"):
        get_credential("mystery")


def test_get_credential_with_type_override(monkeypatch, tmp_path):
    """Explicit credential_type bypasses CREDENTIAL_TYPES lookup."""
    monkeypatch.setenv("ARCHIE_HOME_DIR", str(tmp_path))
    (tmp_path / "credentials.yaml").write_text("notion:\n  access_token: abc\n  client_id: cid\n")
    cred = get_credential("notion", OAuthCredential)
    assert cred.access_token == "abc"
    assert cred.client_id == "cid"


# --- Tests: set_credential ---


def test_set_credential_new(monkeypatch, tmp_path):
    """set_credential creates entry for new service."""
    monkeypatch.setenv("ARCHIE_HOME_DIR", str(tmp_path))
    set_credential("bedrock", {"aws_access_key_id": "AKIA", "aws_secret_access_key": "sec"})
    cred = get_credential("bedrock")
    assert cred.aws_access_key_id == "AKIA"


def test_set_credential_merge(monkeypatch, tmp_path):
    """set_credential merges into existing entry."""
    monkeypatch.setenv("ARCHIE_HOME_DIR", str(tmp_path))
    set_credential("bedrock", {"aws_access_key_id": "AKIA1"})
    set_credential("bedrock", {"aws_secret_access_key": "sec"})
    cred = get_credential("bedrock")
    assert cred.aws_access_key_id == "AKIA1"
    assert cred.aws_secret_access_key == "sec"


def test_set_credential_none_removes_field(monkeypatch, tmp_path):
    """Setting a field to None removes it from the entry."""
    monkeypatch.setenv("ARCHIE_HOME_DIR", str(tmp_path))
    set_credential("bedrock", {"aws_access_key_id": "AKIA", "aws_session_token": "tok"})
    set_credential("bedrock", {"aws_session_token": None})
    store = load_store()
    assert "aws_session_token" not in store["bedrock"]
