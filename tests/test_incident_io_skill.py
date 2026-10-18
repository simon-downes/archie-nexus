"""Consumer-facing incident.io skill contract tests."""

from pathlib import Path

from archie_agent.exec.tools import get_all_tools
from archie_agent.skills import _scan_directory

ROOT = Path(__file__).parents[1]
SKILL = ROOT / "plugins" / "incident_io" / "skills" / "incident-io" / "SKILL.md"


def test_skill_documents_every_public_function_and_error():
    text = SKILL.read_text(encoding="utf-8")
    functions = sorted(key for key in get_all_tools() if key.startswith("incident_io."))
    assert len(functions) == 18
    for function in functions:
        assert f"`{function}(" in text, function
    for term in (
        "IncidentIoConfigurationError",
        "IncidentIoPolicyError",
        "IncidentIoInputError",
        "IncidentIoRequestRejectedError",
        "IncidentIoAuthenticationError",
        "IncidentIoAuthorizationError",
        "IncidentIoNotFoundError",
        "IncidentIoRateLimitError",
        "retry_after_seconds",
        "IncidentIoTransportError",
        "IncidentIoResponseError",
        "read-only",
        "Choose the right function",
        "YYYY-MM-DD",
    ):
        assert term in text, term


def test_skill_contains_no_credentials_or_raw_api_details():
    text = SKILL.read_text(encoding="utf-8")
    for leaked in ("api.incident.io", "ARCHIE_HOME_DIR", "Bearer", "credentials.yaml", "/v2/"):
        assert leaked not in text, leaked


def test_skill_is_discovered():
    catalog = {}
    _scan_directory(ROOT / "plugins" / "incident_io" / "skills", catalog)
    assert "incident-io" in catalog
    assert "read-only" in catalog["incident-io"].description
