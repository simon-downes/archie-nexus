"""Tests for archie_orchestrator.titles: request input, reply cleaning, calls and logs (083)."""

import json
import logging
from types import SimpleNamespace

import pytest
from archie_orchestrator import titles
from archie_orchestrator.titles import (
    SYSTEM_PROMPT,
    Completion,
    TitleInput,
    Titler,
    clean_title,
    is_keep,
    read_title_input,
    render_input,
)
from archie_shared.models import (
    BedrockOpenAIProvider,
    CostConfig,
    ModelEntry,
    OllamaProvider,
    Rate,
    load_models,
)
from archie_shared.schemas import GlobalConfig, NexusConfig, OrchestratorConfig

SID = "proj-01kx77jkz8"
LUNA = "bedrock-openai-gpt-5-6-luna"


def _config(model: str | None = LUNA, region: str = "eu-west-1") -> NexusConfig:
    return NexusConfig(
        global_=GlobalConfig(region=region), orchestrator=OrchestratorConfig(title_model=model)
    )


# ---------------------------------------------------------------------------
# read_title_input and render_input
# ---------------------------------------------------------------------------


def _five_turn_log(w):
    return w.write(
        SID,
        [
            w.user("read the tui prototype doc", turn=1),
            w.assistant("Reading it now.", turn=1, iteration=1),
            w.assistant("The prototype is a mock-up of the sidebar.", turn=1, iteration=2),
            w.user("what blocks it?", turn=2),
            w.user("child prompt", turn=1, scope="tc-1", index=0),
            w.assistant("child reply", turn=1, scope="tc-1", index=0),
            w.assistant("Nothing hard-blocks it.", turn=2),
            w.user("add a workspace api", turn=3),
            w.assistant("Added GET /api/workspaces.", turn=3),
            w.user("run a review", turn=4),
            w.assistant("The review found one blocker.", turn=4),
            w.user("session titles?", turn=5),
            w.assistant("A hybrid would", turn=5, interrupted=True),
        ],
    )


def test_read_title_input_takes_root_prompts_and_final_replies(session_logs):
    path = _five_turn_log(session_logs)

    title_input = read_title_input(path, "proj", "Nexus TUI gaps")

    assert title_input == TitleInput(
        workspace="proj",
        current_title="Nexus TUI gaps",
        first_request="read the tui prototype doc",
        first_reply="The prototype is a mock-up of the sidebar.",
        recent_requests=("add a workspace api", "run a review", "session titles?"),
        latest_reply="The review found one blocker.",  # turn 5's reply was interrupted
    )


def test_render_input_labels_each_section(session_logs):
    title_input = read_title_input(_five_turn_log(session_logs), "proj", "Nexus TUI gaps")

    assert render_input(title_input) == (
        "WORKSPACE: proj\n\n"
        "CURRENT TITLE: Nexus TUI gaps\n\n"
        "FIRST REQUEST:\n<<<\nread the tui prototype doc\n>>>\n\n"
        "FIRST REPLY:\n<<<\nThe prototype is a mock-up of the sidebar.\n>>>\n\n"
        "RECENT REQUESTS (oldest first):\n<<<\n"
        "add a workspace api\n---\nrun a review\n---\nsession titles?\n>>>\n\n"
        "LATEST REPLY:\n<<<\nThe review found one blocker.\n>>>"
    )


def test_a_first_prompt_alone_renders_workspace_and_request_only(session_logs):
    w = session_logs
    path = w.write(SID, [w.user("x" * 700, turn=1)])

    title_input = read_title_input(path, "proj", None)

    assert render_input(title_input) == (
        "WORKSPACE: proj\n\nFIRST REQUEST:\n<<<\n" + "x" * 600 + "\u2026\n>>>"
    )


def test_recent_requests_and_replies_are_cut_to_their_limits():
    title_input = TitleInput(
        workspace="proj",
        current_title=None,
        first_request="a",
        first_reply="b" * 601,
        recent_requests=("c" * 401,),
        latest_reply="d" * 700,
    )
    rendered = render_input(title_input)
    assert "b" * 600 + "\u2026" in rendered and "b" * 601 not in rendered
    assert "c" * 400 + "\u2026" in rendered and "c" * 401 not in rendered
    assert "d" * 600 + "\u2026" in rendered and "d" * 601 not in rendered


def test_older_log_shapes_and_bad_lines_still_read(tmp_path):
    path = tmp_path / f"{SID}.jsonl"
    lines = [
        "not json user_message",
        json.dumps({"type": "user_message", "content": "legacy prompt", "extra": 1}),
        json.dumps({"type": "assistant_message", "turn": 1, "content": "   "}),
        json.dumps({"type": "tool_result", "content": "user_message noise"}),
    ]
    path.write_text("\n".join(lines) + "\n")

    title_input = read_title_input(path, "proj", None)

    assert title_input == TitleInput("proj", None, "legacy prompt")


def test_a_log_without_a_root_prompt_gives_none(session_logs):
    w = session_logs
    path = w.write(SID, [w.user("child", scope="tc-1", index=0), w.assistant("hi")])
    assert read_title_input(path, "proj", None) is None


def test_a_missing_log_raises_oserror(tmp_path):
    with pytest.raises(OSError):
        read_title_input(tmp_path / "missing.jsonl", "proj", None)


# ---------------------------------------------------------------------------
# clean_title and is_keep
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("reply", "expected"),
    [
        ("Workspaces API review", "Workspaces API review"),
        ('  "Workspaces API review."  ', "Workspaces API review"),
        ("`Fix the parser`", "Fix the parser"),
        ("\n\nTitle: Cloudflare WAF cleanup!\nextra line", "Cloudflare WAF cleanup"),
        ("\u201cSession titles design\u201d", "Session titles design"),
        ("Split   on\twhitespace", "Split on whitespace"),
        (
            "A title that keeps going well past the limit of forty-eight",
            "A title that keeps going well past the limit",  # no dangling "of"
        ),
        (
            "Session titles design and implementation of things",
            "Session titles design and implementation",
        ),
        ("x" * 60, "x" * 48),
        ("", None),
        ("   \n  ", None),
        ('""', None),
        ("Bell \x07 title", None),
        ("I can't help with that", None),
        ("I\u2019m sorry, but no", None),
        ("Sorry", None),
        ("As an AI I cannot", None),
    ],
)
def test_clean_title(reply, expected):
    assert clean_title(reply) == expected


@pytest.mark.parametrize(
    ("reply", "expected"),
    [("KEEP", True), ("keep.", True), (' "Keep" ', True), ("Keep it simple", False), ("", False)],
)
def test_is_keep(reply, expected):
    assert is_keep(reply) is expected


# ---------------------------------------------------------------------------
# Titler.generate and its log line
# ---------------------------------------------------------------------------


UNTITLED = TitleInput("proj", None, "add a workspace api")
TITLED = TitleInput("proj", "Workspaces API", "add a workspace api")


def _fake_complete(monkeypatch, reply: str = "Workspaces API", **usage):
    calls = []

    def complete(model, region, system, user):
        calls.append((model, region, system, user))
        return Completion(text=reply, **({"input_tokens": 1000, "output_tokens": 10} | usage))

    monkeypatch.setattr(titles, "_complete", complete)
    return calls


def test_generate_returns_the_title_with_usage_and_cost(monkeypatch):
    calls = _fake_complete(monkeypatch, cache_read_tokens=200)

    result = Titler(lambda: _config()).generate(SID, UNTITLED)

    assert (result.title, result.keep, result.error) == ("Workspaces API", False, None)
    assert (result.model_key, result.input_tokens, result.output_tokens) == (LUNA, 1200, 10)
    luna = load_models()[LUNA]
    expected = 1000 * luna.cost.rates[0].input / 1e6 + 10 * luna.cost.rates[0].output / 1e6
    expected += 200 * luna.cost.rates[0].cache_read / 1e6
    assert result.cost == pytest.approx(expected)
    [(model, region, system, user)] = calls
    assert (model.name, region, system) == (luna.name, "eu-west-1", SYSTEM_PROMPT)
    assert user == render_input(UNTITLED)


def test_generate_logs_one_line_without_content(monkeypatch, caplog):
    _fake_complete(monkeypatch)
    caplog.set_level(logging.INFO, logger="archie_orchestrator.titles")

    Titler(lambda: _config()).generate(SID, UNTITLED)

    [entry] = caplog.records
    assert entry.levelno == logging.INFO
    message = entry.getMessage()
    assert message.startswith(f"Session title {SID}: model={LUNA} tokens_in=1000 tokens_out=10")
    assert "cost_usd=0.000212" in message and message.endswith("result=ok")
    assert "workspace api" not in message.lower()


def test_keep_keeps_a_current_title(monkeypatch, caplog):
    _fake_complete(monkeypatch, reply="KEEP")
    caplog.set_level(logging.INFO, logger="archie_orchestrator.titles")

    result = Titler(lambda: _config()).generate(SID, TITLED)

    assert (result.title, result.keep, result.error) == (None, True, None)
    assert caplog.records[0].getMessage().endswith("result=keep")


@pytest.mark.parametrize("reply", ["KEEP", "I can't help with that", ""])
def test_keep_without_a_title_and_bad_replies_are_rejected(monkeypatch, caplog, reply):
    _fake_complete(monkeypatch, reply=reply)

    result = Titler(lambda: _config()).generate(SID, UNTITLED)

    assert (result.title, result.keep, result.error) == (None, False, "rejected reply")
    assert result.output_tokens == 10  # the call still cost something
    [entry] = caplog.records
    assert entry.levelno == logging.WARNING
    assert entry.getMessage().endswith("result=error=rejected reply")


def test_a_call_error_is_logged_and_returned(monkeypatch, caplog):
    def complete(*args):
        raise TimeoutError("Request timed out.\nretry later")

    monkeypatch.setattr(titles, "_complete", complete)

    result = Titler(lambda: _config()).generate(SID, UNTITLED)

    assert result.title is None
    assert result.error == "TimeoutError: Request timed out. retry later"
    [entry] = caplog.records
    assert entry.levelno == logging.WARNING
    assert entry.getMessage() == (
        f"Session title {SID}: model={LUNA} tokens_in=0 tokens_out=0 cost_usd=0.000000 "
        "result=error=TimeoutError: Request timed out. retry later"
    )


def test_an_unknown_model_key_fails_the_call(monkeypatch):
    calls = _fake_complete(monkeypatch)

    result = Titler(lambda: _config(model="no-such-model")).generate(SID, UNTITLED)

    assert result.error.startswith("KeyError") and "no-such-model" in result.error
    assert calls == []


def test_enabled_follows_the_config():
    assert Titler(lambda: _config()).enabled() is True
    assert Titler(lambda: _config(model=None)).enabled() is False

    def broken():
        raise RuntimeError("no config")

    assert Titler(broken).enabled() is False


def test_the_config_is_read_once_per_call(monkeypatch):
    _fake_complete(monkeypatch)
    reads = []

    def config():
        reads.append(1)
        return _config()

    Titler(config).generate(SID, UNTITLED)
    assert len(reads) == 1


# ---------------------------------------------------------------------------
# Providers (SDK clients patched)
# ---------------------------------------------------------------------------


class _FakeResponses:
    def __init__(self, owner):
        self.owner = owner

    def create(self, **kwargs):
        self.owner.requests.append(kwargs)
        details = SimpleNamespace(cached_tokens=100, cache_write_tokens=0)
        usage = SimpleNamespace(input_tokens=900, output_tokens=7, input_tokens_details=details)
        return SimpleNamespace(output_text="Workspaces API", usage=usage)


class _FakeClient:
    instances: list = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.requests: list = []
        self.responses = _FakeResponses(self)
        _FakeClient.instances.append(self)


BEDROCK_KEYS = SimpleNamespace(
    aws_access_key_id="AKIA", aws_secret_access_key="secret", aws_session_token="tok"
)


@pytest.fixture
def bedrock_keys(monkeypatch):
    monkeypatch.setattr(
        "archie_shared.credentials.get_credential",
        lambda name: BEDROCK_KEYS if name == "bedrock" else None,
    )


@pytest.fixture
def fake_bedrock_openai(monkeypatch, bedrock_keys):
    import openai

    _FakeClient.instances = []
    monkeypatch.setattr(openai, "BedrockOpenAI", _FakeClient)
    return _FakeClient


@pytest.mark.title_calls
def test_bedrock_openai_sends_one_responses_request(fake_bedrock_openai):
    result = Titler(lambda: _config()).generate(SID, UNTITLED)

    [client] = fake_bedrock_openai.instances
    assert client.kwargs["aws_region"] == "us-east-1"  # the catalogue entry's region
    assert client.kwargs["base_url"] == "https://bedrock-runtime.us-east-1.amazonaws.com/openai/v1"
    assert (client.kwargs["timeout"], client.kwargs["max_retries"]) == (15.0, 1)
    [request] = client.requests
    assert request == {
        "model": "global.openai.gpt-5.6-luna",
        "input": [
            {"role": "developer", "content": SYSTEM_PROMPT},
            {"role": "user", "content": render_input(UNTITLED)},
        ],
        "max_output_tokens": 64,
        "reasoning": {"effort": "none"},
        "store": False,
    }
    # 900 raw input tokens of which 100 were cached: 800 billable input + 100 cache reads.
    assert (result.title, result.input_tokens, result.output_tokens) == ("Workspaces API", 900, 7)
    rate = load_models()[LUNA].cost.rates[0]
    assert result.cost == pytest.approx(
        (800 * rate.input + 100 * rate.cache_read + 7 * rate.output) / 1e6
    )


@pytest.mark.title_calls
def test_bedrock_openai_tokens_come_from_the_stored_credential(fake_bedrock_openai, monkeypatch):
    import aws_bedrock_token_generator

    seen = []

    def provide_token(*, region, aws_credentials_provider):
        seen.append((region, aws_credentials_provider.load()))
        return "token"

    monkeypatch.setattr(aws_bedrock_token_generator, "provide_token", provide_token)
    Titler(lambda: _config()).generate(SID, UNTITLED)

    token = fake_bedrock_openai.instances[0].kwargs["bedrock_token_provider"]()
    assert token == "token"
    [(region, credentials)] = seen
    assert region == "us-east-1"
    assert (credentials.access_key, credentials.secret_key, credentials.token) == (
        "AKIA",
        "secret",
        "tok",
    )


@pytest.mark.title_calls
@pytest.mark.parametrize("model", [LUNA, "bedrock-claude-haiku-4-5"])
def test_bedrock_models_never_use_the_default_aws_chain(monkeypatch, model):
    import boto3
    import openai

    def no_client(*args, **kwargs):
        raise AssertionError("no client may be built without a stored credential")

    monkeypatch.setattr(openai, "BedrockOpenAI", no_client)
    monkeypatch.setattr(boto3, "client", no_client)
    monkeypatch.setattr("archie_shared.credentials.get_credential", lambda name: None)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIA-AMBIENT")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "ambient")

    result = Titler(lambda: _config(model=model)).generate(SID, UNTITLED)

    assert result.error == ("RuntimeError: No bedrock credential: run `archie auth login bedrock`")


@pytest.mark.title_calls
def test_a_provider_without_a_region_uses_global_region(fake_bedrock_openai):
    catalog = {
        "titles": ModelEntry(
            name="T",
            context=1000,
            provider=BedrockOpenAIProvider(model_id="m"),
            cost=CostConfig(rates=[Rate(input=1, output=1)]),
        )
    }
    Titler(lambda: _config(model="titles", region="ap-south-1"), catalog=catalog).generate(
        SID, UNTITLED
    )
    assert fake_bedrock_openai.instances[0].kwargs["aws_region"] == "ap-south-1"


@pytest.mark.title_calls
def test_openai_uses_the_stored_key(fake_bedrock_openai, monkeypatch):
    import openai

    monkeypatch.setattr(openai, "OpenAI", _FakeClient)
    monkeypatch.setattr(
        "archie_shared.credentials.get_credential",
        lambda name: SimpleNamespace(api_key="sk-test") if name == "openai" else None,
    )
    monkeypatch.setenv("OPENAI_BASE_URL", "http://elsewhere.invalid/v1")
    model = "openai-gpt-6-luna"
    assert model in load_models()

    result = Titler(lambda: _config(model=model)).generate(SID, UNTITLED)

    [client] = _FakeClient.instances
    assert client.kwargs == {
        "api_key": "sk-test",
        "base_url": "https://api.openai.com/v1",  # never OPENAI_BASE_URL
        "max_retries": 1,
        "timeout": 15.0,
    }
    assert client.requests[0]["model"] == "gpt-6-luna"
    assert client.requests[0]["reasoning"] == {"effort": "none"}
    assert result.title == "Workspaces API"


@pytest.mark.title_calls
def test_openai_without_a_key_is_a_logged_error(monkeypatch):
    monkeypatch.setattr("archie_shared.credentials.get_credential", lambda name: None)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    result = Titler(lambda: _config(model="openai-gpt-6-luna")).generate(SID, UNTITLED)

    assert result.error == "RuntimeError: No OpenAI API key: run `archie auth login openai`"


class _FakeBedrockRuntime:
    def __init__(self):
        self.client_kwargs: dict = {}
        self.requests: list = []

    def client(self, service, **kwargs):
        assert service == "bedrock-runtime"
        self.client_kwargs = kwargs
        return self

    def converse(self, **kwargs):
        self.requests.append(kwargs)
        return {
            "output": {"message": {"content": [{"text": "Cloudflare "}, {"text": "WAF review"}]}},
            "usage": {"inputTokens": 500, "outputTokens": 6, "cacheReadInputTokens": 0},
        }


@pytest.mark.title_calls
def test_bedrock_converse_sends_one_request(monkeypatch):
    import boto3

    fake = _FakeBedrockRuntime()
    monkeypatch.setattr(boto3, "client", fake.client)
    monkeypatch.setattr(
        "archie_shared.credentials.get_credential",
        lambda name: SimpleNamespace(
            aws_access_key_id="AKIA", aws_secret_access_key="secret", aws_session_token=None
        ),
    )
    model = "bedrock-claude-haiku-4-5"

    result = Titler(lambda: _config(model=model, region="eu-west-2")).generate(SID, UNTITLED)

    assert fake.client_kwargs["region_name"] == "eu-west-2"  # Haiku's entry has no region
    assert (
        fake.client_kwargs["aws_access_key_id"],
        fake.client_kwargs["aws_secret_access_key"],
    ) == (
        "AKIA",
        "secret",
    )
    assert "aws_session_token" not in fake.client_kwargs
    config = fake.client_kwargs["config"]
    assert (config.read_timeout, config.retries["max_attempts"]) == (15.0, 2)
    [request] = fake.requests
    assert request == {
        "modelId": "global.anthropic.claude-haiku-4-5-20251001-v1:0",
        "system": [{"text": SYSTEM_PROMPT}],
        "messages": [{"role": "user", "content": [{"text": render_input(UNTITLED)}]}],
        "inferenceConfig": {"maxTokens": 40},
    }
    assert (result.title, result.input_tokens, result.output_tokens) == (
        "Cloudflare WAF review",
        500,
        6,
    )
    haiku = load_models()[model].cost.rates[0]
    assert result.cost == pytest.approx(500 * haiku.input / 1e6 + 6 * haiku.output / 1e6)


@pytest.mark.title_calls
def test_an_unsupported_provider_is_a_logged_error(caplog):
    catalog = {"local": ModelEntry(name="L", context=1000, provider=OllamaProvider(model_id="x"))}

    result = Titler(lambda: _config(model="local"), catalog=catalog).generate(SID, UNTITLED)

    assert result.error == "ValueError: Unsupported title model provider: ollama"
    assert "Unsupported title model provider: ollama" in caplog.records[0].getMessage()


def test_title_model_calls_are_blocked_outside_marked_tests():
    result = Titler(lambda: _config()).generate(SID, UNTITLED)
    assert result.error == "RuntimeError: title model calls are disabled in tests"
