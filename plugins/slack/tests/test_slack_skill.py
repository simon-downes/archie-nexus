"""Consumer-facing Slack skill contract tests.

Function coverage, frontmatter and generic leak checks live in tests/test_plugin_layout.py.
"""

from pathlib import Path


def test_slack_skill_documents_safety_rules_and_hides_api_details():
    skill = Path(__file__).resolve().parents[1] / "skills" / "slack" / "SKILL.md"
    text = skill.read_text(encoding="utf-8")
    assert "name: slack" in text
    for term in (
        "SlackRateLimitError",
        "retry_after_seconds",
        "SlackMutationIndeterminateError",
        "scope:",
        "deny:",
        "Do not automatically repeat",
        "Choose the right function",
        "When multiple people or conversations match",
    ):
        assert term in text
    for leaked_detail in (
        "users.conversations",
        "conversations.history",
        "conversations.replies",
        "results.messages",
    ):
        assert leaked_detail not in text
    for block in ("header", "section", "context", "divider"):
        assert f"`{block}`" in text
