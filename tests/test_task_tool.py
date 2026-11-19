import json

import pytest
from archie_agent.agents import AgentEntry, create_task_tool
from archie_agent.llm._types import Done, TextDelta, Usage
from archie_agent.llm.fake import FakeLLMClient
from archie_agent.session import Session
from archie_agent.skills import SkillEntry
from archie_shared.events import encode_event
from archie_shared.models import BedrockProvider, CostConfig, ModelEntry, Rate


def _model() -> ModelEntry:
    return ModelEntry(
        name="Child model",
        context=10000,
        provider=BedrockProvider(model_id="child-model", region="us-east-1"),
        cost=CostConfig(rates=[Rate(input=2.0, output=4.0)]),
    )


def _entry(tmp_path, name="researcher", skills=None) -> AgentEntry:
    path = tmp_path / f"{name}.md"
    path.write_text("", encoding="utf-8")
    return AgentEntry(name, name.title(), None, None, skills or [], f"{name} role", path)


async def _make_tool(
    tmp_path,
    monkeypatch,
    clients,
    agents=None,
    skills=None,
    max_concurrent=3,
    tiers=None,
    catalog=None,
    used_models=None,
):
    model = _model()
    iterator = iter(clients)

    def fake_client(child_model, *_):
        if used_models is not None:
            used_models.append(child_model.name)
        return next(iterator)

    monkeypatch.setattr("archie_agent.agents.create_llm_client", fake_client)
    session = Session("parent", model, "session")
    broadcasts: list[str] = []

    async def emit(event):
        broadcasts.append(encode_event(event))

    spec = create_task_tool(
        agent_catalog=agents or {"researcher": _entry(tmp_path)},
        skill_catalog=skills or {},
        session=session,
        model_catalog={"parent": model, **(catalog or {})},
        active_model=model,
        active_model_key="parent",
        region="us-east-1",
        log_path=tmp_path / "session.jsonl",
        emit=emit,
        max_concurrent=max_concurrent,
        tiers=tiers,
    )
    return spec, broadcasts


def _named(name: str) -> ModelEntry:
    return ModelEntry(
        name=name,
        context=10000,
        provider=BedrockProvider(model_id=name, region="us-east-1"),
        cost=CostConfig(rates=[Rate(input=1.0, output=1.0)]),
    )


TIERS = {"light": "small", "standard": "mid", "deep": "big"}
TIER_CATALOG = {key: _named(key) for key in TIERS.values()}


def _done(text="done"):
    return FakeLLMClient(
        [[TextDelta(text=text), Usage(input_tokens=1, output_tokens=1), Done("end_turn")]]
    )


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
    assert "question" not in [tool["name"] for tool in child_llm.calls[0]["tool_config"]]


@pytest.mark.asyncio
async def test_validation_errors_do_not_start_children(tmp_path, monkeypatch):
    client = FakeLLMClient([])
    spec, _ = await _make_tool(tmp_path, monkeypatch, [client])

    result = await spec.handler(tasks=[{"agent": "researcher", "prompt": ""}], _launch_scope="s")

    assert "requires non-empty" in result
    assert client.calls == []


@pytest.mark.asyncio
async def test_default_agent_is_accepted_without_warning(tmp_path, monkeypatch):
    clients = [FakeLLMClient([[TextDelta(text="done"), Done("end_turn")]])]
    spec, _ = await _make_tool(tmp_path, monkeypatch, clients)

    result = await spec.handler(tasks=[{"agent": "default", "prompt": "go"}], _launch_scope="s")

    assert result == "[0] default: done"


@pytest.mark.asyncio
async def test_description_lists_agents_and_concurrency(tmp_path, monkeypatch):
    spec, _ = await _make_tool(tmp_path, monkeypatch, [], max_concurrent=4)

    assert "up to 4 at a time" in spec.description
    assert "cannot delegate further" in spec.description
    assert "no conversation history" in spec.description
    assert "default — General-purpose subagent" in spec.description
    assert "researcher — Researcher" in spec.description


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
    assert "(available: default, researcher)" in result
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


# --- Tiers (095) ---


@pytest.mark.asyncio
async def test_schema_requires_a_tier_from_the_enum(tmp_path, monkeypatch):
    for tiers in (None, TIERS):
        spec, _ = await _make_tool(tmp_path, monkeypatch, [], tiers=tiers, catalog=TIER_CATALOG)
        item = spec.schema["properties"]["tasks"]["items"]
        assert item["required"] == ["agent", "prompt", "tier"]
        assert item["properties"]["tier"]["enum"] == ["light", "standard", "deep"]


@pytest.mark.asyncio
async def test_description_explains_each_tier(tmp_path, monkeypatch):
    spec, _ = await _make_tool(tmp_path, monkeypatch, [])

    assert "choose it by the work, not by your own model" in spec.description
    assert "light — mechanical work" in spec.description
    assert "standard — a well-scoped change" in spec.description
    assert "deep — reviews, design judgement" in spec.description


@pytest.mark.asyncio
async def test_each_task_runs_on_its_tier_model(tmp_path, monkeypatch):
    used: list[str] = []
    spec, broadcasts = await _make_tool(
        tmp_path,
        monkeypatch,
        [_done("a"), _done("b")],
        tiers=TIERS,
        catalog=TIER_CATALOG,
        used_models=used,
    )

    result = await spec.handler(
        tasks=[
            {"agent": "researcher", "prompt": "review", "tier": "deep"},
            {"agent": "researcher", "prompt": "grep", "tier": "light"},
        ],
        _launch_scope="s",
    )

    assert result == "[0] researcher: a\n[1] researcher: b"
    assert sorted(used) == ["big", "small"]
    requests = [json.loads(raw) for raw in broadcasts]
    keys = {e["subagent_index"]: e["model_key"] for e in requests if e["type"] == "llm_request"}
    assert keys == {0: "big", 1: "small"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("task_tier", "problem"),
    [("huge", "unknown tier 'huge'"), (None, "missing tier"), (3, "unknown tier 3")],
)
async def test_bad_tier_falls_back_to_standard_with_a_warning(
    tmp_path, monkeypatch, task_tier, problem
):
    used: list[str] = []
    spec, _ = await _make_tool(
        tmp_path, monkeypatch, [_done()], tiers=TIERS, catalog=TIER_CATALOG, used_models=used
    )
    task = {"agent": "researcher", "prompt": "go"}
    if task_tier is not None:
        task["tier"] = task_tier

    result = await spec.handler(tasks=[task], _launch_scope="s")

    assert result.startswith(
        f"[0] researcher: Warning: {problem} (use one of light, standard, deep)"
    )
    assert result.endswith("using standard. done")
    assert used == ["mid"]


@pytest.mark.asyncio
async def test_unknown_agent_warning_comes_before_the_tier_warning(tmp_path, monkeypatch):
    spec, _ = await _make_tool(tmp_path, monkeypatch, [_done()], tiers=TIERS, catalog=TIER_CATALOG)

    result = await spec.handler(
        tasks=[{"agent": "missing", "prompt": "go", "tier": "huge"}], _launch_scope="s"
    )

    assert result.index("unknown agent 'missing'") < result.index("unknown tier 'huge'")


@pytest.mark.asyncio
async def test_tier_model_missing_from_catalogue_fails_only_that_task(tmp_path, monkeypatch):
    tiers = {**TIERS, "deep": "gone"}
    spec, _ = await _make_tool(
        tmp_path, monkeypatch, [_done("ok")], tiers=tiers, catalog=TIER_CATALOG
    )

    result = await spec.handler(
        tasks=[
            {"agent": "researcher", "prompt": "a", "tier": "deep"},
            {"agent": "researcher", "prompt": "b", "tier": "light"},
        ],
        _launch_scope="s",
    )

    assert "[0] researcher: KeyError" in result and "unknown model 'gone'" in result
    assert "[1] researcher: ok" in result


@pytest.mark.asyncio
async def test_without_tiers_children_use_the_session_model(tmp_path, monkeypatch):
    used: list[str] = []
    spec, _ = await _make_tool(tmp_path, monkeypatch, [_done()], used_models=used)

    result = await spec.handler(
        tasks=[{"agent": "researcher", "prompt": "go", "tier": "deep"}], _launch_scope="s"
    )

    assert result == "[0] researcher: done"
    assert used == ["Child model"]


@pytest.mark.asyncio
async def test_pinned_tier_wins_over_the_callers_tier(tmp_path, monkeypatch):
    used: list[str] = []
    reviewer = _entry(tmp_path, "reviewer")
    reviewer = AgentEntry(**{**reviewer.__dict__, "tier": "deep"})
    spec, _ = await _make_tool(
        tmp_path,
        monkeypatch,
        [_done()],
        agents={"reviewer": reviewer},
        tiers=TIERS,
        catalog=TIER_CATALOG,
        used_models=used,
    )

    result = await spec.handler(
        tasks=[{"agent": "reviewer", "prompt": "review", "tier": "light"}], _launch_scope="s"
    )

    assert result == "[0] reviewer: done"
    assert used == ["big"]
    assert "reviewer — Reviewer (fixed tier: deep)" in spec.description


@pytest.mark.asyncio
async def test_pinned_model_wins_over_tiers_without_a_tier_warning(tmp_path, monkeypatch):
    used: list[str] = []
    local = AgentEntry(**{**_entry(tmp_path, "local").__dict__, "model": "mid", "tier": "deep"})
    spec, _ = await _make_tool(
        tmp_path,
        monkeypatch,
        [_done()],
        agents={"local": local},
        tiers=TIERS,
        catalog=TIER_CATALOG,
        used_models=used,
    )

    result = await spec.handler(tasks=[{"agent": "local", "prompt": "go"}], _launch_scope="s")

    assert result == "[0] local: done"
    assert used == ["mid"]
    assert "local — Local (fixed model)" in spec.description
