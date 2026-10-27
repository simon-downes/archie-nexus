"""The Jira skill carries the rules that used to be system-prompt guidelines (070 M9)."""

from pathlib import Path

SKILL = Path(__file__).resolve().parents[1] / "skills" / "jira" / "SKILL.md"


def _global_rules() -> str:
    text = SKILL.read_text(encoding="utf-8")
    start = text.index("## Global rules")
    end = text.index("\n## ", start + 1)
    return text[start:end]


def test_skill_has_namespace_and_credential_rule():
    rules = _global_rules()
    assert "Use Jira only through the `jira` namespace" in rules
    assert "never provide credentials" in rules


def test_skill_has_fetch_issue_before_comments_rule():
    assert "Fetch issue details with `jira.get_issue` before requesting comments" in _global_rules()


def test_skill_has_structured_filters_rule():
    rules = _global_rules()
    assert "Use structured Jira filters" in rules
    assert "raw JQL" in rules and "not available" in rules


def test_namespace_rule_is_not_duplicated():
    rules = _global_rules()
    assert rules.count("`jira.<function>`") == 1
    assert "Call Jira only through" not in rules


def test_skill_documents_markdown_contract():
    text = SKILL.read_text(encoding="utf-8")
    assert "Markdown" in text and "32,767" in text
    assert "Re-saving such a description turns these into plain text" in text
    assert "plain text, not ADF" not in text
    assert '"description": "Plain text' not in text


def test_readme_lists_adf_module():
    readme = SKILL.parents[2] / "README.md"
    assert "`adf.py`" in readme.read_text(encoding="utf-8")
