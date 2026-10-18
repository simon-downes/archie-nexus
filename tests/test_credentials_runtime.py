import pytest
from archie_shared.credentials.runtime import runtime_environment
from archie_shared.credentials.store import replace_credential
from archie_shared.plugins.config import PluginAuthConfig, PluginConfig
from archie_shared.schemas import NexusConfig


def test_runtime_environment_projects_selected_credentials(monkeypatch, tmp_path):
    monkeypatch.setenv("ARCHIE_HOME_DIR", str(tmp_path))
    replace_credential("aws", {"access_key_id": "AKIA", "secret_access_key": "secret"})
    replace_credential("bedrock", {"aws_access_key_id": "BEDROCK"})
    env = runtime_environment(NexusConfig())
    assert env == {"AWS_ACCESS_KEY_ID": "AKIA", "AWS_SECRET_ACCESS_KEY": "secret"}


def test_runtime_environment_projects_scalr_but_not_github(monkeypatch, tmp_path):
    monkeypatch.setenv("ARCHIE_HOME_DIR", str(tmp_path))
    replace_credential("github", {"personal_token": "personal", "work_token": "work"})
    replace_credential("scalr", {"token": "scalr", "hostname": "host", "account": "acct"})
    assert runtime_environment(NexusConfig()) == {
        "SCALR_TOKEN": "scalr",
        "SCALR_HOSTNAME": "host",
        "SCALR_ACCOUNT": "acct",
    }


def test_runtime_environment_rejects_invalid_override_mapping(tmp_path, monkeypatch):
    monkeypatch.setenv("ARCHIE_HOME_DIR", str(tmp_path))
    config = NexusConfig(
        plugins={
            "github": PluginConfig(auth=PluginAuthConfig(env={"token": "GH_TOKEN"}, set_env=True))
        }
    )
    with pytest.raises(ValueError, match="unknown credential field"):
        runtime_environment(config)
