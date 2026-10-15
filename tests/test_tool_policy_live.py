"""Tests for live tool-policy reload (plan 065)."""

import pytest
from archie_agent.tool_policy_live import (
    RECOVERED_KIND,
    RELOAD_FAILED_KIND,
    LiveToolPolicy,
    load_policy_snapshot,
)

NOTION_WRITE_ON = """\
tools:
  notion:
    write:
      enabled: true
"""


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("ARCHIE_HOME_DIR", str(tmp_path))
    return tmp_path


def test_load_policy_snapshot_reads_config(home):
    (home / "config.yaml").write_text(NOTION_WRITE_ON)
    snapshot = load_policy_snapshot()
    assert snapshot["notion"]["write"]["enabled"] is True


def test_missing_config_yields_defaults(home):
    assert load_policy_snapshot() == {}


def test_refresh_picks_up_file_change(home):
    live = LiveToolPolicy({})
    (home / "config.yaml").write_text(NOTION_WRITE_ON)
    snapshot, notice = live.refresh()
    assert snapshot["notion"]["write"]["enabled"] is True
    assert notice is None


def test_invalid_config_keeps_last_valid_and_notifies_once(home):
    (home / "config.yaml").write_text(NOTION_WRITE_ON)
    live = LiveToolPolicy({})
    good, _ = live.refresh()

    (home / "config.yaml").write_text("tools: [unclosed\n")
    results = [live.refresh() for _ in range(3)]

    assert all(snapshot == good for snapshot, _ in results)
    notices = [notice for _, notice in results if notice is not None]
    assert len(notices) == 1
    assert notices[0].kind == RELOAD_FAILED_KIND
    assert "last valid" in notices[0].message


def test_changed_error_notifies_again(home):
    live = LiveToolPolicy({})
    (home / "config.yaml").write_text("tools: [unclosed\n")
    _, first = live.refresh()
    (home / "config.yaml").write_text("tools:\n  notion:\n    bogus: 1\n")
    _, second = live.refresh()
    _, third = live.refresh()
    assert first is not None and second is not None
    assert first.message != second.message
    assert third is None


def test_recovery_notifies_once(home):
    live = LiveToolPolicy({})
    (home / "config.yaml").write_text("tools: [unclosed\n")
    live.refresh()
    (home / "config.yaml").write_text(NOTION_WRITE_ON)
    snapshot, notice = live.refresh()
    _, again = live.refresh()
    assert snapshot["notion"]["write"]["enabled"] is True
    assert notice is not None and notice.kind == RECOVERED_KIND
    assert again is None


def test_static_policy_never_reloads(home):
    (home / "config.yaml").write_text(NOTION_WRITE_ON)
    live = LiveToolPolicy.static({"jira": {"read": {"enabled": False}}})
    snapshot, notice = live.refresh()
    assert snapshot == {"jira": {"read": {"enabled": False}}}
    assert notice is None


def test_custom_loader_exception_falls_back():
    def boom():
        raise OSError("disk gone")

    live = LiveToolPolicy({"slack": {}}, loader=boom)
    snapshot, notice = live.refresh()
    assert snapshot == {"slack": {}}
    assert notice is not None and "disk gone" in notice.message
