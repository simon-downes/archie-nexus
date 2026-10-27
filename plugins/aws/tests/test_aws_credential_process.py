"""AWS ``bin/credential-process``: prints stored base credentials as credential_process JSON.

The script runs as a subprocess against a temporary ``ARCHIE_HOME_DIR``. The last test
uses the real AWS CLI on a generated config, so it checks that the CLI resolves the base
profile through ``credential_process`` without contacting AWS.
"""

import json
import os
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from archie_plugins.aws.profiles import BASE_PROFILE, CREDENTIAL_PROCESS, render_config

pytestmark = pytest.mark.skipif(
    not all(shutil.which(tool) for tool in ("bash", "yq", "jq", "date")),
    reason="credential-process tests need bash, yq, jq and date",
)

FULL = (
    'aws:\n  access_key_id: AKIAEXAMPLE\n  secret_access_key: s3cr"et\\\\x\n  session_token: tok\n'
)


@pytest.fixture
def nexus_home(tmp_path):
    home = tmp_path / "nexus"
    home.mkdir()
    return home


def run(nexus_home: Path, **extra: str) -> subprocess.CompletedProcess:
    env = {"PATH": os.environ["PATH"], "ARCHIE_HOME_DIR": str(nexus_home), **extra}
    return subprocess.run(
        [str(CREDENTIAL_PROCESS)], env=env, capture_output=True, text=True, check=False
    )


def test_prints_credential_process_json(nexus_home):
    (nexus_home / "credentials.yaml").write_text(FULL)
    proc = run(nexus_home)
    assert proc.returncode == 0, proc.stderr
    data = json.loads(proc.stdout)
    expiration = datetime.fromisoformat(data.pop("Expiration"))
    assert data == {
        "Version": 1,
        "AccessKeyId": "AKIAEXAMPLE",
        "SecretAccessKey": 's3cr"et\\\\x',
        "SessionToken": "tok",
    }
    left = expiration - datetime.now(UTC)
    assert timedelta(minutes=19) < left <= timedelta(minutes=20)


def test_reads_the_store_on_every_call(nexus_home):
    store = nexus_home / "credentials.yaml"
    store.write_text(FULL)
    assert json.loads(run(nexus_home).stdout)["AccessKeyId"] == "AKIAEXAMPLE"
    store.write_text(FULL.replace("AKIAEXAMPLE", "AKIANEW"))
    assert json.loads(run(nexus_home).stdout)["AccessKeyId"] == "AKIANEW"


def test_session_token_is_optional(nexus_home):
    (nexus_home / "credentials.yaml").write_text(
        "aws:\n  access_key_id: AKIA\n  secret_access_key: secret\n"
    )
    proc = run(nexus_home)
    assert proc.returncode == 0, proc.stderr
    assert "SessionToken" not in json.loads(proc.stdout)


def test_default_home_is_dot_nexus(tmp_path):
    userhome = tmp_path / "userhome"
    (userhome / ".nexus").mkdir(parents=True)
    (userhome / ".nexus" / "credentials.yaml").write_text(FULL)
    env = {"PATH": os.environ["PATH"], "HOME": str(userhome)}
    proc = subprocess.run(
        [str(CREDENTIAL_PROCESS)], env=env, capture_output=True, text=True, check=False
    )
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["AccessKeyId"] == "AKIAEXAMPLE"


@pytest.mark.parametrize(
    ("store", "needle"),
    [
        (None, "no credential store"),
        ("github:\n  personal_token: p\n", "aws credentials are not set"),
        ("aws:\n  access_key_id: AKIA\n", "aws credentials are not set"),
        ("aws:\n  access_key_id: 123\n  secret_access_key: s\n", "aws credentials are not set"),
        ("aws: [unclosed\n", "could not read"),
    ],
)
def test_unusable_store_fails_with_login_hint(nexus_home, store, needle):
    if store is not None:
        (nexus_home / "credentials.yaml").write_text(store)
    proc = run(nexus_home)
    assert proc.returncode == 1
    assert proc.stdout == ""
    assert needle in proc.stderr


@pytest.mark.skipif(shutil.which("aws") is None, reason="needs the AWS CLI")
def test_aws_cli_resolves_base_profile_through_credential_process(nexus_home, tmp_path):
    (nexus_home / "credentials.yaml").write_text(FULL)
    config = tmp_path / "aws-config"
    config.write_text(render_config("eu-west-1", {"agent-dev": "arn:aws:iam::1:role/r"}))
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(tmp_path),
        "ARCHIE_HOME_DIR": str(nexus_home),
        "AWS_CONFIG_FILE": str(config),
        "AWS_SHARED_CREDENTIALS_FILE": str(tmp_path / "none"),
    }
    proc = subprocess.run(
        ["aws", "configure", "export-credentials", "--profile", BASE_PROFILE],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["AccessKeyId"] == "AKIAEXAMPLE"
    listed = subprocess.run(
        ["aws", "configure", "list-profiles"], env=env, capture_output=True, text=True, check=True
    )
    assert listed.stdout.split() == [BASE_PROFILE, "agent-dev"]
