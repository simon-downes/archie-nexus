"""Tests for live tool-policy reload (plan 065)."""

import pytest
from archie_agent.tool_policy_live import (
    RECOVERED_KIND,
    RELOAD_FAILED_KIND,
    LiveToolPolicy,
    load_policy_snapshot,
    startup_runner_state,
)

NOTION_WRITE_ON = """\
plugins:
  notion:
    policy:
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


def test_missing_config_yields_manifest_defaults(home):
    snapshot = load_policy_snapshot()
    assert set(snapshot) == {"datadog", "google", "incident_io", "jira", "notion", "slack"}
    for entry in snapshot.values():
        assert entry == {
            "read": {"enabled": True, "scope": None},
            "write": {"enabled": False, "scope": None},
        }


def test_old_tools_section_fails_reload_and_keeps_last_valid(home):
    live = LiveToolPolicy({"notion": {"write": {"enabled": True}}})
    (home / "config.yaml").write_text("tools:\n  notion:\n    write: {enabled: false}\n")
    snapshot, notice = live.refresh()
    assert snapshot == {"notion": {"write": {"enabled": True}}}
    assert notice is not None and "archie config migrate" in notice.message


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

    (home / "config.yaml").write_text("plugins: [unclosed\n")
    results = [live.refresh() for _ in range(3)]

    assert all(snapshot == good for snapshot, _ in results)
    notices = [notice for _, notice in results if notice is not None]
    assert len(notices) == 1
    assert notices[0].kind == RELOAD_FAILED_KIND
    assert "last valid" in notices[0].message


def test_changed_error_notifies_again(home):
    live = LiveToolPolicy({})
    (home / "config.yaml").write_text("plugins: [unclosed\n")
    _, first = live.refresh()
    (home / "config.yaml").write_text("plugins:\n  notion:\n    bogus: 1\n")
    _, second = live.refresh()
    _, third = live.refresh()
    assert first is not None and second is not None
    assert second.kind == RELOAD_FAILED_KIND
    assert first.message != second.message
    assert third is None


def test_same_error_after_recovery_notifies_again(home):
    live = LiveToolPolicy({})
    (home / "config.yaml").write_text("plugins: [unclosed\n")
    live.refresh()
    (home / "config.yaml").write_text(NOTION_WRITE_ON)
    live.refresh()
    (home / "config.yaml").write_text("plugins: [unclosed\n")
    _, notice = live.refresh()
    assert notice is not None and notice.kind == RELOAD_FAILED_KIND


def test_recovery_notifies_once(home):
    live = LiveToolPolicy({})
    (home / "config.yaml").write_text("plugins: [unclosed\n")
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


def test_runner_context_settings_are_startup_only_policy_is_live(home):
    from archie_shared.plugins import load_manifests
    from archie_shared.schemas import load_nexus_config

    (home / "config.yaml").write_text("plugins:\n  datadog:\n    settings: {site: datadoghq.com}\n")
    settings, enabled = startup_runner_state(load_nexus_config(), load_manifests())
    live = LiveToolPolicy(load_policy_snapshot(), settings=settings, enabled=enabled)
    before = live.runner_context()
    assert before["settings"]["datadog"] == {"site": "datadoghq.com"}
    assert before["policy"]["datadog"]["write"]["enabled"] is False

    (home / "config.yaml").write_text(
        "plugins:\n  datadog:\n    enabled: true\n    settings: {site: datadoghq.eu}\n"
        "    policy:\n      write: {enabled: true}\n  jira: {enabled: false}\n"
    )
    snapshot, _ = live.refresh()
    after = live.runner_context(snapshot)
    assert after["policy"]["datadog"]["write"]["enabled"] is True
    assert after["settings"] == before["settings"]
    assert after["enabled"] == before["enabled"] and "jira" in after["enabled"]


def test_static_runner_context_defaults():
    from archie_shared.plugins import load_manifests

    live = LiveToolPolicy.static({"jira": {}})
    assert live.runner_context() == {
        "policy": {"jira": {}},
        "settings": {},
        "enabled": sorted(load_manifests()),
    }


def test_runner_context_enabled_is_always_a_list():
    assert LiveToolPolicy({}, loader=None, enabled=[]).runner_context()["enabled"] == []
    assert LiveToolPolicy({}, loader=None, enabled=("jira",)).runner_context()["enabled"] == [
        "jira"
    ]
