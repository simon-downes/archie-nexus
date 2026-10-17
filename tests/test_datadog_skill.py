"""Consumer-facing Datadog skill contract tests."""

import inspect
from pathlib import Path

from archie_agent.exec.tools import get_all_tools
from archie_agent.exec.tools.datadog import client as client_module
from archie_agent.exec.tools.datadog.client import DatadogError
from archie_agent.skills import _scan_directory

ROOT = Path(__file__).parents[1]
SKILL = ROOT / "persona" / "skills" / "datadog" / "SKILL.md"


def test_skill_documents_every_public_function():
    text = SKILL.read_text(encoding="utf-8")
    functions = sorted(key for key in get_all_tools() if key.startswith("datadog."))
    assert len(functions) == 15
    for function in functions:
        assert f"`{function}(" in text, function


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
        "ARCHIE_HOME_DIR",
        "credentials.yaml",
        "/api/v1",
        "/api/v2",
    ):
        assert leaked not in text, leaked


def test_skill_is_discovered():
    catalog = {}
    _scan_directory(ROOT / "persona" / "skills", catalog)
    assert "datadog" in catalog
    assert "managedby:archie" in catalog["datadog"].description
