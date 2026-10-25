"""The Cloudflare skill carries the read-only rules and names only real cf commands (plan 072)."""

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

SKILL = Path(__file__).resolve().parents[1] / "skills" / "cloudflare" / "SKILL.md"

AREAS = (
    "Workers",
    "Queues",
    "Workflows",
    "Workers VPC",
    "R2",
    "Workers KV",
    "Zones and domains",
    "DNS",
    "SSL and certificates",
    "WAF",
    "Turnstile",
    "Tunnels",
)

# A command path is the leading lowercase words after `cf`, stopping at the first flag,
# placeholder, or anything else (`-z`, `<worker-id>`, `"query"`, `|`).
_COMMAND = re.compile(r"(?:`|^\s*)cf((?: [a-z0-9][a-z0-9-]*)+)", re.MULTILINE)
# Commands whose following words name another command (`cf schema workers list`): both the
# command and the named command are checked.
_TAKES_WORDS = {"schema"}


def _text() -> str:
    return SKILL.read_text(encoding="utf-8")


def _section(heading: str) -> str:
    text = _text()
    start = text.index(f"# {heading}\n")
    end = text.find("\n# ", start + 1)
    return text[start : end if end != -1 else None]


def command_paths(text: str) -> list[str]:
    paths = set()
    for match in _COMMAND.finditer(text):
        words = match.group(1).split()
        if words[0] in _TAKES_WORDS:
            paths.add(words[0])
            words = words[1:]
        if words:
            paths.add(" ".join(words))
    return sorted(paths)


@pytest.mark.parametrize("area", AREAS)
def test_skill_covers_area(area):
    assert f"\n### {area}\n" in _text()


@pytest.mark.parametrize(
    "phrase",
    [
        "managed by Terraform",
        "Never create, change or delete Cloudflare resources",
        "A 403 or authentication error means the token's scope is working",
        "don't work around it",
        "Never pass `--force`",
        "Create and update commands don't ask: only the token stops them",
        "Don't read secret values",
        "`cf tunnels token get`",
        "`cf kv keys get`",
        "`cf r2 objects get`",
    ],
)
def test_principles_state_rule(phrase):
    assert phrase in _section("Principles")


def test_no_example_command_passes_force():
    assert not re.search(r"cf [^`\n]*--force", _text())


def test_secret_reads_appear_only_in_principles():
    rest = _text().replace(_section("Principles"), "")
    for secret in ("cf tunnels token get", "cf kv keys get", "cf r2 objects get"):
        assert secret not in rest


def test_command_extraction_stops_at_arguments():
    sample = (
        "`cf zones settings get <setting-id> -z <zone>` and\n"
        "   cf workers list | jq\n"
        '`cf schema dns records list` `cf cli search "x"`'
    )
    assert command_paths(sample) == [
        "cli search",
        "dns records list",
        "schema",
        "workers list",
        "zones settings get",
    ]


def _cf() -> str | None:
    return shutil.which("cf")


@pytest.mark.skipif(_cf() is None, reason="cf CLI is not on PATH (it is installed in the image)")
@pytest.mark.parametrize("path", command_paths(_text()))
def test_skill_command_exists(path, tmp_path):
    # An unknown subcommand prints its parent group's help and still exits 0, so the
    # help header must name the full command path.
    env = {**os.environ, "HOME": str(tmp_path), "CF_SEND_TELEMETRY": "false"}
    result = subprocess.run(
        [_cf(), *path.split(), "--help"],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    header = result.stdout.lstrip().splitlines()[0]
    assert re.fullmatch(rf"cf {re.escape(path)}( .*)?", header), header
