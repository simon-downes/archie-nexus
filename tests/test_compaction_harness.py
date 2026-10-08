"""Harness-level context compaction: manual, threshold and cold-cache triggers."""

import pytest
from archie_agent.harness import AgentHarness
from archie_agent.llm._types import Done, TextDelta, ToolUseEvent, ToolUseStart, Usage
from archie_agent.llm.fake import FakeLLMClient
from archie_agent.loop import PreRequest
from archie_agent.session import Session, Turn
from archie_shared.events import (
    ContextCompacted,
    ErrorNotice,
    IterationStart,
    LLMRequest,
    TurnComplete,
    TurnError,
    decode_event,
)
from archie_shared.models import DEFAULT_MODELS, BedrockProvider, CostConfig, ModelEntry, Rate
from archie_shared.schemas import CompactionConfig
from archie_shared.session.log import LogAppendError
from archie_shared.types import TextBlock

# --- Fixtures ---


def _session_model(*, cache_ttl: int | None = None, context: int = 200_000) -> ModelEntry:
    return ModelEntry(
        name="Session",
        context=context,
        provider=BedrockProvider(model_id="session"),
        can_cache=cache_ttl is not None,
        cache_ttl_seconds=cache_ttl,
        cost=CostConfig(rates=[Rate(input=5, output=25, cache_read=0.5, cache_write=6.25)]),
        max_output_tokens=1000,
    )


def _compaction_model() -> ModelEntry:
    return ModelEntry(
        name="Compactor",
        context=1_000_000,
        provider=BedrockProvider(model_id="compactor"),
        cost=CostConfig(rates=[Rate(input=0.1, output=0.5)]),
        max_output_tokens=1000,
    )


def _summary(text: str = "## Goal\nKeep going") -> list:
    return [TextDelta(text), Usage(100, 20), Done("end_turn")]


def _reply(context_tokens: int, text: str = "ok") -> list:
    return [TextDelta(text), Usage(context_tokens, 10), Done("end_turn")]


def _tool_call(tool_use_id: str, context_tokens: int) -> list:
    return [
        ToolUseStart(tool_use_id=tool_use_id, name="unknown_tool"),
        ToolUseEvent(tool_use_id=tool_use_id, name="unknown_tool", input={}),
        Usage(context_tokens, 10),
        Done("tool_use"),
    ]


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def _harness(
    tmp_path,
    responses: list[list],
    summaries: list[list] | None = None,
    *,
    model: ModelEntry | None = None,
    config: CompactionConfig | None = None,
    clock=None,
) -> tuple[AgentHarness, FakeLLMClient, FakeLLMClient]:
    active = model or _session_model()
    llm = FakeLLMClient(responses)
    compaction = FakeLLMClient(summaries or [])
    harness = AgentHarness(
        Session("session", active, "compact"),
        llm,
        active.name,
        tmp_path,
        model_catalog={"session": active, "compactor": _compaction_model()},
        compaction=config or CompactionConfig(model="compactor", percent=50),
        compaction_llm=compaction,
        **({"clock": clock} if clock is not None else {}),
    )
    return harness, llm, compaction


def _events(harness: AgentHarness) -> list:
    path = harness.log_path
    if not path.exists():
        return []
    return [decode_event(line, persisted=True) for line in path.read_bytes().splitlines()]


def _of(events, cls) -> list:
    return [event for event in events if isinstance(event, cls)]


def _sequence(events) -> list[str]:
    names = []
    for event in events:
        if isinstance(event, LLMRequest):
            names.append(f"llm_request:{event.purpose}")
        elif isinstance(event, ContextCompacted):
            names.append(f"context_compacted:{event.reason}")
        else:
            names.append(type(event).__name__)
    return names


def _texts(turn: Turn) -> list[str]:
    return [block.text for block in turn.content if isinstance(block, TextBlock)]


# --- Manual compaction ---


async def test_manual_compaction_persists_order_and_replaces_transcript(tmp_path):
    model = DEFAULT_MODELS["bedrock-openai-gpt-5-6-luna"]
    session = Session("bedrock-openai-gpt-5-6-luna", model, "compact-test")
    session.add_turn("user", "We are fixing the parser", turn_index=1)
    compaction = FakeLLMClient([_summary("## Goal\nFix parser")])
    harness = AgentHarness(
        session=session,
        llm_client=FakeLLMClient([[]]),
        model_name=model.name,
        log_dir=tmp_path,
        model_catalog=DEFAULT_MODELS,
        compaction=CompactionConfig(),
        compaction_llm=compaction,
    )

    await harness.run_manual_compaction()

    events = _events(harness)
    assert len(compaction.calls) == 1
    assert compaction.calls[0]["tool_config"] is None
    assert _sequence(events) == ["llm_request:compaction", "context_compacted:manual"]
    request = _of(events, LLMRequest)[0]
    assert request.model_key == "bedrock-openai-gpt-6-luna"
    assert _of(events, ContextCompacted)[0].request_id == request.id
    assert len(session.turns) == 1
    assert session.turns[0].compacted
    assert session.total_cost > 0
    assert harness.turn_active is False


async def test_manual_compaction_priced_at_compaction_model_rates(tmp_path):
    harness, _, _ = _harness(tmp_path, [], [_summary()])
    harness.session.add_turn("user", "hello")
    harness._turn_active = True

    await harness.run_manual_compaction()

    request = _of(_events(harness), LLMRequest)[0]
    # Compactor rates: 100 input × $0.1/M + 20 output × $0.5/M.
    assert request.cost_usd == pytest.approx(100 * 0.1 / 1e6 + 20 * 0.5 / 1e6)
    assert harness.turn_active is False


async def test_manual_empty_transcript_does_not_call_provider(tmp_path):
    harness, _, compaction = _harness(tmp_path, [])
    harness._turn_active = True

    await harness.run_manual_compaction()

    assert not compaction.calls
    assert harness.turn_active is False
    assert not _of(_events(harness), LLMRequest)


async def test_manual_provider_failure_records_request_and_keeps_transcript(tmp_path):
    class FailingClient:
        model_id = "failing"

        def stream(self, messages, system, tool_config=None, history_boundary=None):
            yield Usage(10, 0)
            raise RuntimeError("provider down")

    harness, _, _ = _harness(tmp_path, [])
    harness._compaction_llm = FailingClient()
    harness.session.add_turn("user", "hello")
    notices = []
    original = harness.event_bus.emit

    async def emit(event, *args, **kwargs):
        if isinstance(event, ErrorNotice):
            notices.append(event)
        return await original(event, *args, **kwargs)

    harness.event_bus.emit = emit
    await harness.run_manual_compaction()

    assert [notice.kind for notice in notices] == ["manual_compaction_failed"]
    assert _of(_events(harness), LLMRequest)[0].status == "error"
    assert not _of(_events(harness), ContextCompacted)
    assert not harness.session.turns[0].compacted


async def test_manual_empty_summary_fails_without_replacing(tmp_path):
    harness, _, _ = _harness(tmp_path, [], [[Usage(10, 0), Done("end_turn")]])
    harness.session.add_turn("user", "hello")

    await harness.run_manual_compaction()

    assert _of(_events(harness), LLMRequest)[0].purpose == "compaction"
    assert not _of(_events(harness), ContextCompacted)
    assert _texts(harness.session.turns[0]) == ["hello"]


async def test_manual_storage_error_uses_storage_notice(tmp_path, monkeypatch):
    harness, _, _ = _harness(tmp_path, [], [_summary()])
    harness.session.add_turn("user", "hello")
    original = harness.event_bus.emit
    calls = 0
    emitted = []

    async def emit(event, *args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise LogAppendError("disk full")
        emitted.append(event)
        return await original(event, *args, **kwargs)

    monkeypatch.setattr(harness.event_bus, "emit", emit)
    await harness.run_manual_compaction()

    assert not harness.session.turns[0].compacted
    assert any(getattr(event, "kind", None) == "storage_error" for event in emitted)


async def test_next_message_after_manual_merges_into_compacted_turn(tmp_path):
    harness, llm, _ = _harness(tmp_path, [_reply(500, "Parser")], [_summary()])
    harness.session.add_turn("user", "We are fixing the parser")

    await harness.run_manual_compaction()
    await harness.handle_message("what were we doing?")

    sent = llm.calls[0]["messages"]
    assert len(sent) == 1
    assert sent[0].role == "user"
    texts = _texts(sent[0])
    assert texts[0].startswith("Earlier conversation was compacted")
    assert texts[-1] == "what were we doing?"
    assert [turn.role for turn in harness.session.turns] == ["user", "assistant"]


async def test_manual_compaction_tokens_after_ignores_stale_baseline(tmp_path):
    harness, _, _ = _harness(tmp_path, [_reply(90_000)], [_summary()])
    await harness.handle_message("first")
    harness._turn_active = True

    await harness.run_manual_compaction()

    compacted = _of(_events(harness), ContextCompacted)[0]
    assert compacted.tokens_before > 90_000
    assert compacted.tokens_after < 20_000


# --- Threshold trigger ---


async def test_below_limit_does_not_compact(tmp_path):
    harness, _, compaction = _harness(tmp_path, [_reply(500), _reply(600)])

    await harness.handle_message("first")
    await harness.handle_message("second")

    assert not compaction.calls
    assert not _of(_events(harness), ContextCompacted)


async def test_over_limit_at_turn_start_compacts_and_keeps_user_message(tmp_path):
    harness, llm, compaction = _harness(
        tmp_path, [_reply(150_000), _reply(500, "answer")], [_summary()]
    )

    await harness.handle_message("first")
    await harness.handle_message("second")

    events = _events(harness)
    second_turn = [e for e in events if getattr(e, "turn", None) == 2]
    assert _sequence(second_turn) == [
        "UserMessage",
        "IterationStart",
        "llm_request:compaction",
        "context_compacted:threshold",
        "llm_request:conversation",
        "AssistantMessage",
        "TurnComplete",
    ]
    compacted = _of(events, ContextCompacted)[0]
    assert compacted.tokens_before > 150_000
    assert compacted.tokens_after < 50_000
    # The summarised transcript excludes the pending message ...
    transcript = compaction.calls[0]["messages"][0].content[0].text
    assert "first" in transcript
    assert "second" not in transcript
    # ... which is carried verbatim, once, into the replacement.
    sent = llm.calls[1]["messages"]
    assert len(sent) == 1
    assert _texts(sent[0])[-1] == "second"
    assert _texts(sent[0]).count("second") == 1


async def test_over_limit_mid_turn_uses_continuation(tmp_path):
    harness, llm, _ = _harness(
        tmp_path, [_tool_call("tu_1", 150_000), _reply(500, "done")], [_summary()]
    )

    await harness.handle_message("do work")

    events = _events(harness)
    compacted = _of(events, ContextCompacted)
    assert [(c.reason, c.iteration) for c in compacted] == [("threshold", 1)]
    sent = llm.calls[1]["messages"]
    assert len(sent) == 1
    assert "Continue from the summary" in _texts(sent[0])[-1]
    assert _of(events, TurnComplete)


async def test_two_compactions_in_one_turn(tmp_path):
    harness, llm, compaction = _harness(
        tmp_path,
        [_tool_call("tu_1", 150_000), _tool_call("tu_2", 150_000), _reply(500, "done")],
        [_summary("## Goal\nfirst"), _summary("## Goal\nsecond")],
    )

    await harness.handle_message("do work")

    compacted = _of(_events(harness), ContextCompacted)
    assert [c.iteration for c in compacted] == [1, 2]
    assert len(compaction.calls) == 2
    # The second compaction merges the first summary.
    assert "first" in compaction.calls[1]["messages"][0].content[0].text
    assert _of(_events(harness), TurnComplete)


async def test_manual_then_auto_carries_only_new_message(tmp_path):
    harness, llm, compaction = _harness(
        tmp_path,
        [_reply(150_000, "a1"), _reply(500, "a2")],
        [_summary("## Goal\nmanual"), _summary("## Goal\nauto")],
    )
    harness.session.add_turn("user", "history")
    await harness.run_manual_compaction()
    await harness.handle_message("turn one")
    await harness.handle_message("turn two")

    # The auto compaction summarises the prior summary and turn one ...
    transcript = compaction.calls[1]["messages"][0].content[0].text
    assert "manual" in transcript
    assert "turn one" in transcript
    assert "turn two" not in transcript
    # ... and carries only the new message, not the old framing or summary.
    sent = llm.calls[1]["messages"][0]
    texts = _texts(sent)
    assert len(texts) == 3
    assert "auto" in texts[1]
    assert "manual" not in "".join(texts)
    assert texts[2] == "turn two"


async def test_unanswered_message_after_failed_turn_is_summarised(tmp_path):
    harness, _, compaction = _harness(
        tmp_path, [], [_summary("## Goal\nmanual"), _summary("## Goal\nauto")]
    )
    harness.session.add_turn("user", "history")
    await harness.run_manual_compaction()

    async def fail_turn(*_args, **_kwargs):
        return PreRequest(error="forced failure")

    harness._before_request = fail_turn
    await harness.handle_message("unanswered")
    del harness._before_request
    # Both messages are merged into the single compacted user turn.
    harness.session.add_turn("user", "turn two")
    assert [turn.role for turn in harness.session.turns] == ["user"]
    harness._current_turn_index = 3
    harness._baseline_context_tokens = 150_000
    harness._baseline_turn_count = 0

    result = await harness._before_request(harness.session.turns, 0)

    transcript = compaction.calls[1]["messages"][0].content[0].text
    assert "unanswered" in transcript
    assert "turn two" not in transcript
    assert result is not None and result.messages is not None
    texts = _texts(result.messages[0])
    assert texts[-1] == "turn two"
    assert "unanswered" not in "".join(texts)


async def test_still_over_limit_after_compaction_ends_turn_with_error(tmp_path):
    harness, llm, _ = _harness(tmp_path, [_reply(150_000)], [_summary("x" * 500_000)])

    await harness.handle_message("first")
    await harness.handle_message("second")

    events = _events(harness)
    assert _of(events, ContextCompacted)
    errors = _of(events, TurnError)
    assert errors and "still exceeds" in errors[-1].message
    assert len(llm.calls) == 1


async def test_failed_auto_compaction_notices_once_and_continues(tmp_path):
    harness, llm, _ = _harness(
        tmp_path,
        [_reply(150_000), _tool_call("tu_1", 150_000), _reply(500, "done")],
        [[Usage(10, 0), Done("end_turn")]],
    )
    notices = []
    original = harness.event_bus.emit

    async def emit(event, *args, **kwargs):
        if isinstance(event, ErrorNotice):
            notices.append(event.kind)
        return await original(event, *args, **kwargs)

    harness.event_bus.emit = emit
    await harness.handle_message("first")
    await harness.handle_message("second")

    assert notices == ["compaction_failed"]
    assert len(harness._compaction_llm.calls) == 1
    assert len(llm.calls) == 3
    assert _of(_events(harness), TurnComplete)[-1].turn == 2


async def test_child_agents_never_compact(tmp_path, monkeypatch):
    seen = {}

    async def fake_run_loop(**kwargs):
        seen.update(kwargs)
        if False:
            yield None

    harness, _, _ = _harness(tmp_path, [])
    monkeypatch.setattr("archie_agent.agents.create_llm_client", lambda *args: object())
    monkeypatch.setattr("archie_agent.agents.run_loop", fake_run_loop)
    task = harness._registry.get("task")
    assert task is not None
    await task.handler(
        tasks=[{"agent": "missing", "prompt": "child"}], _launch_scope="t1", _parent_turn=1
    )

    assert "before_request" not in seen or seen["before_request"] is None


async def test_hook_sees_flushed_tool_turns(tmp_path):
    harness, _, _ = _harness(tmp_path, [_tool_call("tu_1", 500), _reply(500, "done")])
    snapshots = []
    original = harness._before_request

    async def spy(messages, iteration):
        snapshots.append((iteration, [t.role for t in harness.session.turns]))
        return await original(messages, iteration)

    harness._before_request = spy
    await harness.handle_message("go")

    assert snapshots == [(0, ["user"]), (1, ["user", "assistant", "user"])]


# --- Cold-cache trigger ---


def _cold_harness(tmp_path, *, threshold: float = 0.5, cache_ttl: int | None = 300):
    clock = Clock()
    harness, llm, compaction = _harness(
        tmp_path,
        [_reply(200_000), _reply(500), _reply(500)],
        [_summary()],
        model=_session_model(cache_ttl=cache_ttl, context=1_000_000),
        config=CompactionConfig(model="compactor", cold_cost_threshold=threshold),
        clock=clock,
    )
    return harness, compaction, clock


async def test_cold_within_ttl_does_not_compact(tmp_path):
    harness, compaction, clock = _cold_harness(tmp_path)
    await harness.handle_message("first")
    clock.now += 299
    await harness.handle_message("second")

    assert not compaction.calls


async def test_cold_past_ttl_below_threshold_does_not_compact(tmp_path):
    harness, compaction, clock = _cold_harness(tmp_path, threshold=100.0)
    await harness.handle_message("first")
    clock.now += 301
    await harness.handle_message("second")

    assert not compaction.calls


async def test_cold_past_ttl_above_threshold_compacts(tmp_path):
    harness, compaction, clock = _cold_harness(tmp_path)
    await harness.handle_message("first")
    clock.now += 301
    await harness.handle_message("second")

    compacted = _of(_events(harness), ContextCompacted)
    assert [(c.reason, c.iteration) for c in compacted] == [("cold_cache", 0)]


async def test_cold_check_skipped_after_iteration_zero(tmp_path):
    harness, _, clock = _cold_harness(tmp_path)
    harness._last_request_finished = 0.0
    clock.now = 10_000

    result = await harness._before_request([Turn("user", [TextBlock("x" * 400_000)])], 1)

    assert result is None


async def test_cold_after_model_switch_compacts(tmp_path):
    harness, compaction, _ = _cold_harness(tmp_path)
    await harness.handle_message("first")
    model = harness.session.model
    harness.switch_model("session", model, harness._llm)
    # A switch clears the baseline, so give the transcript real size.
    harness.session.turns[0].content.append(TextBlock("x" * 400_000))
    await harness.handle_message("second")

    assert [c.reason for c in _of(_events(harness), ContextCompacted)] == ["cold_cache"]


async def test_cold_never_triggers_for_non_caching_model(tmp_path):
    harness, compaction, clock = _cold_harness(tmp_path, threshold=0, cache_ttl=None)
    await harness.handle_message("first")
    clock.now += 10_000
    await harness.handle_message("second")

    assert not compaction.calls


async def test_cold_without_prior_request_does_not_compact(tmp_path):
    harness, compaction, clock = _cold_harness(tmp_path, threshold=0)
    clock.now = 10_000

    result = await harness._before_request([Turn("user", [TextBlock("hello")])], 0)

    assert result is None
    assert not compaction.calls


async def test_cold_and_threshold_reason_is_cold_cache(tmp_path):
    harness, compaction, clock = _cold_harness(tmp_path)
    harness._compaction_config = CompactionConfig(model="compactor", percent=50)
    harness._llm = FakeLLMClient([_reply(600_000), _reply(500)])
    await harness.handle_message("first")
    clock.now += 301
    await harness.handle_message("second")

    assert [c.reason for c in _of(_events(harness), ContextCompacted)] == ["cold_cache"]


async def test_iteration_start_precedes_compaction(tmp_path):
    harness, _, _ = _harness(tmp_path, [_reply(150_000), _reply(500)], [_summary()])
    await harness.handle_message("first")
    await harness.handle_message("second")

    second = [e for e in _events(harness) if getattr(e, "turn", None) == 2]
    start = next(i for i, e in enumerate(second) if isinstance(e, IterationStart))
    assert _sequence(second)[start : start + 4] == [
        "IterationStart",
        "llm_request:compaction",
        "context_compacted:threshold",
        "llm_request:conversation",
    ]


# --- Interrupts, model validation and shared compaction state ---


class _InterruptingClient:
    """A compaction client that interrupts the harness mid-stream."""

    model_id = "interrupting"

    def __init__(self) -> None:
        self.harness: AgentHarness | None = None
        self.calls = 0

    def stream(self, messages, system, tool_config=None, history_boundary=None):
        self.calls += 1
        yield TextDelta("## Goal\npartial")
        assert self.harness is not None
        self.harness.interrupt()
        yield TextDelta(" more")
        yield Usage(100, 20)
        yield Done("end_turn")


async def test_interrupt_during_auto_compaction_skips_conversation_request(tmp_path):
    harness, llm, _ = _harness(tmp_path, [_reply(150_000), _reply(500, "unused")])
    client = _InterruptingClient()
    client.harness = harness
    harness._compaction_llm = client
    await harness.handle_message("first")
    before = list(harness.session.turns)

    await harness.handle_message("second")

    second = [e for e in _events(harness) if getattr(e, "turn", None) == 2]
    assert _sequence(second) == [
        "UserMessage",
        "IterationStart",
        "llm_request:compaction",
        "TurnInterrupted",
    ]
    assert _of(second, LLMRequest)[0].status == "interrupted"
    assert client.calls == 1
    assert len(llm.calls) == 1
    assert not _of(_events(harness), ContextCompacted)
    # The transcript is unchanged apart from the new user message.
    assert harness.session.turns[: len(before)] == before
    assert not any(turn.compacted for turn in harness.session.turns)


async def test_interrupt_during_manual_compaction_keeps_transcript(tmp_path):
    harness, _, _ = _harness(tmp_path, [])
    client = _InterruptingClient()
    client.harness = harness
    harness._compaction_llm = client
    harness.session.add_turn("user", "hello")
    harness._turn_active = True
    notices = []
    original = harness.event_bus.emit

    async def emit(event, *args, **kwargs):
        if isinstance(event, ErrorNotice):
            notices.append(event.kind)
        return await original(event, *args, **kwargs)

    harness.event_bus.emit = emit
    await harness.run_manual_compaction()

    assert notices == ["manual_compaction_failed"]
    assert _of(_events(harness), LLMRequest)[0].status == "interrupted"
    assert not _of(_events(harness), ContextCompacted)
    assert _texts(harness.session.turns[0]) == ["hello"]
    assert harness.turn_active is False


def test_unknown_compaction_model_is_rejected(tmp_path):
    active = _session_model()
    with pytest.raises(ValueError, match="Unknown compaction model: missing"):
        AgentHarness(
            Session("session", active, "compact"),
            FakeLLMClient([]),
            active.name,
            tmp_path,
            model_catalog={"session": active},
            compaction=CompactionConfig(model="missing"),
            compaction_llm=FakeLLMClient([]),
        )


@pytest.mark.parametrize("manual", [True, False])
async def test_compaction_debug_metadata(tmp_path, manual):
    harness, _, _ = _harness(tmp_path, [_reply(150_000), _reply(500)], [_summary()])
    await harness.handle_message("first")
    if manual:
        await harness.run_manual_compaction()
    else:
        await harness.handle_message("second")

    debug = _of(_events(harness), ContextCompacted)[0].debug
    fields = dict(part.split("=", 1) for part in debug.split(";"))
    assert set(fields) == {
        "estimate_source",
        "turns_in",
        "tool_results_kept",
        "tool_results_removed",
        "orphan_results",
        "rounds_dropped",
    }
    assert fields["estimate_source"] == "baseline"
    assert "first" not in debug


async def test_manual_compaction_resets_cold_cache_state(tmp_path):
    harness, _, clock = _cold_harness(tmp_path)
    await harness.handle_message("first")
    harness._cache_cold = True
    clock.now += 10_000

    await harness.run_manual_compaction()

    assert harness._cache_cold is False
    assert harness._last_request_finished is None
    await harness.handle_message("second")
    assert [c.reason for c in _of(_events(harness), ContextCompacted)] == ["manual"]


async def test_manual_tokens_before_measured_before_compaction(tmp_path):
    harness, _, _ = _harness(tmp_path, [_reply(90_000)], [_summary()])
    await harness.handle_message("first")
    expected = harness._estimate_context(harness.session.turns)

    await harness.run_manual_compaction()

    assert _of(_events(harness), ContextCompacted)[0].tokens_before == expected


async def test_tokens_after_excludes_output_reserve(tmp_path):
    harness, _, _ = _harness(tmp_path, [_reply(90_000)], [_summary()])
    await harness.handle_message("first")

    await harness.run_manual_compaction()

    compacted = _of(_events(harness), ContextCompacted)[0]
    replacement = harness.session.turns[0]
    from archie_agent.compaction import estimate_tokens

    assert compacted.tokens_after == estimate_tokens(
        harness._build_prompt(), harness._tool_config, [replacement]
    )
    # tokens_before is the baseline-derived input context, also reserve-free.
    assert compacted.tokens_before < 90_000 + 1_000
