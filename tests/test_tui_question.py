"""TUI pending-question panel: display, keys, free text, Esc and state from session_status."""

from unittest.mock import AsyncMock

import pytest
from archie_cli.tui.app import ArchieApp
from archie_cli.tui.input import MessageInput
from archie_cli.tui.question import QuestionPanel
from archie_shared.commands import AnswerCommand
from archie_shared.events import Handshake, PendingQuestion, QuestionOption, SessionStatus
from textual.widgets import Static

QUESTION = PendingQuestion(
    id="tu_q",
    question="Which database?",
    options=[
        QuestionOption(label="Postgres"),
        QuestionOption(label="SQLite", description="Zero setup"),
        QuestionOption(label="MySQL"),
    ],
    recommended=0,
)


def _status(
    state: str, question: PendingQuestion | None = None, event_id: str = "s"
) -> SessionStatus:
    return SessionStatus(
        id=event_id, model_key="m", git_branch="main", state=state, question=question
    )


def _app() -> ArchieApp:
    app = ArchieApp(
        ws_url="ws://127.0.0.1:7600/sessions/s/stream",
        api_url="http://127.0.0.1:7600/sessions/s",
        container_name="archie-s",
    )
    app._ws.connect = AsyncMock(side_effect=OSError("offline"))
    app._ws.send_command = AsyncMock()
    app._ws.send_interrupt = AsyncMock()
    app._ensure_connected = AsyncMock(return_value=True)
    return app


def _handshake(app, version: int = 4) -> None:
    app._apply_event(Handshake(id=f"h{version}", protocol_version=version, session_id="s"))


async def _blocked(app, pilot):
    _handshake(app)
    app._apply_event(_status("blocked", QUESTION, "s1"))
    await pilot.pause()
    return app.query_one("#question", QuestionPanel)


@pytest.mark.asyncio
async def test_panel_shows_question_with_recommended_selected():
    app = _app()
    async with app.run_test(size=(100, 30)) as pilot:
        panel = await _blocked(app, pilot)
        assert panel.display
        assert panel.selected == 0
        text = "\n".join(str(static.render()) for static in panel.query(Static))
        assert "Which database?" in text
        assert "❯ 1. Postgres (recommended)" in text
        assert "  2. SQLite" in text
        assert "Zero setup" in text
        inp = app.query_one("#input", MessageInput)
        assert not inp.disabled
        assert app._turn_active


@pytest.mark.asyncio
async def test_digit_picks_option_and_hides_panel():
    app = _app()
    async with app.run_test(size=(100, 30)) as pilot:
        panel = await _blocked(app, pilot)
        await pilot.press("3")
        await pilot.pause()
        app._ws.send_command.assert_awaited_once_with(AnswerCommand(question_id="tu_q", option=2))
        assert not panel.display
        assert app.query_one("#input", MessageInput).text == ""


@pytest.mark.asyncio
async def test_arrows_and_enter_pick_selected_option():
    app = _app()
    async with app.run_test(size=(100, 30)) as pilot:
        await _blocked(app, pilot)
        await pilot.press("down", "down", "enter")
        await pilot.pause()
        # Recommended is 0; two downs select 2.
        app._ws.send_command.assert_awaited_once_with(AnswerCommand(question_id="tu_q", option=2))


@pytest.mark.asyncio
async def test_typed_text_is_sent_as_answer():
    app = _app()
    async with app.run_test(size=(100, 30)) as pilot:
        await _blocked(app, pilot)
        await pilot.press("C", "o", "c", "k", "r", "o", "a", "c", "h", "1", "enter")
        await pilot.pause()
        app._ws.send_command.assert_awaited_once_with(
            AnswerCommand(question_id="tu_q", text="Cockroach1")
        )


@pytest.mark.asyncio
async def test_first_esc_dismisses_second_interrupts():
    app = _app()
    async with app.run_test(size=(100, 30)) as pilot:
        panel = await _blocked(app, pilot)
        await pilot.press("escape")
        await pilot.pause()
        app._ws.send_command.assert_awaited_once_with(
            AnswerCommand(question_id="tu_q", dismiss=True)
        )
        app._ws.send_interrupt.assert_not_awaited()
        assert not panel.display

        await pilot.press("escape")
        await pilot.pause()
        app._ws.send_interrupt.assert_awaited_once()


@pytest.mark.asyncio
async def test_send_failure_keeps_panel():
    app = _app()
    app._ws.send_command = AsyncMock(side_effect=RuntimeError("socket closed"))
    async with app.run_test(size=(100, 30)) as pilot:
        panel = await _blocked(app, pilot)
        await pilot.press("1")
        await pilot.pause()
        assert panel.display
        assert app._question is not None
        # The user can retry after a failure.
        await pilot.press("1")
        await pilot.pause()
        assert app._ws.send_command.await_count == 2


@pytest.mark.asyncio
async def test_status_without_question_hides_panel_and_idle_ends_turn():
    app = _app()
    async with app.run_test(size=(100, 30)) as pilot:
        panel = await _blocked(app, pilot)
        app._apply_event(_status("working", None, "s2"))
        await pilot.pause()
        assert not panel.display
        assert app._turn_active
        assert app.query_one("#input", MessageInput).disabled
        app._apply_event(_status("idle", None, "s3"))
        await pilot.pause()
        assert not app._turn_active
        assert not app.query_one("#input", MessageInput).disabled


@pytest.mark.asyncio
async def test_working_status_marks_turn_active_after_reconnect():
    app = _app()
    async with app.run_test(size=(100, 30)) as pilot:
        assert not app._turn_active
        _handshake(app)
        app._apply_event(_status("working", None, "s1"))
        await pilot.pause()
        assert app._turn_active
        await pilot.press("escape")
        await pilot.pause()
        app._ws.send_interrupt.assert_awaited_once()


@pytest.mark.asyncio
async def test_history_keys_work_again_after_question():
    app = _app()
    async with app.run_test(size=(100, 30)) as pilot:
        inp = app.query_one("#input", MessageInput)
        inp._history = ["earlier message"]
        inp._history_idx = 1
        panel = await _blocked(app, pilot)
        await pilot.press("up")
        assert inp.text == ""
        assert panel.selected == 2
        await pilot.press("2")
        await pilot.pause()
        app._apply_event(_status("idle", None, "s2"))
        await pilot.pause()
        await pilot.press("up")
        assert inp.text == "earlier message"


@pytest.mark.asyncio
async def test_old_agent_status_does_not_end_the_turn():
    """A protocol-3 agent sends session_status without state; it decodes as idle."""
    app = _app()
    async with app.run_test(size=(100, 30)) as pilot:
        _handshake(app, version=3)
        app._turn_active = True
        app._apply_event(SessionStatus(id="old", model_key="m", git_branch="main"))
        await pilot.pause()
        assert app._turn_active


@pytest.mark.asyncio
async def test_long_descriptions_wrap_at_120_columns():
    long = " ".join(["word"] * 80)
    question = PendingQuestion(
        id="tu_long",
        question=long,
        options=[QuestionOption(label="A", description=long), QuestionOption(label="B")],
        recommended=0,
    )
    app = _app()
    async with app.run_test(size=(200, 60)) as pilot:
        _handshake(app)
        app._apply_event(_status("blocked", question, "s1"))
        await pilot.pause()
        panel = app.query_one("#question", QuestionPanel)
        for static in panel.query(Static):
            assert static.size.width <= 120
        description = panel.query_one(".option-description", Static)
        assert description.size.height > 1
