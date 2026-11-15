"""AWS ``bin/credential-process``: prints stored base credentials as credential_process JSON.

The script runs as a subprocess against the root conftest's ``fake_agent_socket``, which
stands in for the agent's local API (plan 090). The last test uses the real AWS CLI on a
generated config, so it checks that the CLI resolves the base profile through
``credential_process`` without contacting AWS.
"""

import json
import os
import shutil
import subprocess
from datetime import UTC, datetime, timedelta

import pytest
from archie_plugins.aws.profiles import BASE_PROFILE, CREDENTIAL_PROCESS, render_config

pytestmark = pytest.mark.skipif(
    not all(shutil.which(tool) for tool in ("bash", "curl", "jq", "date")),
    reason="credential-process tests need bash, curl, jq and date",
)

ROUTE = "/credentials/aws"
FULL = {"access_key_id": "AKIAEXAMPLE", "secret_access_key": 's3cr"et\\x', "session_token": "tok"}


@pytest.fixture
def agent(fake_agent_socket):
    fake_agent_socket[ROUTE] = (200, {"value": dict(FULL)})
    return fake_agent_socket


def run(**extra: str) -> subprocess.CompletedProcess:
    env = {"PATH": os.environ["PATH"], "ARCHIE_AGENT_SOCKET": os.environ["ARCHIE_AGENT_SOCKET"]}
    return subprocess.run(
        [str(CREDENTIAL_PROCESS)], env={**env, **extra}, capture_output=True, text=True, check=False
    )


def test_prints_credential_process_json(agent):
    proc = run()
    assert proc.returncode == 0, proc.stderr
    data = json.loads(proc.stdout)
    expiration = datetime.fromisoformat(data.pop("Expiration"))
    assert data == {
        "Version": 1,
        "AccessKeyId": "AKIAEXAMPLE",
        "SecretAccessKey": 's3cr"et\\x',
        "SessionToken": "tok",
    }
    left = expiration - datetime.now(UTC)
    assert timedelta(minutes=19) < left <= timedelta(minutes=20)


def test_asks_the_agent_on_every_call(agent):
    assert json.loads(run().stdout)["AccessKeyId"] == "AKIAEXAMPLE"
    agent[ROUTE] = (200, {"value": {**FULL, "access_key_id": "AKIANEW"}})
    assert json.loads(run().stdout)["AccessKeyId"] == "AKIANEW"


def test_session_token_is_optional(agent):
    agent[ROUTE] = (200, {"value": {"access_key_id": "AKIA", "secret_access_key": "secret"}})
    proc = run()
    assert proc.returncode == 0, proc.stderr
    assert "SessionToken" not in json.loads(proc.stdout)


def test_default_socket_is_the_agents():
    text = CREDENTIAL_PROCESS.read_text()
    assert 'sock="${ARCHIE_AGENT_SOCKET:-/tmp/archie-agent/agent.sock}"' in text
    assert "credentials.yaml" not in text


@pytest.mark.parametrize(
    ("reply", "needle"),
    [
        ((200, {"value": None}), "aws credentials are not set"),
        ((200, {"value": {"access_key_id": "AKIA"}}), "aws credentials are not set"),
        (
            (200, {"value": {"access_key_id": 123, "secret_access_key": "s"}}),
            "aws credentials are not set",
        ),
        ((503, {"error": "orchestrator down"}), "could not get the aws credentials from the agent"),
    ],
    ids=["absent", "no-secret", "non-string-key", "agent-error"],
)
def test_unusable_reply_fails_with_a_hint(fake_agent_socket, reply, needle):
    fake_agent_socket[ROUTE] = reply
    proc = run()
    assert proc.returncode == 1
    assert proc.stdout == ""
    assert needle in proc.stderr


def test_unreachable_agent_fails(tmp_path):
    proc = run(ARCHIE_AGENT_SOCKET=str(tmp_path / "none.sock"))
    assert proc.returncode == 1
    assert "could not get the aws credentials from the agent" in proc.stderr


@pytest.mark.skipif(shutil.which("aws") is None, reason="needs the AWS CLI")
def test_aws_cli_resolves_base_profile_through_credential_process(agent, tmp_path):
    config = tmp_path / "aws-config"
    config.write_text(render_config("eu-west-1", {"agent-dev": "arn:aws:iam::1:role/r"}))
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(tmp_path),
        "ARCHIE_AGENT_SOCKET": os.environ["ARCHIE_AGENT_SOCKET"],
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
