"""Tests for one-shot session-log migration and host CLI integration."""

import json
import os
import sqlite3
from pathlib import Path

from archie_cli.cli import main
from archie_shared.events import ShellCommand, decode_event
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
        shell_records_removed=0,
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
        "shell-1",
    ]
    assert migrated[0]["schema_version"] == 2
    assert migrated[1]["turn"] == 2
    assert migrated[1]["iteration"] == 1
    assert "index" not in migrated[1]
    assert migrated[2]["turn"] == 2
    assert migrated[2]["iteration"] == 1
    assert "turn_iteration" not in migrated[2]
    assert "turn_iteration" not in migrated[3]
    assert decode_event(path.read_text().splitlines()[-1], persisted=True) == ShellCommand(
        id="shell-1",
        command="false",
        exit_code=1,
        output="",
    )


def test_migrate_session_log_skips_schema_v2(tmp_path):
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
    assert stats.shell_records_removed == 0
    assert json.loads(path.read_text())["type"] == "shell_command"


def test_migrate_session_logs_aggregates_statistics(tmp_path):
    first = tmp_path / "first.jsonl"
    second = tmp_path / "second.jsonl"
    _write_legacy_log(first)
    _write_legacy_log(second)

    assert migrate_session_logs([first, second]) == MigrationStats(
        logs_migrated=2,
        shell_records_removed=0,
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
    metrics_path = tmp_path / "metrics.db"

    result = CliRunner().invoke(
        main,
        [
            "migrate-sessions",
            "--sessions-dir",
            str(sessions_dir),
            "--metrics-db",
            str(metrics_path),
        ],
    )

    assert result.exit_code == 0, result.output
    assert (
        f"Migrated 1 session logs; removed 0 shell records and 1 model-switch records; "
        f"rebuilt metrics at {metrics_path}"
    ) in result.output
    conn = sqlite3.connect(metrics_path)
    try:
        row = conn.execute("SELECT session_id, event_id, turn, iteration FROM requests").fetchone()
    finally:
        conn.close()
    assert row == ("session-5", "request-1", 2, 1)
