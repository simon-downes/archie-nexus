"""Command palette providers for the prototype.

``SessionCommands`` is the main palette: actions only, scoped to the current session.
The sidebar navigates and the palette acts (roadmap item 4), so there are no
"switch to session" entries, and Textual's system commands are left out.

``ModelCommands`` is the model picker: the same palette opened with only the models,
so typing filters them. One line per model with aligned prices.
"""

from __future__ import annotations

from collections.abc import Callable
from functools import partial

from archie_shared.kiro import kiro_model_id
from archie_shared.models import load_models
from rich.text import Text
from textual.command import DiscoveryHit, Hit, Hits, Provider
from textual.content import Content

from archie_cli.tui import theme
from archie_cli.tui.models_provider import model_entries
from archie_cli.tui.prototype.widgets import model_label

MODEL_PLACEHOLDER = "Change model · type to filter · $ per M input / output tokens"


class SessionCommands(Provider):
    def _commands(self) -> list[tuple[str, str, Callable]]:
        app = self.app
        session = app.current  # type: ignore[attr-defined]
        commands = [
            ("New session…", "ctrl+n", app.action_new_session),  # type: ignore[attr-defined]
        ]
        if session is not None:
            commands.append(
                ("Rename session…", "Name shown in the sidebar", app.action_rename_session)  # type: ignore[attr-defined]
            )
        if session is not None and not session.read_only:
            commands += [
                (
                    "Change model…",
                    f"{model_label(session.model_key)}  alt+m",
                    app.action_change_model,  # type: ignore[attr-defined]
                ),
                (
                    "Compact context",
                    "Summarise the conversation to free context",
                    app.action_compact,  # type: ignore[attr-defined]
                ),
                (
                    "Stop session…",
                    "Stop this session's container; the log is kept",
                    app.action_stop_session,  # type: ignore[attr-defined]
                ),
            ]
        commands += [
            ("Open web interface", app.web_url, app.action_open_web),  # type: ignore[attr-defined]
            ("Keyboard shortcuts", "f1", app.action_keys),  # type: ignore[attr-defined]
            ("Quit", "Close this window; sessions keep running  ctrl+q", app.action_quit),
        ]
        return commands

    async def discover(self) -> Hits:
        for name, help_text, callback in self._commands():
            yield DiscoveryHit(name, callback, help=help_text)

    async def search(self, query: str) -> Hits:
        matcher = self.matcher(query)
        for name, help_text, callback in self._commands():
            score = matcher.match(name)
            if score > 0:
                yield Hit(score, matcher.highlight(name), callback, help=help_text)


def model_price(model_key: str) -> str:
    """``$ 4.00 / $20.00``: base input and output price per M tokens, aligned."""
    if kiro_model_id(model_key) is not None:
        return "Kiro credits"
    model = load_models().get(model_key)
    if model is None or not model.cost.rates:
        return ""
    rate = next(
        (r for r in model.cost.rates if r.criteria == ("total_input", "min", 0)),
        model.cost.rates[0],
    )
    if not rate.input and not rate.output:
        return "free"
    return f"{f'${rate.input:.2f}':>6} / {f'${rate.output:.2f}':>6}"


class ModelCommands(Provider):
    """The session's models, current one ticked; picking one switches the session."""

    def _entries(self) -> list[tuple[str, str, bool]]:
        session = self.app.current  # type: ignore[attr-defined]
        current = session.model_key if session is not None else ""
        return [
            (key, label.removeprefix("Change Model → "), key == current)
            for key, label, _help in model_entries(current)
        ]

    def _line(self, name: Content, width: int, key: str, current: bool) -> Content:
        tick = Content.styled("✓ " if current else "  ", theme.SUCCESS)
        price = Content.from_rich_text(Text(model_price(key), style=theme.COST))
        return Content.assemble(tick, name, " " * (width - len(name.plain) + 3), price)

    async def discover(self) -> Hits:
        entries = self._entries()
        width = max((len(name) for _key, name, _cur in entries), default=0)
        for key, name, current in entries:
            label = Content.styled(name, "bold") if current else Content(name)
            yield DiscoveryHit(
                self._line(label, width, key, current),
                partial(self.app.set_model, key),  # type: ignore[attr-defined]
                text=name,
            )

    async def search(self, query: str) -> Hits:
        matcher = self.matcher(query)
        entries = self._entries()
        width = max((len(name) for _key, name, _cur in entries), default=0)
        for key, name, current in entries:
            score = matcher.match(name)
            if score > 0:
                yield Hit(
                    score,
                    self._line(matcher.highlight(name), width, key, current),
                    partial(self.app.set_model, key),  # type: ignore[attr-defined]
                    text=name,
                )
