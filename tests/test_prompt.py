"""Tests for prompt.py — dynamic system prompt builder."""

from archie_agent.prompt import _IDENTITY, _build_environment, _build_tools, build_system_prompt


def test_identity_section_content():
    """Identity section contains key personality traits."""
    assert "Archie" in _IDENTITY
    assert "concise" in _IDENTITY
    assert "tools proactively" in _IDENTITY
    assert "Do not re-read to verify" in _IDENTITY


def test_build_environment_contains_model_and_workspace():
    """Environment section includes model name and workspace dir."""
    env = _build_environment("test-model-v1", "/workspace")
    assert "test-model-v1" in env
    assert "/workspace" in env
    assert "Docker" in env
    assert "Exec interpreter" in env


def test_build_environment_custom_workspace():
    """Environment section respects custom workspace dir."""
    env = _build_environment("model", "/custom/path")
    assert "/custom/path" in env


def test_build_tools_contains_strategy():
    """Tools section includes strategy patterns."""
    tools = _build_tools()
    assert "async def main()" in tools
    assert "grep" in tools
    assert "asyncio.gather" in tools
    assert "shell" in tools


def test_build_tools_contains_guidelines():
    """Tools section includes aggregated guidelines from exec tools."""
    tools = _build_tools()
    assert "### Guidelines" in tools
    assert "read" in tools
    assert "write" in tools
    assert "edit" in tools
    assert "glob" in tools
    assert "shell" in tools


def test_build_system_prompt_assembles_all_sections():
    """build_system_prompt returns a single string with all sections."""
    prompt = build_system_prompt("claude-sonnet-4-20250514")

    # Identity
    assert "Archie" in prompt
    # Environment
    assert "claude-sonnet-4-20250514" in prompt
    assert "Docker" in prompt
    # Tools
    assert "async def main()" in prompt
    assert "### Guidelines" in prompt


def test_build_system_prompt_is_single_string():
    """Prompt is a non-empty string (suitable for Bedrock system field)."""
    prompt = build_system_prompt("test-model")
    assert isinstance(prompt, str)
    assert len(prompt) > 100


def test_build_system_prompt_accepts_model_name():
    """Model name parameter is passed through to environment."""
    prompt = build_system_prompt("my-custom-model-7b")
    assert "my-custom-model-7b" in prompt
