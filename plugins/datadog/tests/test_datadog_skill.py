"""Consumer-facing Datadog skill contract tests.

Function coverage, frontmatter and generic leak checks live in tests/test_plugin_layout.py.
"""

import inspect
from pathlib import Path

from archie_agent.skills import _scan_directory
from archie_plugins.datadog import client as client_module
from archie_plugins.datadog.client import DatadogError

SKILLS = Path(__file__).resolve().parents[1] / "skills"
SKILL = SKILLS / "datadog" / "SKILL.md"


def test_skill_documents_every_error_class():
    text = SKILL.read_text(encoding="utf-8")
    errors = [
        name
        for name, value in inspect.getmembers(client_module, inspect.isclass)
        if issubclass(value, DatadogError) and value is not DatadogError
    ]
    assert len(errors) == 12
    for name in errors:
        assert f"`{name}`" in text, name
    assert "retry_after_seconds" in text


def test_skill_states_the_safety_rules():
    text = SKILL.read_text(encoding="utf-8")
    for term in (
        "managedby:archie",
        "Nothing can be deleted",
        "read-only",
        "write:",
        "Choose the right function",
    ):
        assert term in text, term


def test_skill_contains_no_credentials_or_raw_api_details():
    text = SKILL.read_text(encoding="utf-8")
    for leaked in (
        "api.datadoghq",
        "DD-API-KEY",
        "DD-APPLICATION-KEY",
        "/api/v1",
        "/api/v2",
    ):
        assert leaked not in text, leaked


def test_skill_is_discovered():
    catalog = {}
    _scan_directory(SKILLS, catalog)
    assert "datadog" in catalog
    assert "managedby:archie" in catalog["datadog"].description


def test_skill_names_permissions_and_query_syntax():
    text = SKILL.read_text(encoding="utf-8")
    for term in (
        "logs_read_config",
        "data_scanner_read",
        "CIDR(",
        "type:metric",
        "## Query syntax",
        "## How logs are processed",
        "fields=",
    ):
        assert term in text, term


def test_readme_names_every_registered_function():
    from archie_agent.exec.plugin import plugin_tools

    readme = (SKILLS.parent / "README.md").read_text(encoding="utf-8")
    names = sorted(key for key in plugin_tools() if key.startswith("datadog."))
    assert names
    missing = [name for name in names if f"`{name}`" not in readme]
    assert not missing, missing
