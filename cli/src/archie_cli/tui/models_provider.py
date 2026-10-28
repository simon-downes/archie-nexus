"""Command-palette providers for Archie."""

from __future__ import annotations

from functools import partial

from archie_shared.kiro import kiro_model_id, kiro_model_key
from archie_shared.models import load_models
from textual.command import DiscoveryHit, Hit, Hits, Provider


def _cost_summary(model) -> str:
    rate = next(
        (rate for rate in model.cost.rates if rate.criteria == ("total_input", "min", 0)),
        model.cost.rates[0],
    )
    tiered = " tiered" if len(model.cost.rates) > 1 else ""
    return f"${rate.input:.2f}/${rate.output:.2f} per M tokens{tiered}"


_catalog_cache: dict | None = None


def _get_catalog() -> dict:
    global _catalog_cache
    if _catalog_cache is None:
        _catalog_cache = load_models()
    return _catalog_cache


def _kiro_models() -> tuple[str, ...]:
    """``kiro.models`` from this host's config (defaults if the config is invalid)."""
    from archie_shared.schemas import KiroConfig, load_nexus_config

    try:
        return load_nexus_config().kiro.models
    except Exception:  # noqa: BLE001 — the palette must open even with a bad config
        return KiroConfig().models


def model_entries(session_model_key: str) -> list[tuple[str, str, str]]:
    """``(model_key, label, help)`` palette entries for the session's backend (077 D25).

    Kiro sessions (``kiro/`` keys) list the allowlisted Kiro models; other sessions list
    the token-priced catalog.
    """
    if kiro_model_id(session_model_key) is not None:
        return [
            (kiro_model_key(m), f"Change Model → {m} (Kiro)", "Kiro credits")
            for m in _kiro_models()
        ]
    catalog = _get_catalog()
    return [
        (key, f"Change Model → {catalog[key].name}", _cost_summary(catalog[key]))
        for key in sorted(catalog)
    ]


class ModelProvider(Provider):
    """Textual command palette provider for model switching and app commands."""

    def _entries(self) -> list[tuple[str, str, str]]:
        return model_entries(getattr(self.app, "_session_model_key", "") or "")

    async def discover(self) -> Hits:
        for key, label, help_text in self._entries():
            yield DiscoveryHit(label, partial(self._switch, key), help=help_text)
        yield DiscoveryHit("Quit", self._quit, help="Exit Archie")

    async def search(self, query: str) -> Hits:
        matcher = self.matcher(query)
        for key, label, help_text in self._entries():
            score = matcher.match(label)
            if score > 0:
                yield Hit(
                    score,
                    matcher.highlight(label),
                    partial(self._switch, key),
                    help=help_text,
                )
        quit_score = matcher.match("Quit")
        if quit_score > 0:
            yield Hit(quit_score, matcher.highlight("Quit"), self._quit, help="Exit Archie")

    def _switch(self, model_key: str) -> None:
        self.app.switch_model(model_key)

    async def _quit(self) -> None:
        await self.app.action_quit()


class SubagentProvider(Provider):
    """Command-palette picker for currently known child scopes."""

    async def discover(self) -> Hits:
        for key, child in sorted(self.app._child_activity.items()):
            label = f"Subagent {child.agent} #{child.index} ({child.status})"
            yield DiscoveryHit(label, partial(self._open, key), help=f"${child.cost:.4f}")

    async def search(self, query: str) -> Hits:
        matcher = self.matcher(query)
        for key, child in sorted(self.app._child_activity.items()):
            label = f"Subagent {child.agent} #{child.index} ({child.status})"
            score = matcher.match(label)
            if score > 0:
                yield Hit(score, matcher.highlight(label), partial(self._open, key))

    def _open(self, key: tuple[str, int]) -> None:
        self.app.open_child_detail(key)
