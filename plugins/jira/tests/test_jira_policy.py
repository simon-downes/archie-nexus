"""Jira policy gates read the plugin context and keep their messages (070 M9)."""

import pytest
from archie_plugins.jira.client import JiraPolicyError
from archie_plugins.jira.policy import (
    JiraValidationError,
    JiraWritePolicyError,
    require_read,
    require_write,
)


def test_default_policy_allows_reads_and_blocks_writes():
    require_read("PLAT")
    with pytest.raises(JiraWritePolicyError, match="^Jira writes are disabled by policy$"):
        require_write("PLAT")


def test_read_disabled(policy):
    policy({"jira": {"read": {"enabled": False}}})
    with pytest.raises(JiraPolicyError, match="^Jira reads are disabled by policy$"):
        require_read()


def test_read_scope(policy):
    policy({"jira": {"read": {"enabled": True, "scope": ["plat"]}}})
    require_read("PLAT")
    require_read()
    with pytest.raises(
        JiraPolicyError, match="^Jira resource is outside the configured read scope$"
    ):
        require_read("OTHER")


def test_write_scope(policy):
    policy({"jira": {"write": {"enabled": True, "scope": ["PLAT"]}}})
    require_write("PLAT")
    with pytest.raises(
        JiraWritePolicyError, match="^Jira resource is outside the configured write scope$"
    ):
        require_write("OTHER")


@pytest.mark.parametrize("scope", [None, []])
def test_empty_scope_is_unrestricted(policy, scope):
    policy({"jira": {"read": {"enabled": True, "scope": scope}}})
    require_read("ANY")


def test_invalid_scope_in_either_block_fails_closed(policy):
    policy({"jira": {"write": {"enabled": False, "scope": ["not a key"]}}})
    with pytest.raises(JiraValidationError, match="^Invalid Jira project key$"):
        require_read("PLAT")
