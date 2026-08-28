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
    """Tests for create_skill_tool."""

    def _make_catalog(self, tmp_path) -> dict[str, SkillEntry]:
        path = _write_skill(tmp_path, "my-skill", VALID_SKILL)
        # Add a reference file
        ref_dir = tmp_path / "my-skill" / "references"
        ref_dir.mkdir()
        (ref_dir / "guide.md").write_text("Reference content here.")
        return {"test-skill": SkillEntry(name="test-skill", description="A test skill.", path=path)}

    @pytest.mark.asyncio
    async def test_load_skill(self, tmp_path):
        catalog = self._make_catalog(tmp_path)
        loaded: list[tuple[str, str]] = []
        spec = create_skill_tool(catalog, loaded)

        result = await spec.handler(name="test-skill")
        assert "Loaded skill 'test-skill'" in result
        assert "(3 lines)" in result
        assert len(loaded) == 1
        assert loaded[0][0] == "test-skill"
        assert "This is the body of the test skill." in loaded[0][1]

    @pytest.mark.asyncio
    async def test_load_lists_reference_files(self, tmp_path):
        catalog = self._make_catalog(tmp_path)
        loaded: list[tuple[str, str]] = []
        spec = create_skill_tool(catalog, loaded)

        result = await spec.handler(name="test-skill")
        assert "references/guide.md" in result

    @pytest.mark.asyncio
    async def test_already_loaded(self, tmp_path):
        catalog = self._make_catalog(tmp_path)
        loaded: list[tuple[str, str]] = [("test-skill", "body")]
        spec = create_skill_tool(catalog, loaded)

        result = await spec.handler(name="test-skill")
        assert "already loaded" in result
        assert len(loaded) == 1  # Not appended again

    @pytest.mark.asyncio
    async def test_unknown_skill(self, tmp_path):
        catalog = self._make_catalog(tmp_path)
        loaded: list[tuple[str, str]] = []
        spec = create_skill_tool(catalog, loaded)

        result = await spec.handler(name="nonexistent")
        assert "unknown skill" in result.lower()
        assert "test-skill" in result  # Shows available names

    @pytest.mark.asyncio
    async def test_missing_name(self, tmp_path):
        catalog = self._make_catalog(tmp_path)
        loaded: list[tuple[str, str]] = []
        spec = create_skill_tool(catalog, loaded)

        result = await spec.handler(name="")
        assert "missing" in result.lower()

    @pytest.mark.asyncio
    async def test_read_reference_file(self, tmp_path):
        catalog = self._make_catalog(tmp_path)
        loaded: list[tuple[str, str]] = []
        spec = create_skill_tool(catalog, loaded)

        result = await spec.handler(name="test-skill", file="references/guide.md")
        assert result == "Reference content here."

    @pytest.mark.asyncio
    async def test_read_nonexistent_file(self, tmp_path):
        catalog = self._make_catalog(tmp_path)
        loaded: list[tuple[str, str]] = []
        spec = create_skill_tool(catalog, loaded)

        result = await spec.handler(name="test-skill", file="nope.md")
        assert "not found" in result.lower()

    @pytest.mark.asyncio
    async def test_read_path_traversal_rejected(self, tmp_path):
        catalog = self._make_catalog(tmp_path)
        loaded: list[tuple[str, str]] = []
        spec = create_skill_tool(catalog, loaded)

        result = await spec.handler(name="test-skill", file="../../etc/passwd")
        assert "outside" in result.lower()

    @pytest.mark.asyncio
    async def test_read_binary_file(self, tmp_path):
        catalog = self._make_catalog(tmp_path)
        # Write a binary file in the skill dir
        binary_path = tmp_path / "my-skill" / "binary.bin"
        binary_path.write_bytes(b"\x00\x01\x02binary")
        loaded: list[tuple[str, str]] = []
        spec = create_skill_tool(catalog, loaded)

        result = await spec.handler(name="test-skill", file="binary.bin")
        assert "binary" in result.lower()

    @pytest.mark.asyncio
    async def test_tool_spec_schema(self, tmp_path):
        catalog = self._make_catalog(tmp_path)
        loaded: list[tuple[str, str]] = []
        spec = create_skill_tool(catalog, loaded)

        assert spec.name == "skill"
        assert "name" in spec.schema["properties"]
        assert "file" in spec.schema["properties"]
        assert spec.schema["required"] == ["name"]
