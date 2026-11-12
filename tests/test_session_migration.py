"""Tests for one-shot session-log migration and host CLI integration."""

import json
import os
import sqlite3
from pathlib import Path

import pytest
from archie_cli.cli import main
from archie_shared.events import decode_event
from archie_shared.session.migrate import MigrationStats, migrate_session_log, migrate_session_logs
from click.testing import CliRunner


def _legacy_line(*, event_id: str, turn_iteration: str) -> dict:
    return {
        "type": "llm_request",
        "id": event_id,
        "scope": None,
        "turn_iteration": turn_iteration,
        "model_key": "model",
        "sent_at": "2026-07-01T10:00:00+00:00",
        "duration_ms": 10,
        "status": "completed",
        "input_tokens": 1,
        "output_tokens": 2,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "context_tokens": 3,
        "cost_usd": 0.1,
        "stop_reason": None,
        "error": None,
        "subagent_index": None,
    }


def _write_legacy_log(path: Path) -> None:
    lines = [
        {
            "type": "session_started",
            "id": "session-1",
            "schema_version": 1,
            "sent_at": "2026-07-01T10:00:00+00:00",
            "model_key": "model",
        },
        {
            "type": "iteration_start",
            "id": "iteration-1",
            "turn_iteration": "2.1",
            "scope": None,
            "index": 1,
            "subagent_index": None,
        },
        _legacy_line(event_id="request-1", turn_iteration="2.1"),
        {
            "id": "assistant-1",
            "type": "assistant_message",
            "turn": 2,
            "scope": None,
            "request_ids": ["request-1"],
            "content": "done",
            "interrupted": False,
            "subagent_index": None,
            "turn_iteration": "2.1",
        },
        {
            "type": "model_switch",
            "id": "model-switch-1",
            "model_key": "other-model",
            "sent_at": "2026-07-01T10:02:00+00:00",
        },
        {
            "id": "shell-1",
            "when": "2026-07-01T10:01:00+00:00",
            "role": "shell",
            "content": json.dumps({"command": "false", "exit_code": 1, "output": ""}),
        },
    ]
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n")


def test_migrate_session_log_preserves_order_and_identity(tmp_path):
    path = tmp_path / "session-1.jsonl"
    _write_legacy_log(path)
    original = path.read_bytes()

    assert migrate_session_log(path) == MigrationStats(
        logs_migrated=1,
        shell_records_removed=1,
        model_switch_records_removed=1,
    )

    backup = path.with_name(f"{path.name}.legacy")
    assert backup.read_bytes() == original
    migrated = [json.loads(line) for line in path.read_text().splitlines()]
    assert [line["id"] for line in migrated] == [
        "session-1",
        "iteration-1",
        "request-1",
        "assistant-1",
    ]
    assert migrated[0]["schema_version"] == 2
    assert migrated[1]["turn"] == 2
    assert migrated[1]["iteration"] == 1
    assert "index" not in migrated[1]
    assert migrated[2]["turn"] == 2
    assert migrated[2]["iteration"] == 1
    assert "turn_iteration" not in migrated[2]
    assert "turn_iteration" not in migrated[3]
    assert migrated[3]["request_id"] == "request-1"
    assert migrated[3]["iteration"] == 1
    for line in path.read_text().splitlines():
        decode_event(line, persisted=True)
    assert all(line["type"] != "shell_command" for line in migrated)


def test_assistant_without_matching_request_aborts_without_replacement(tmp_path):
    path = tmp_path / "session-missing-request.jsonl"
    original = (
        json.dumps(
            {
                "type": "assistant_message",
                "id": "assistant-1",
                "turn": 1,
                "scope": None,
                "request_ids": ["missing"],
                "content": "partial",
                "interrupted": True,
                "subagent_index": None,
            }
        )
        + "\n"
    )
    path.write_text(original)

    with pytest.raises(ValueError, match="matching request identity"):
        migrate_session_log(path)

    assert path.read_text() == original
    backup = path.with_name(f"{path.name}.legacy")
    assert backup.read_bytes() == original.encode()


def test_assistant_uses_final_legacy_request_as_direct_producer(tmp_path):
    path = tmp_path / "session-request-chain.jsonl"
    request_one = _legacy_line(event_id="request-1", turn_iteration="1.0")
    request_two = _legacy_line(event_id="request-2", turn_iteration="2.3")
    assistant = {
        "type": "assistant_message",
        "id": "assistant-1",
        "turn": 99,
        "scope": None,
        "request_ids": ["request-1", "request-2"],
        "content": "done",
        "interrupted": False,
        "subagent_index": None,
    }
    path.write_text(
        "\n".join(
            json.dumps(line)
            for line in (
                {
                    "type": "session_started",
                    "id": "session-chain",
                    "schema_version": 1,
                    "sent_at": "2026-07-01T10:00:00+00:00",
                    "model_key": "model",
                },
                request_one,
                request_two,
                assistant,
            )
        )
        + "\n"
    )

    migrate_session_log(path)

    migrated = [json.loads(line) for line in path.read_text().splitlines()]
    converted = migrated[-1]
    assert converted["request_id"] == "request-2"
    assert converted["turn"] == 2
    assert converted["iteration"] == 3
    assert "request_ids" not in converted

    path = tmp_path / "session-2.jsonl"
    event = {
        "type": "session_started",
        "id": "session-2",
        "schema_version": 2,
        "sent_at": "2026-07-01T10:00:00+00:00",
        "model_key": "model",
    }
    path.write_text(json.dumps(event) + "\n")
    original = path.read_bytes()

    assert migrate_session_log(path) == MigrationStats()
    assert path.read_bytes() == original
    assert not path.with_name(f"{path.name}.legacy").exists()


def test_model_switch_removal_uses_discriminator_before_validation(tmp_path):
    path = tmp_path / "session-model-switch.jsonl"
    path.write_text(json.dumps({"type": "model_switch", "id": 123}) + "\n")

    stats = migrate_session_log(path)

    assert stats == MigrationStats(
        logs_migrated=1,
        shell_records_removed=0,
        model_switch_records_removed=1,
    )
    assert path.read_text() == ""
    assert json.loads(path.with_suffix(".jsonl.legacy").read_text())["id"] == 123


def test_model_switch_overlap_reserves_shell_precedence(tmp_path):
    path = tmp_path / "session-shell-overlap.jsonl"
    path.write_text(
        json.dumps(
            {
                "type": "model_switch",
                "id": "shell-overlap",
                "role": "shell",
                "when": "2026-07-01T10:00:00+00:00",
                "content": json.dumps({"command": "true", "exit_code": 0, "output": ""}),
            }
        )
        + "\n"
    )

    stats = migrate_session_log(path)

    assert stats.model_switch_records_removed == 0
    assert stats.shell_records_removed == 1
    assert path.read_text() == ""


@pytest.mark.parametrize(
    "record",
    [
        {"role": "shell", "id": "malformed-role", "content": "not-json"},
        {"type": "shell_command", "id": "malformed-type", "command": 123},
    ],
)
def test_malformed_shell_discriminators_are_removed_before_validation(tmp_path, record):
    path = tmp_path / "malformed-shell.jsonl"
    path.write_text(json.dumps(record) + "\n")

    stats = migrate_session_log(path)

    assert stats == MigrationStats(
        logs_migrated=1,
        shell_records_removed=1,
        model_switch_records_removed=0,
    )
    assert path.read_text() == ""


def test_migrate_session_logs_aggregates_statistics(tmp_path):
    first = tmp_path / "first.jsonl"
    second = tmp_path / "second.jsonl"
    _write_legacy_log(first)
    _write_legacy_log(second)

    assert migrate_session_logs([first, second]) == MigrationStats(
        logs_migrated=2,
        shell_records_removed=2,
        model_switch_records_removed=2,
    )


def test_migrate_session_log_keeps_malformed_lines(tmp_path, caplog):
    path = tmp_path / "session-3.jsonl"
    path.write_text("not json\n" + json.dumps({"type": "unknown", "id": "u1"}) + "\n")

    with caplog.at_level("WARNING"):
        migrate_session_log(path)

    assert path.read_text().splitlines() == [
        "not json",
        json.dumps({"type": "unknown", "id": "u1"}),
    ]
    assert any(record.levelname == "WARNING" for record in caplog.records)


def test_migrate_session_log_failure_leaves_original_unchanged(tmp_path, monkeypatch):
    path = tmp_path / "session-4.jsonl"
    _write_legacy_log(path)
    original = path.read_bytes()

    def fail_replace(source, destination):
        raise OSError("rename failed")

    monkeypatch.setattr(os, "replace", fail_replace)
    try:
        migrate_session_log(path)
    except OSError:
        pass
    else:
        raise AssertionError("migration should propagate an atomic rename failure")

    assert path.read_bytes() == original
    assert not list(tmp_path.glob(f".{path.name}.*.tmp"))


def test_migrate_sessions_cli_backfills_metrics(tmp_path, monkeypatch):
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    log_path = sessions_dir / "session-5.jsonl"
    _write_legacy_log(log_path)
    with log_path.open("a") as output:
        output.write("[]\n")
    db = tmp_path / "archie.db"

    result = CliRunner().invoke(
        main, ["migrate-sessions", "--sessions-dir", str(sessions_dir), "--db", str(db)]
    )

    assert result.exit_code == 0, result.output
    assert (
        f"Migrated 1 session logs; removed 1 shell records and 1 model-switch records; "
        f"rebuilt metrics in {db}"
    ) in result.output
    conn = sqlite3.connect(db)
    try:
        row = conn.execute(
            "SELECT session_id, event_id, turn, iteration FROM metrics_requests"
        ).fetchone()
        sessions = conn.execute("SELECT session_id, status FROM sessions").fetchall()
    finally:
        conn.close()
    assert row == ("session-5", "request-1", 2, 1)
    assert sessions == [("session-5", "stopped")]  # imported once, as a new database


def test_migrate_sessions_cli_db_rebuilds_metrics_and_keeps_sessions(tmp_path):
    from archie_orchestrator.metrics import MetricsIndex

    from tests.session_fakes import migrated_store, sessions_rows

    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    _write_legacy_log(sessions_dir / "session-7.jsonl")
    store = migrated_store(tmp_path)
    store.update("session-7", name="Kept", name_source="user")
    db = tmp_path / "archie.db"
    MetricsIndex(db, sessions_dir).sync()
    before = sessions_rows(db)

    result = CliRunner().invoke(
        main, ["migrate-sessions", "--sessions-dir", str(sessions_dir), "--db", str(db)]
    )

    assert result.exit_code == 0, result.output
    assert f"rebuilt metrics in {db}" in result.output
    assert sessions_rows(db) == before
    assert db.exists() and not list(tmp_path.glob("archie.db.legacy*"))
    conn = sqlite3.connect(db)
    try:
        count = conn.execute("SELECT COUNT(*) FROM metrics_requests").fetchone()[0]
    finally:
        conn.close()
    assert count == 1


def test_migrate_sessions_cli_defaults_to_home_archie_db(tmp_path, monkeypatch):
    monkeypatch.setenv("ARCHIE_HOME_DIR", str(tmp_path))
    (tmp_path / "sessions").mkdir()
    _write_legacy_log(tmp_path / "sessions" / "session-8.jsonl")

    result = CliRunner().invoke(main, ["migrate-sessions"])

    assert result.exit_code == 0, result.output
    assert f"rebuilt metrics in {tmp_path / 'archie.db'}" in result.output
    assert not (tmp_path / "metrics.db").exists()


def test_migrate_sessions_cli_metrics_db_option_is_gone(tmp_path):
    result = CliRunner().invoke(
        main, ["migrate-sessions", "--metrics-db", str(tmp_path / "metrics.db")]
    )
    assert result.exit_code == 2
    assert "No such option" in result.output


def test_migrate_sessions_cli_reports_metrics_failure(tmp_path, monkeypatch):
    from archie_orchestrator.metrics import MetricsIndex

    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    _write_legacy_log(sessions_dir / "session-6.jsonl")

    def boom(self, conn, path, stats):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(MetricsIndex, "_sync_file", boom)
    result = CliRunner().invoke(
        main,
        [
            "migrate-sessions",
            "--sessions-dir",
            str(sessions_dir),
            "--db",
            str(tmp_path / "archie.db"),
        ],
    )

    assert result.exit_code == 1
    assert "disk I/O error" in result.output
    assert "rebuilt metrics" not in result.output
