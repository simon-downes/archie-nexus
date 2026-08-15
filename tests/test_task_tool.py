import pytest
from archie_agent.agents import AgentEntry, create_task_tool
from archie_agent.llm._types import Done, TextDelta, Usage
from archie_agent.llm.fake import FakeLLMClient
from archie_agent.session import Session
from archie_shared.models import BedrockProvider, CostConfig, ModelEntry


@pytest.mark.asyncio
async def test_single_task_returns_child_text_and_scoped_events(tmp_path, monkeypatch):
    model = ModelEntry(
        name="Child model",
        context=10000,
        provider=BedrockProvider(model_id="child-model", region="us-east-1"),
        cost=CostConfig(input=2.0, output=4.0),
    )
    child_llm = FakeLLMClient(
        [[TextDelta(text="child result"), Usage(input_tokens=10, output_tokens=5), Done("end_turn")]]
    )
    monkeypatch.setattr("archie_agent.agents.create_llm_client", lambda *_: child_llm)

    agent_path = tmp_path / "researcher.md"
    agent_path.write_text("", encoding="utf-8")
    agent = AgentEntry("researcher", "Research", None, None, [], "Research role", agent_path)
    session = Session("parent", model, "session")
    broadcasts: list[str] = []

    async def broadcast(data: str):
        broadcasts.append(data)

    spec = create_task_tool(
        agent_catalog={"researcher": agent},
        skill_catalog={},
        session=session,
        model_catalog={"parent": model},
        active_model=model,
        active_model_key="parent",
        region="us-east-1",
        log_path=tmp_path / "session.jsonl",
        broadcast=broadcast,
    )

    result = await spec.handler(
        tasks=[{"agent": "researcher", "prompt": "Investigate this"}],
        _launch_scope="root-task-id",
        _parent_turn=2,
    )

    assert "child result" in result
    assert child_llm.calls[0]["messages"][0].content[0].text == "Investigate this"
    assert "root-task-id" in "\n".join(broadcasts)
    assert "task" not in [tool["name"] for tool in child_llm.calls[0]["tool_config"]]
