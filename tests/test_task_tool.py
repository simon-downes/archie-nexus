import json

import pytest
from archie_agent.agents import AgentEntry, create_task_tool
from archie_agent.llm._types import Done, TextDelta, Usage
from archie_agent.llm.fake import FakeLLMClient
from archie_agent.session import Session
from archie_agent.skills import SkillEntry
from archie_shared.events import encode_event
from archie_shared.models import BedrockProvider, CostConfig, ModelEntry


def _model() -> ModelEntry:
    return ModelEntry(
        name="Child model",
        context=10000,
        provider=BedrockProvider(model_id="child-model", region="us-east-1"),
        cost=CostConfig(input=2.0, output=4.0),
    )


def _entry(tmp_path, name="researcher", skills=None) -> AgentEntry:
    path = tmp_path / f"{name}.md"
    path.write_text("", encoding="utf-8")
    return AgentEntry(name, name.title(), None, None, skills or [], f"{name} role", path)


async def _make_tool(tmp_path, monkeypatch, clients, agents=None, skills=None, max_concurrent=3):
    model = _model()
    iterator = iter(clients)
    monkeypatch.setattr("archie_agent.agents.create_llm_client", lambda *_: next(iterator))
    session = Session("parent", model, "session")
    broadcasts: list[str] = []

    async def emit(event):
        broadcasts.append(encode_event(event))

    spec = create_task_tool(
        agent_catalog=agents or {"researcher": _entry(tmp_path)},
        skill_catalog=skills or {},
        session=session,
        model_catalog={"parent": model},
        active_model=model,
        active_model_key="parent",
        region="us-east-1",
        log_path=tmp_path / "session.jsonl",
        emit=emit,
        max_concurrent=max_concurrent,
    )
    return spec, broadcasts


@pytest.mark.asyncio
async def test_single_task_returns_child_text_and_scoped_events(tmp_path, monkeypatch):
    child_llm = FakeLLMClient(
        [
            [
                TextDelta(text="child result"),
                Usage(input_tokens=10, output_tokens=5),
                Done("end_turn"),
            ]
        ]
    )
    spec, broadcasts = await _make_tool(tmp_path, monkeypatch, [child_llm])

    result = await spec.handler(
        tasks=[{"agent": "researcher", "prompt": "Investigate this"}],
        _launch_scope="root-task-id",
        _parent_turn=2,
    )

    assert "child result" in result
    assert child_llm.calls[0]["messages"][0].content[0].text == "Investigate this"
    assert "root-task-id" in "\n".join(broadcasts)
    assert "task" not in [tool["name"] for tool in child_llm.calls[0]["tool_config"]]


@pytest.mark.asyncio
async def test_validation_errors_do_not_start_children(tmp_path, monkeypatch):
    client = FakeLLMClient([])
    spec, _ = await _make_tool(tmp_path, monkeypatch, [client])

    result = await spec.handler(tasks=[{"agent": "researcher", "prompt": ""}], _launch_scope="s")

    assert "requires non-empty" in result
    assert client.calls == []


@pytest.mark.asyncio
async def test_unknown_agent_falls_back_without_sinking_valid_sibling(tmp_path, monkeypatch):
    clients = [
        FakeLLMClient([[TextDelta(text="fallback"), Done("end_turn")]]),
        FakeLLMClient([[TextDelta(text="valid"), Done("end_turn")]]),
    ]
    spec, _ = await _make_tool(tmp_path, monkeypatch, clients)

    result = await spec.handler(
        tasks=[
            {"agent": "missing", "prompt": "nope"},
            {"agent": "researcher", "prompt": "yes"},
        ],
        _launch_scope="s",
    )

    assert "unknown agent 'missing'" in result
    assert "using default agent" in result
    assert "fallback" in result
    assert "valid" in result
    assert len(clients[0].calls) == 1
    assert "task" not in [tool["name"] for tool in clients[0].calls[0]["tool_config"]]


@pytest.mark.asyncio
async def test_child_exception_isolated_from_sibling(tmp_path, monkeypatch):
    bad = FakeLLMClient([])
    good = FakeLLMClient([[TextDelta(text="good"), Done("end_turn")]])
    spec, _ = await _make_tool(tmp_path, monkeypatch, [bad, good])
    result = await spec.handler(
        tasks=[
            {"agent": "researcher", "prompt": "bad"},
            {"agent": "researcher", "prompt": "good"},
        ],
        _launch_scope="s",
    )

    assert "IndexError" in result
    assert "good" in result


@pytest.mark.asyncio
async def test_skill_scoping_only_declared_skills_reach_child(tmp_path, monkeypatch):
    client = FakeLLMClient([[Done("end_turn")]])
    skills = {
        "research": SkillEntry("research", "Research", tmp_path / "research.md"),
        "other": SkillEntry("other", "Other", tmp_path / "other.md"),
    }
    spec, _ = await _make_tool(
        tmp_path,
        monkeypatch,
        [client],
        agents={"researcher": _entry(tmp_path, skills=["research"])},
        skills=skills,
    )

    await spec.handler(tasks=[{"agent": "researcher", "prompt": "work"}], _launch_scope="s")

    names = [tool["name"] for tool in client.calls[0]["tool_config"]]
    assert "skill" in names
    assert "research: Research" in client.calls[0]["system"].static_system.text
    assert "other: Other" not in client.calls[0]["system"].static_system.text


@pytest.mark.asyncio
async def test_out_of_order_completion_preserves_input_order(tmp_path, monkeypatch):
    first = FakeLLMClient([[TextDelta(text="first"), Done("end_turn")]], delay=0.05)
    second = FakeLLMClient([[TextDelta(text="second"), Done("end_turn")]], delay=0.0)
    spec, _ = await _make_tool(tmp_path, monkeypatch, [first, second])

    result = await spec.handler(
        tasks=[
            {"agent": "researcher", "prompt": "first"},
            {"agent": "researcher", "prompt": "second"},
        ],
        _launch_scope="s",
    )

    assert result.index("[0] researcher: first") < result.index("[1] researcher: second")


@pytest.mark.asyncio
async def test_max_concurrent_one_serializes_children(tmp_path, monkeypatch):
    active = 0
    maximum = 0

    class TrackingClient(FakeLLMClient):
        def stream(self, *args, **kwargs):
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            try:
                yield from super().stream(*args, **kwargs)
            finally:
                active -= 1

    clients = [
        TrackingClient([[TextDelta(text="one"), Done("end_turn")]]),
        TrackingClient([[TextDelta(text="two"), Done("end_turn")]]),
    ]
    spec, _ = await _make_tool(tmp_path, monkeypatch, clients, max_concurrent=1)

    result = await spec.handler(
        tasks=[
            {"agent": "researcher", "prompt": "one"},
            {"agent": "researcher", "prompt": "two"},
        ],
        _launch_scope="s",
    )

    assert "one" in result and "two" in result
    assert maximum == 1


@pytest.mark.asyncio
async def test_malformed_child_item_emits_scoped_terminal_event(tmp_path, monkeypatch):
    """A malformed task item still receives the child terminal guarantee."""
    client = FakeLLMClient([])
    spec, broadcasts = await _make_tool(tmp_path, monkeypatch, [client])

    result = await spec.handler(tasks=[["not a task mapping"]], _launch_scope="scope-1")

    assert "requires" in result
    events = [json.loads(raw) for raw in broadcasts]
    terminal = [event for event in events if event["type"] == "turn_error"]
    assert len(terminal) == 1
    assert terminal[0]["scope"] == "scope-1"
    assert terminal[0]["subagent_index"] == 0
    assert client.calls == []
