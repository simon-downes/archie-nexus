"""Integration tests: exec calls see live tool-policy changes (plan 065)."""

import json
import sys

import pytest
from archie_agent.agents import ChildDispatch
from archie_agent.exec.tool import create_registry
from archie_agent.harness import AgentHarness
from archie_agent.llm.fake import FakeLLMClient
from archie_agent.session import Session
from archie_agent.tool_policy_live import (
    RECOVERED_KIND,
    RELOAD_FAILED_KIND,
    LiveToolPolicy,
    load_policy_snapshot,
)
from archie_shared.models import BedrockProvider, CostConfig, ModelEntry, Rate
from archie_shared.types import ToolUseBlock

SOURCE = "async def main():\n    return _tool_policy\n"


def _config(write_enabled: bool) -> str:
    flag = "true" if write_enabled else "false"
    return f"plugins:\n  notion:\n    policy:\n      write:\n        enabled: {flag}\n"


def _model() -> ModelEntry:
    return ModelEntry(
        name="Test Model",
        provider=BedrockProvider(model_id="test-model-id", region="us-east-1"),
        cost=CostConfig(rates=[Rate(input=3.0, output=15.0)]),
        context=200000,
        max_output_tokens=4096,
    )


class FakeWebSocket:
    def __init__(self):
        self.messages: list[str] = []

    async def send_text(self, data: str) -> None:
        self.messages.append(data)

    def notices(self) -> list[dict]:
        events = [json.loads(m) for m in self.messages]
        return [e for e in events if e["type"] == "error_notice"]


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("ARCHIE_HOME_DIR", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    return tmp_path / "home"


def _harness(tmp_path, policy) -> AgentHarness:
    return AgentHarness(
        session=Session(model_id="test-model", model=_model(), session_id="s-065"),
        llm_client=FakeLLMClient(responses=[]),
        model_name="Test Model",
        log_dir=tmp_path,
        exec_python=sys.executable,
        exec_run_root=tmp_path / "runs",
        tool_policy=policy,
    )


def _notion_write(content: str) -> bool:
    assert content.startswith("return: "), content
    policy = json.loads(content.removeprefix("return: "))
    return policy["notion"]["write"]["enabled"]


async def _exec(executor) -> str:
    block = ToolUseBlock(tool_use_id="tu", name="exec", input={"source": SOURCE})
    result = await executor(block)
    assert not result.is_error, result.content
    return result.content


@pytest.mark.asyncio
async def test_root_exec_sees_config_change_without_restart(tmp_path, home):
    (home / "config.yaml").write_text(_config(False))
    harness = _harness(tmp_path, LiveToolPolicy(load_policy_snapshot()))

    assert _notion_write(await _exec(harness._execute_tool)) is False
    (home / "config.yaml").write_text(_config(True))
    assert _notion_write(await _exec(harness._execute_tool)) is True


@pytest.mark.asyncio
async def test_invalid_config_keeps_last_policy_with_one_notice(tmp_path, home):
    (home / "config.yaml").write_text(_config(True))
    harness = _harness(tmp_path, LiveToolPolicy(load_policy_snapshot()))
    ws = FakeWebSocket()
    await harness.event_bus.register_client(ws, ())

    (home / "config.yaml").write_text("plugins: [unclosed\n")
    for _ in range(3):
        assert _notion_write(await _exec(harness._execute_tool)) is True
    notices = ws.notices()
    assert [n["kind"] for n in notices] == [RELOAD_FAILED_KIND]

    (home / "config.yaml").write_text(_config(False))
    assert _notion_write(await _exec(harness._execute_tool)) is False
    assert [n["kind"] for n in ws.notices()] == [RELOAD_FAILED_KIND, RECOVERED_KIND]


@pytest.mark.asyncio
async def test_child_dispatch_awaits_live_policy_per_call(tmp_path):
    states = iter(
        [{"notion": {"write": {"enabled": False}}}, {"notion": {"write": {"enabled": True}}}]
    )

    async def source() -> dict:
        return next(states)

    dispatch = ChildDispatch(
        create_registry(),
        exec_python=sys.executable,
        exec_run_root=tmp_path / "runs",
        tool_policy=source,
    )
    assert _notion_write(await _exec(dispatch.execute)) is False
    assert _notion_write(await _exec(dispatch.execute)) is True


@pytest.mark.asyncio
async def test_child_dispatch_accepts_plain_dict(tmp_path):
    dispatch = ChildDispatch(
        create_registry(),
        exec_python=sys.executable,
        exec_run_root=tmp_path / "runs",
        tool_policy={"notion": {"write": {"enabled": True}}},
    )
    assert _notion_write(await _exec(dispatch.execute)) is True


def test_harness_wraps_dict_as_static(tmp_path):
    harness = _harness(tmp_path, {"notion": {}})
    assert harness._tool_policy.refresh() == ({"notion": {}}, None)


def test_harness_passes_live_source_to_task_tool(tmp_path, monkeypatch):
    import archie_agent.harness as harness_module

    captured = {}
    real = harness_module.create_task_tool

    def spy(**kwargs):
        captured["tool_policy"] = kwargs["tool_policy"]
        return real(**kwargs)

    monkeypatch.setattr(harness_module, "create_task_tool", spy)
    harness = _harness(tmp_path, {"notion": {}})
    assert captured["tool_policy"] == harness._current_tool_policy
