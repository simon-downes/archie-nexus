import pytest
from archie_shared.credentials.runtime import runtime_environment
from archie_shared.credentials.store import replace_credential
from archie_shared.plugins.config import PluginAuthConfig, PluginConfig
from archie_shared.schemas import NexusConfig


def test_runtime_environment_projects_no_aws_credentials_by_default(monkeypatch, tmp_path):
    """aws is read per call by its credential_process; bedrock is never projected."""
    monkeypatch.setenv("ARCHIE_HOME_DIR", str(tmp_path))
    replace_credential("aws", {"access_key_id": "AKIA", "secret_access_key": "secret"})
    replace_credential("bedrock", {"aws_access_key_id": "BEDROCK"})
    assert runtime_environment(NexusConfig()) == {}


def test_runtime_environment_projects_aws_when_override_sets_env(monkeypatch, tmp_path):
    monkeypatch.setenv("ARCHIE_HOME_DIR", str(tmp_path))
    replace_credential("aws", {"access_key_id": "AKIA", "secret_access_key": "secret"})
    replace_credential("bedrock", {"aws_access_key_id": "BEDROCK"})
    config = NexusConfig(plugins={"aws": PluginConfig(auth=PluginAuthConfig(set_env=True))})
    env = runtime_environment(config)
    assert env == {"AWS_ACCESS_KEY_ID": "AKIA", "AWS_SECRET_ACCESS_KEY": "secret"}


def test_runtime_environment_projects_scalr_but_not_github(monkeypatch, tmp_path):
    monkeypatch.setenv("ARCHIE_HOME_DIR", str(tmp_path))
    replace_credential("github", {"personal_token": "personal", "work_token": "work"})
    replace_credential("scalr", {"token": "scalr"})
    config = NexusConfig(
        plugins={"scalr": PluginConfig(settings={"hostname": "host", "account": "acct"})}
    )
    assert runtime_environment(config) == {
        "SCALR_TOKEN": "scalr",
        "SCALR_HOSTNAME": "host",
        "SCALR_ACCOUNT": "acct",
    }


def test_runtime_environment_skips_unset_settings(monkeypatch, tmp_path):
    monkeypatch.setenv("ARCHIE_HOME_DIR", str(tmp_path))
    replace_credential("scalr", {"token": "scalr"})
    config = NexusConfig(plugins={"scalr": PluginConfig(settings={"hostname": "host"})})
    assert runtime_environment(config) == {"SCALR_TOKEN": "scalr", "SCALR_HOSTNAME": "host"}


def test_old_scalr_entry_with_hostname_fails_only_scalr(monkeypatch, tmp_path):
    """A pre-D9 stored scalr entry is rejected by name; other providers are unaffected."""
    from archie_shared.credentials.store import get_credential

    monkeypatch.setenv("ARCHIE_HOME_DIR", str(tmp_path))
    replace_credential("scalr", {"token": "t", "hostname": "h", "account": "a"})
    replace_credential("github", {"personal_token": "p"})
    assert get_credential("github").personal_token == "p"
    with pytest.raises(ValueError, match="'scalr'"):
        runtime_environment(NexusConfig())


def test_runtime_environment_rejects_invalid_override_mapping(tmp_path, monkeypatch):
    monkeypatch.setenv("ARCHIE_HOME_DIR", str(tmp_path))
    config = NexusConfig(
        plugins={
            "github": PluginConfig(auth=PluginAuthConfig(env={"token": "GH_TOKEN"}, set_env=True))
        }
    )
    with pytest.raises(ValueError, match="unknown credential field"):
        runtime_environment(config)
