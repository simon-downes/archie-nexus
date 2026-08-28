from pathlib import Path

from archie_agent.agents import DEFAULT_AGENT, _parse_agent_file, discover_agents


def test_parse_agent_file_extracts_optional_fields_and_body(tmp_path: Path):
    path = tmp_path / "researcher.md"
    path.write_text(
        "---\nname: researcher\ndescription: Finds facts\nprovider: ollama\n"
        "model: local\nskills: [research]\n---\n\nDo research.\n",
        encoding="utf-8",
    )

    entry = _parse_agent_file(path)

    assert entry is not None
    assert entry.name == "researcher"
    assert entry.description == "Finds facts"
    assert entry.provider == "ollama"
    assert entry.model == "local"
    assert entry.skills == ["research"]
    assert entry.body == "Do research."


def test_parse_agent_file_defaults_optional_fields(tmp_path: Path):
    path = tmp_path / "basic.md"
    path.write_text("---\nname: basic\ndescription: Basic\n---\nBody", encoding="utf-8")

    entry = _parse_agent_file(path)

    assert entry is not None
    assert entry.provider is None
    assert entry.model is None
    assert entry.skills == []


def test_discover_agents_missing_directory(monkeypatch, tmp_path: Path):
    monkeypatch.setattr("archie_agent.agents.persona_dir", lambda: tmp_path)

    assert discover_agents() == {}
    assert DEFAULT_AGENT.name == "default"
    assert DEFAULT_AGENT.model is None


def test_discover_agents_skips_malformed_file(monkeypatch, tmp_path: Path, caplog):
    agents = tmp_path / "agents"
    agents.mkdir()
    (agents / "bad.md").write_text("not frontmatter", encoding="utf-8")
    (agents / "good.md").write_text(
        "---\nname: good\ndescription: Good\n---\nBody", encoding="utf-8"
    )
    monkeypatch.setattr("archie_agent.agents.persona_dir", lambda: tmp_path)

    catalog = discover_agents()

    assert set(catalog) == {"good"}
    assert "frontmatter" in caplog.text


def test_discover_agents_duplicate_name_last_sorted_file_wins(
    monkeypatch, tmp_path: Path, caplog
):
    agents = tmp_path / "agents"
    agents.mkdir()
    (agents / "a.md").write_text("---\nname: same\ndescription: A\n---\nA", encoding="utf-8")
    (agents / "b.md").write_text("---\nname: same\ndescription: B\n---\nB", encoding="utf-8")
    monkeypatch.setattr("archie_agent.agents.persona_dir", lambda: tmp_path)

    catalog = discover_agents()

    assert catalog["same"].body == "B"
    assert "Duplicate agent name" in caplog.text
