"""Tests for the provider-neutral tool registry and Bedrock shaping."""

import pytest
from archie_agent.llm.bedrock import _shape_tool_config_for_bedrock
from archie_agent.tools import ToolRegistry, ToolSpec


async def _noop_handler(**kwargs):
    return "ok"


@pytest.fixture
def registry():
    reg = ToolRegistry()
    reg.register(
        ToolSpec(
            name="exec",
            description="Execute Python code.",
            schema={
                "type": "object",
                "properties": {"source": {"type": "string"}},
                "required": ["source"],
            },
            handler=_noop_handler,
        )
    )
    return reg


def test_registry_register_and_get(registry):
    spec = registry.get("exec")
    assert spec is not None
    assert spec.name == "exec"
    assert spec.description == "Execute Python code."
    assert spec.handler is _noop_handler


def test_registry_get_missing(registry):
    assert registry.get("nonexistent") is None


def test_to_tool_config_neutral_format(registry):
    config = registry.to_tool_config()
    assert len(config) == 1
    item = config[0]
    assert item["name"] == "exec"
    assert item["description"] == "Execute Python code."
    assert item["input_schema"] == {
        "type": "object",
        "properties": {"source": {"type": "string"}},
        "required": ["source"],
    }
    # Must NOT have Bedrock-specific keys
    assert "toolSpec" not in item
    assert "inputSchema" not in item


def test_bedrock_shaping():
    neutral = [
        {
            "name": "exec",
            "description": "Execute Python code.",
            "input_schema": {
                "type": "object",
                "properties": {"source": {"type": "string"}},
                "required": ["source"],
            },
        }
    ]
    shaped = _shape_tool_config_for_bedrock(neutral)
    assert len(shaped) == 1
    tool_spec = shaped[0]["toolSpec"]
    assert tool_spec["name"] == "exec"
    assert tool_spec["description"] == "Execute Python code."
    assert tool_spec["inputSchema"]["json"] == {
        "type": "object",
        "properties": {"source": {"type": "string"}},
        "required": ["source"],
    }


def test_multiple_tools():
    reg = ToolRegistry()
    reg.register(ToolSpec(name="a", description="Tool A", schema={}, handler=_noop_handler))
    reg.register(ToolSpec(name="b", description="Tool B", schema={}, handler=_noop_handler))
    config = reg.to_tool_config()
    assert len(config) == 2
    names = {item["name"] for item in config}
    assert names == {"a", "b"}


def test_bedrock_shaping_multiple():
    neutral = [
        {"name": "a", "description": "A", "input_schema": {"type": "object"}},
        {"name": "b", "description": "B", "input_schema": {"type": "string"}},
    ]
    shaped = _shape_tool_config_for_bedrock(neutral)
    assert len(shaped) == 2
    assert shaped[0]["toolSpec"]["name"] == "a"
    assert shaped[1]["toolSpec"]["name"] == "b"
