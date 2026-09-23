"""Consumer-facing Slack skill contract tests."""

from pathlib import Path


def test_slack_skill_documents_public_contract_and_safety_rules():
    skill = Path(__file__).parents[1] / "persona" / "skills" / "slack" / "SKILL.md"
    text = skill.read_text(encoding="utf-8")
    assert "name: slack" in text
    for function in (
        "slack.search",
        "slack.conversations",
        "slack.users",
        "slack.messages",
        "slack.thread",
        "slack.send_message",
        "slack.react",
        "slack.reactions",
    ):
        assert function in text
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
        "ARCHIE_HOME_DIR",
    ):
        assert leaked_detail not in text
    for block in ("header", "section", "context", "divider"):
        assert f"`{block}`" in text
