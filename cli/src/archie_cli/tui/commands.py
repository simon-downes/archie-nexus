"""Command palette providers: session actions (ctrl+p) and the model picker (alt+m).

``SessionCommands`` is the palette: actions only, acting on the session on screen (084
D22, D23). The sidebar navigates and the palette acts, so there are no "switch to
session" entries, and the app lists only these providers (Textual's system commands
are left out). Rename, Change model, Compact and Stop are hidden while no session is on
screen or it has ended; Compact also for Kiro sessions, which compact by themselves
(D24), and Change model and Compact while the session is still starting (nothing
receives them yet).

``ModelCommands`` is the model picker: the palette with only the session's models, one
line per model with aligned ``$in / $out`` prices per million tokens and a tick on the
current one. Kiro sessions list the allowlisted Kiro models (077 D25), priced in Kiro
credits.

Both reach the app through typed protocols: ``AppActions`` for the palette and its
part ``ModelActions`` for the picker.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from functools import partial
from typing import Protocol, runtime_checkable

from archie_shared.kiro import kiro_model_id, kiro_model_key
from archie_shared.models import ModelEntry, load_models
from archie_shared.session import SessionDescriptor
from textual.app import App
from textual.command import DiscoveryHit, Hit, Hits, Provider
from textual.content import Content

from archie_cli.tui import theme
from archie_cli.tui.client import ProfileInfo

MODEL_PLACEHOLDER = "Change model · type to filter · $ per M input / output tokens"
"""The model picker's prompt."""

KIRO_PRICE = "Kiro credits"


@runtime_checkable
class ModelActions(Protocol):
    """What the model picker needs from the app."""

    @property
    def session_model_key(self) -> str:
        """The on-screen session's model key, or ``""`` before it reports one."""
        ...

    def switch_model(self, model_key: str) -> None:
        """Ask the on-screen session to switch to ``model_key``."""
        ...


@runtime_checkable
class AppActions(ModelActions, Protocol):
    """What the command palette needs from the app."""

    profile: ProfileInfo

    @property
    def on_screen(self) -> SessionDescriptor | None:
        """The record of the session on screen, or None."""
        ...

    def action_new_session(self) -> None:
        """Open the new-session dialog."""
        ...

    def action_rename_session(self) -> None:
        """Open the rename dialog for the on-screen session."""
        ...

    def action_change_model(self) -> None:
        """Open the model picker."""
        ...

    def action_compact(self) -> None:
        """Ask the on-screen session to compact its context."""
        ...

    def action_stop_session(self) -> None:
        """Open the stop dialog for the on-screen session."""
        ...

    def action_open_web(self) -> None:
        """Open the orchestrator's web interface."""
        ...

    def action_keys(self) -> None:
        """Show the keyboard shortcuts."""
        ...

    def action_quit(self) -> Awaitable[None]:
        """Open the quit dialog."""
        ...


def app_actions(app: App) -> AppActions:
    """``app`` as ``AppActions``.

    Raises:
        TypeError: The app does not provide the actions.
    """
    if not isinstance(app, AppActions):
        raise TypeError(f"{type(app).__name__} does not provide the palette's actions")
    return app


def model_actions(app: App) -> ModelActions:
    """``app`` as ``ModelActions``.

    Raises:
        TypeError: The app does not provide them.
    """
    if not isinstance(app, ModelActions):
        raise TypeError(f"{type(app).__name__} does not provide the model picker's actions")
    return app


# --- Session actions --------------------------------------------------------------------


@dataclass(frozen=True)
class Command:
    """One palette entry: its name, the help line under it and what it runs."""

    name: str
    help: str
    run: Callable[[], object]


def session_commands(actions: AppActions) -> list[Command]:
    """The palette's entries for the session on screen, in display order."""
    record = actions.on_screen
    commands = [
        Command(
            "New session…", "Start a session in a workspace  ctrl+n", actions.action_new_session
        )
    ]
    if record is not None and not record.ended:
        commands.append(
            Command(
                "Rename session…", "The name shown in the sidebar", actions.action_rename_session
            )
        )
        if record.status != "starting":
            model = model_display_name(actions.session_model_key or record.model_key or "")
            commands.append(
                Command("Change model…", f"{model}  alt+m", actions.action_change_model)
            )
            if record.backend != "kiro":
                commands.append(
                    Command(
                        "Compact context",
                        "Summarise the conversation to free context",
                        actions.action_compact,
                    )
                )
        commands.append(
            Command(
                "Stop session…",
                "Stop this session's container; its log is kept",
                actions.action_stop_session,
            )
        )
    commands += [
        Command("Open web interface", actions.profile.web_url, actions.action_open_web),
        Command("Keyboard shortcuts", "f1", actions.action_keys),
        Command("Quit", "Close this window; sessions keep running  ctrl+q", actions.action_quit),
    ]
    return commands


class SessionCommands(Provider):
    """The command palette (ctrl+p): actions on the app and the on-screen session."""

    def _commands(self) -> list[Command]:
        return session_commands(app_actions(self.app))

    async def discover(self) -> Hits:
        """Every entry, in order, for the empty palette."""
        for command in self._commands():
            yield DiscoveryHit(command.name, command.run, text=command.name, help=command.help)

    async def search(self, query: str) -> Hits:
        """Entries whose name matches ``query``."""
        matcher = self.matcher(query)
        for command in self._commands():
            score = matcher.match(command.name)
            if score > 0:
                yield Hit(
                    score,
                    matcher.highlight(command.name),
                    command.run,
                    text=command.name,
                    help=command.help,
                )


# --- Models -------------------------------------------------------------------------------

_catalog_cache: dict[str, ModelEntry] | None = None


def _get_catalog() -> dict[str, ModelEntry]:
    """The token-priced model catalog (read once)."""
    global _catalog_cache
    if _catalog_cache is None:
        _catalog_cache = load_models()
    return _catalog_cache


def _kiro_models() -> tuple[str, ...]:
    """``kiro.models`` from this host's config (defaults if the config is invalid)."""
    from archie_shared.schemas import KiroConfig, load_nexus_config

    try:
        return load_nexus_config().kiro.models
    except Exception:  # noqa: BLE001 — the picker must open even with a bad config
        return KiroConfig().models


def model_price(model: ModelEntry) -> str:
    """``$ 3.00 / $15.00``: the base input and output price per M tokens, aligned.

    ``free`` for a model with zero prices; `` tiered`` follows when the price changes
    with the prompt's size.
    """
    rates = model.cost.rates
    if not rates:
        return ""
    rate = next((r for r in rates if r.criteria == ("total_input", "min", 0)), rates[0])
    if not rate.input and not rate.output:
        return "free"
    tiered = " tiered" if len(rates) > 1 else ""
    return f"{f'${rate.input:.2f}':>6} / {f'${rate.output:.2f}':>6}{tiered}"


def model_display_name(model_key: str) -> str:
    """``Claude Sonnet 4.6``, ``claude-opus-5.5 (Kiro)``, the key itself, or ``—``."""
    if not model_key:
        return "—"
    kiro_id = kiro_model_id(model_key)
    if kiro_id is not None:
        return f"{kiro_id} (Kiro)"
    model = _get_catalog().get(model_key)
    return model.name if model is not None else model_key


@dataclass(frozen=True)
class ModelChoice:
    """One model in the picker."""

    key: str
    name: str
    price: str


def model_choices(session_model_key: str) -> list[ModelChoice]:
    """The models a session can switch to (077 D25).

    Kiro sessions (``kiro/`` keys) list the allowlisted Kiro models; other sessions list
    the token-priced catalog, by key.
    """
    if kiro_model_id(session_model_key) is not None:
        return [ModelChoice(kiro_model_key(m), f"{m} (Kiro)", KIRO_PRICE) for m in _kiro_models()]
    catalog = _get_catalog()
    return [
        ModelChoice(key, catalog[key].name, model_price(catalog[key])) for key in sorted(catalog)
    ]


def model_line(choice: ModelChoice, name: Content, width: int, current: bool) -> Content:
    """``✓ name   $ 3.00 / $15.00``: a tick on the current model, prices in a column."""
    tick = Content.styled("✓ " if current else "  ", theme.SUCCESS)
    gap = " " * (max(0, width - len(choice.name)) + 3)
    return Content.assemble(tick, name, gap, Content.styled(choice.price, theme.COST))


class ModelCommands(Provider):
    """The model picker (alt+m): picking a model switches the on-screen session."""

    def _choices(self) -> tuple[ModelActions, str, list[ModelChoice]]:
        actions = model_actions(self.app)
        current = actions.session_model_key
        return actions, current, model_choices(current)

    async def discover(self) -> Hits:
        """Every model, the current one ticked and bold."""
        actions, current, choices = self._choices()
        width = max((len(c.name) for c in choices), default=0)
        for choice in choices:
            is_current = choice.key == current
            name = Content.styled(choice.name, "bold") if is_current else Content(choice.name)
            yield DiscoveryHit(
                model_line(choice, name, width, is_current),
                partial(actions.switch_model, choice.key),
                text=choice.name,
            )

    async def search(self, query: str) -> Hits:
        """Models whose name matches ``query``."""
        actions, current, choices = self._choices()
        matcher = self.matcher(query)
        width = max((len(c.name) for c in choices), default=0)
        for choice in choices:
            score = matcher.match(choice.name)
            if score > 0:
                yield Hit(
                    score,
                    model_line(
                        choice, matcher.highlight(choice.name), width, choice.key == current
                    ),
                    partial(actions.switch_model, choice.key),
                    text=choice.name,
                )
