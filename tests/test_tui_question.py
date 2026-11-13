"""TUI pending-question panel: display, keys, free text, Esc and state from session_status.

Question and turn state are asserted on ``SessionController`` with a recording
listener; the panel, keys and input on the host app over the fake client.
"""

import asyncio
from unittest.mock import AsyncMock

import pytest
from archie_cli.tui.app import ArchieApp
from archie_cli.tui.input import MessageInput
from archie_cli.tui.question import QuestionPanel
from archie_shared.commands import AnswerCommand
from archie_shared.events import Handshake, PendingQuestion, QuestionOption, SessionStatus
from textual.widgets import Static

from tests.tui_fakes import FakeOrchestrator, local_texts, make_controller

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


def _handshake(version: int = 4) -> Handshake:
    return Handshake(id=f"h{version}", protocol_version=version, session_id="s")


# --- Controller -------------------------------------------------------------------


def test_blocked_status_sets_question_and_joins_the_turn():
    controller, listener = make_controller()
    controller.apply(_handshake())
    controller.apply(_status("blocked", QUESTION, "s1"))

    assert controller.question == QUESTION
    assert controller.turn_active
    assert not controller.input_locked  # the input takes the answer
    assert not controller.thinking
    assert listener.names()[-2:] == ["turn", "question"]


def test_status_without_question_clears_it_and_idle_ends_the_turn():
    controller, listener = make_controller()
    controller.apply(_handshake())
    controller.apply(_status("blocked", QUESTION, "s1"))
    controller.apply(_status("working", None, "s2"))
    assert controller.question is None
    assert controller.turn_active and controller.input_locked

    controller.apply(_status("idle", None, "s3"))
    assert not controller.turn_active and not controller.input_locked
    assert controller.turns_ended == 1
    assert listener.names().count("question") == 2


def test_working_status_marks_turn_active_after_reconnect():
    controller, _ = make_controller()
    controller.apply(_handshake())
    controller.apply(_status("working", None, "s1"))
    assert controller.turn_active and controller.input_locked


def test_old_agent_status_does_not_end_the_turn():
    """A protocol-3 agent sends session_status without state; it decodes as idle."""
    controller, _ = make_controller()
    controller.apply(_handshake(version=3))
    controller.turn_active = True
    controller.apply(SessionStatus(id="old", model_key="m", git_branch="main"))
    assert controller.turn_active
    assert controller.git_branch == "main"


@pytest.mark.asyncio
async def test_answer_is_sent_once_and_clears_the_question():
    client = FakeOrchestrator()
    controller, _ = make_controller(client)
    await controller.start()
    controller.apply(_handshake())
    controller.apply(_status("blocked", QUESTION, "s1"))

    command = AnswerCommand(question_id="tu_q", option=1)
    controller.answer(command)
    controller.answer(command)  # in flight: not sent twice
    assert controller.answer_in_flight == "tu_q"
    for _ in range(5):
        await asyncio.sleep(0)

    assert client.latest.sent == [("command", command)]
    assert controller.question is None and controller.answer_in_flight is None
    await controller.close()


@pytest.mark.asyncio
async def test_answer_send_failure_keeps_the_question_for_a_retry():
    client = FakeOrchestrator()
    controller, _ = make_controller(client)
    await controller.start()
    client.latest.send_command = AsyncMock(side_effect=RuntimeError("socket closed"))
    controller.apply(_handshake())
    controller.apply(_status("blocked", QUESTION, "s1"))

    controller.answer(AnswerCommand(question_id="tu_q", option=0))
    for _ in range(5):
        await asyncio.sleep(0)

    assert controller.question == QUESTION
    assert controller.answer_in_flight is None
    assert local_texts(controller) == ["Send failed: socket closed"]
    await controller.close()


@pytest.mark.asyncio
async def test_esc_dismisses_the_question_before_interrupting():
    client = FakeOrchestrator()
    controller, _ = make_controller(client)
    await controller.start()
    controller.apply(_handshake())
    controller.apply(_status("blocked", QUESTION, "s1"))
    assert controller.cancel() == "dismiss"
    assert controller.answer_in_flight == "tu_q"
    for _ in range(5):
        await asyncio.sleep(0)
    assert client.latest.sent == [("command", AnswerCommand(question_id="tu_q", dismiss=True))]
    assert controller.cancel() == "interrupt"  # the turn is still running
    await asyncio.sleep(0)
    assert client.latest.sent[-1] == ("interrupt", None)
    await controller.close()


# --- Host app ---------------------------------------------------------------------


def _app() -> ArchieApp:
    return ArchieApp(
        ws_url="ws://127.0.0.1:7600/api/sessions/s/stream",
        api_url="http://127.0.0.1:7600/api/sessions/s",
        container_name="archie-s",
        client=FakeOrchestrator(),
    )


async def _blocked(app, pilot):
    await pilot.pause()  # the session connects on mount
    app._apply_event(_handshake())
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
        assert app.session.turn_active


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
    async with app.run_test(size=(100, 30)) as pilot:
        panel = await _blocked(app, pilot)
        app._ws.send_command = AsyncMock(side_effect=RuntimeError("socket closed"))
        await pilot.press("1")
        await pilot.pause()
        assert panel.display
        assert app.session.question is not None
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
        assert app.query_one("#input", MessageInput).disabled
        app._apply_event(_status("idle", None, "s3"))
        await pilot.pause()
        assert not app.query_one("#input", MessageInput).disabled


@pytest.mark.asyncio
async def test_working_status_lets_esc_interrupt():
    app = _app()
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        app._apply_event(_handshake())
        app._apply_event(_status("working", None, "s1"))
        await pilot.pause()
        assert app.query_one("#input", MessageInput).disabled
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
        await pilot.pause()
        app._apply_event(_handshake())
        app._apply_event(_status("blocked", question, "s1"))
        await pilot.pause()
        panel = app.query_one("#question", QuestionPanel)
        for static in panel.query(Static):
            assert static.size.width <= 120
        description = panel.query_one(".option-description", Static)
        assert description.size.height > 1
