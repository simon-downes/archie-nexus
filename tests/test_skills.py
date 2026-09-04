"""Tests for skills discovery and tool handler."""

from pathlib import Path

import pytest
from archie_agent.skills import (
    SkillEntry,
    _extract_body,
    _list_reference_files,
    _parse_skill_file,
    _scan_directory,
    create_skill_tool,
    discover_skills,
)

# ---------------------------------------------------------------------------
# Fixtures — skill file creation helpers
# ---------------------------------------------------------------------------

VALID_SKILL = """\
---
name: test-skill
description: A test skill for unit testing.
---
# Test Skill

This is the body of the test skill.
"""

VALID_SKILL_2 = """\
---
name: another-skill
description: Another skill for testing.
---
Body of another skill.
"""


def _write_skill(base_dir: Path, slug: str, content: str) -> Path:
    """Write a SKILL.md file under base_dir/slug/."""
    skill_dir = base_dir / slug
    skill_dir.mkdir(parents=True, exist_ok=True)
    skill_file = skill_dir / "SKILL.md"
    skill_file.write_text(content, encoding="utf-8")
    return skill_file


# ---------------------------------------------------------------------------
# Discovery tests
# ---------------------------------------------------------------------------


class TestParseSkillFile:
    """Tests for _parse_skill_file."""

    def test_valid_file(self, tmp_path):
        path = _write_skill(tmp_path, "my-skill", VALID_SKILL)
        entry = _parse_skill_file(path)
        assert entry is not None
        assert entry.name == "test-skill"
        assert entry.description == "A test skill for unit testing."
        assert entry.path == path

    def test_missing_frontmatter_start(self, tmp_path):
        path = _write_skill(tmp_path, "bad", "no frontmatter here")
        assert _parse_skill_file(path) is None

    def test_missing_closing_delimiter(self, tmp_path):
        content = "---\nname: x\ndescription: y\n"
        path = _write_skill(tmp_path, "bad", content)
        assert _parse_skill_file(path) is None

    def test_invalid_yaml(self, tmp_path):
        content = "---\n: invalid: yaml: [unclosed\n---\nbody"
        path = _write_skill(tmp_path, "bad", content)
        assert _parse_skill_file(path) is None

    def test_frontmatter_not_a_dict(self, tmp_path):
        content = "---\n- list\n- item\n---\nbody"
        path = _write_skill(tmp_path, "bad", content)
        assert _parse_skill_file(path) is None

    def test_missing_name(self, tmp_path):
        content = "---\ndescription: has desc\n---\nbody"
        path = _write_skill(tmp_path, "bad", content)
        assert _parse_skill_file(path) is None

    def test_missing_description(self, tmp_path):
        content = "---\nname: has-name\n---\nbody"
        path = _write_skill(tmp_path, "bad", content)
        assert _parse_skill_file(path) is None

    def test_extra_fields_ignored(self, tmp_path):
        content = "---\nname: x\ndescription: y\nauthor: nobody\n---\nbody"
        path = _write_skill(tmp_path, "x", content)
        entry = _parse_skill_file(path)
        assert entry is not None
        assert entry.name == "x"

    def test_nonexistent_file(self, tmp_path):
        assert _parse_skill_file(tmp_path / "nonexistent" / "SKILL.md") is None


class TestScanDirectory:
    """Tests for _scan_directory."""

    def test_scans_skills(self, tmp_path):
        _write_skill(tmp_path, "skill-a", VALID_SKILL)
        _write_skill(tmp_path, "skill-b", VALID_SKILL_2)
        catalog: dict[str, SkillEntry] = {}
        _scan_directory(tmp_path, catalog)
        assert "test-skill" in catalog
        assert "another-skill" in catalog

    def test_missing_directory(self, tmp_path):
        catalog: dict[str, SkillEntry] = {}
        _scan_directory(tmp_path / "nonexistent", catalog)
        assert catalog == {}

    def test_skips_non_directories(self, tmp_path):
        # File at top level (not a skill directory)
        (tmp_path / "stray-file.txt").write_text("not a skill")
        catalog: dict[str, SkillEntry] = {}
        _scan_directory(tmp_path, catalog)
        assert catalog == {}

    def test_skips_dir_without_skill_md(self, tmp_path):
        (tmp_path / "empty-dir").mkdir()
        catalog: dict[str, SkillEntry] = {}
        _scan_directory(tmp_path, catalog)
        assert catalog == {}

    def test_skips_malformed_skill(self, tmp_path):
        _write_skill(tmp_path, "good", VALID_SKILL)
        _write_skill(tmp_path, "bad", "no frontmatter")
        catalog: dict[str, SkillEntry] = {}
        _scan_directory(tmp_path, catalog)
        assert len(catalog) == 1
        assert "test-skill" in catalog


class TestDiscoverSkills:
    """Tests for discover_skills — integration with agents + persona scanning."""

    def test_discovers_from_agents_dir(self, tmp_path, monkeypatch):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setenv("ARCHIE_PERSONA_DIR", str(tmp_path / "persona"))
        agents_dir = tmp_path / ".agents" / "skills"
        _write_skill(agents_dir, "shared-skill", VALID_SKILL)

        catalog = discover_skills()
        assert "test-skill" in catalog

    def test_discovers_from_persona_dir(self, tmp_path, monkeypatch):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setenv("ARCHIE_PERSONA_DIR", str(tmp_path / "persona"))
        persona_skills_dir = tmp_path / "persona" / "skills"
        _write_skill(persona_skills_dir, "persona-skill", VALID_SKILL)

        catalog = discover_skills()
        assert "test-skill" in catalog

    def test_persona_overrides_agents(self, tmp_path, monkeypatch):
        """persona skills override ~/.agents skills with same name."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setenv("ARCHIE_PERSONA_DIR", str(tmp_path / "persona"))

        agents_dir = tmp_path / ".agents" / "skills"
        persona_skills_dir = tmp_path / "persona" / "skills"

        # Both have a skill named "test-skill" but from different paths
        _write_skill(agents_dir, "shared", VALID_SKILL)
        persona_path = _write_skill(persona_skills_dir, "persona", VALID_SKILL)

        catalog = discover_skills()
        assert "test-skill" in catalog
        # The persona one should win
        assert catalog["test-skill"].path == persona_path

    def test_empty_when_no_dirs(self, tmp_path, monkeypatch):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setenv("ARCHIE_PERSONA_DIR", str(tmp_path / "persona"))
        catalog = discover_skills()
        assert catalog == {}

    def test_merges_unique_skills(self, tmp_path, monkeypatch):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setenv("ARCHIE_PERSONA_DIR", str(tmp_path / "persona"))

        agents_dir = tmp_path / ".agents" / "skills"
        persona_skills_dir = tmp_path / "persona" / "skills"

        _write_skill(agents_dir, "shared", VALID_SKILL)
        _write_skill(persona_skills_dir, "specific", VALID_SKILL_2)

        catalog = discover_skills()
        assert "test-skill" in catalog
        assert "another-skill" in catalog


# ---------------------------------------------------------------------------
# Body extraction
# ---------------------------------------------------------------------------


class TestExtractBody:
    """Tests for _extract_body."""

    def test_extracts_body(self, tmp_path):
        path = _write_skill(tmp_path, "s", VALID_SKILL)
        body = _extract_body(path)
        assert "This is the body of the test skill." in body
        assert "---" not in body

    def test_strips_whitespace(self, tmp_path):
        content = "---\nname: x\ndescription: y\n---\n\n  body  \n\n"
        path = _write_skill(tmp_path, "s", content)
        body = _extract_body(path)
        assert body == "body"

    def test_returns_empty_on_missing_file(self, tmp_path):
        assert _extract_body(tmp_path / "nonexistent") == ""

    def test_returns_empty_without_frontmatter(self, tmp_path):
        path = _write_skill(tmp_path, "s", "no frontmatter")
        assert _extract_body(path) == ""


# ---------------------------------------------------------------------------
# Reference file listing
# ---------------------------------------------------------------------------


class TestListReferenceFiles:
    """Tests for _list_reference_files."""

    def test_lists_files_excluding_skill_md(self, tmp_path):
        skill_dir = tmp_path / "my-skill"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text("ignored")
        (skill_dir / "references").mkdir()
        (skill_dir / "references" / "arch.md").write_text("arch doc")
        (skill_dir / "references" / "patterns.md").write_text("patterns")

        files = _list_reference_files(skill_dir)
        assert "references/arch.md" in files
        assert "references/patterns.md" in files
        assert "SKILL.md" not in files

    def test_empty_dir(self, tmp_path):
        skill_dir = tmp_path / "empty"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text("x")
        assert _list_reference_files(skill_dir) == []

    def test_nonexistent_dir(self, tmp_path):
        assert _list_reference_files(tmp_path / "nope") == []


# ---------------------------------------------------------------------------
# Skill tool handler tests
# ---------------------------------------------------------------------------


class TestSkillTool:
    """Tests for the current tool-result skill contract."""

    def _make_catalog(self, tmp_path) -> dict[str, SkillEntry]:
        path = _write_skill(tmp_path, "my-skill", VALID_SKILL)
        ref_dir = tmp_path / "my-skill" / "references"
        ref_dir.mkdir()
        (ref_dir / "guide.md").write_text("Reference content here.")
        return {"test-skill": SkillEntry("test-skill", "A test skill.", path)}

    @pytest.mark.asyncio
    async def test_load_skill_returns_exact_wrapped_body_and_tracks_key(self, tmp_path):
        catalog = self._make_catalog(tmp_path)
        loaded: set[tuple[str, str]] = set()
        spec = create_skill_tool(catalog, loaded)

        result = await spec.handler(name="test-skill")
        assert result.startswith(
            "Skill 'test-skill' loaded. Follow the content inside the `<skill>` tag"
        )
        assert '<skill name="test-skill">' in result
        assert "This is the body of the test skill." in result
        assert ("test-skill", "__body__") in loaded
        assert "system prompt" not in result

    @pytest.mark.asyncio
    async def test_load_lists_reference_files(self, tmp_path):
        spec = create_skill_tool(self._make_catalog(tmp_path), set())
        result = await spec.handler(name="test-skill")
        assert "Reference files available:" in result
        assert "- references/guide.md" in result

    @pytest.mark.asyncio
    async def test_already_loaded_returns_manifest(self, tmp_path):
        loaded = {("test-skill", "__body__"), ("test-skill", "references/guide.md")}
        spec = create_skill_tool(self._make_catalog(tmp_path), loaded)
        result = await spec.handler(name="test-skill", references=["references/guide.md"])
        assert "already available in earlier tool results" in result
        assert "- skill body" in result
        assert "- references/guide.md" in result
        assert "<skill" not in result

    @pytest.mark.asyncio
    async def test_reference_load_is_body_first_and_tagged(self, tmp_path):
        spec = create_skill_tool(self._make_catalog(tmp_path), set())
        result = await spec.handler(name="test-skill", references=["references/guide.md"])
        assert result.index('<skill name="test-skill">') < result.index(
            '<reference name="test-skill" file="references/guide.md">'
        )
        assert "Reference content here." in result

    @pytest.mark.asyncio
    async def test_reference_errors_are_atomic_and_aggregated(self, tmp_path):
        loaded: set[tuple[str, str]] = set()
        spec = create_skill_tool(self._make_catalog(tmp_path), loaded)
        result = await spec.handler(name="test-skill", references=["nope.md", "missing.md"])
        assert "nope.md" in result and "missing.md" in result
        assert loaded == set()

    @pytest.mark.asyncio
    async def test_reference_validation_and_binary_errors(self, tmp_path):
        catalog = self._make_catalog(tmp_path)
        (tmp_path / "my-skill" / "binary.bin").write_bytes(b"\x00\x01")
        spec = create_skill_tool(catalog, set())
        result = await spec.handler(
            name="test-skill", references=["../../etc/passwd", ""]
        )
        assert "../../etc/passwd" in result
        assert "invalid reference" in result
        binary_result = await spec.handler(name="test-skill", references=["binary.bin"])
        assert "binary.bin" in binary_result
        assert "binary" in binary_result.lower()

    @pytest.mark.asyncio
    async def test_duplicate_references_are_normalized(self, tmp_path):
        loaded: set[tuple[str, str]] = set()
        spec = create_skill_tool(self._make_catalog(tmp_path), loaded)
        result = await spec.handler(
            name="test-skill", references=["references/guide.md", "references/guide.md"]
        )
        assert result.count('<reference name="test-skill"') == 1
        assert ("test-skill", "references/guide.md") in loaded

    @pytest.mark.asyncio
    async def test_unknown_and_missing_name(self, tmp_path):
        spec = create_skill_tool(self._make_catalog(tmp_path), set())
        assert "available" in (await spec.handler(name="nonexistent")).lower()
        assert "missing" in (await spec.handler(name="")).lower()

    @pytest.mark.asyncio
    async def test_malformed_skill_does_not_mutate_state(self, tmp_path):
        path = _write_skill(tmp_path, "bad", "---\nname: [broken\n---\nbody")
        loaded: set[tuple[str, str]] = set()
        spec = create_skill_tool({"bad": SkillEntry("bad", "Bad", path)}, loaded)
        result = await spec.handler(name="bad")
        assert "SKILL.md" in result or "malformed" in result
        assert not loaded

    @pytest.mark.asyncio
    async def test_tool_spec_schema(self, tmp_path):
        spec = create_skill_tool(self._make_catalog(tmp_path), set())
        assert spec.name == "skill"
        assert spec.schema["required"] == ["name"]
        assert "references" in spec.schema["properties"]
        assert "file" not in spec.schema["properties"]
        assert "check the listed reference files" in spec.description
        assert "before applying guidance" in spec.schema["properties"]["references"]["description"]
