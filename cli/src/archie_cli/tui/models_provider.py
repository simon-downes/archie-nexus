"""Command-palette providers for Archie."""

from __future__ import annotations

from functools import partial

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


class ModelProvider(Provider):
    """Textual command palette provider for model switching and app commands."""

    async def discover(self) -> Hits:
        catalog = _get_catalog()
        for key in sorted(catalog):
            model = catalog[key]
            help_text = _cost_summary(model)
            yield DiscoveryHit(
                f"Change Model → {model.name}", partial(self._switch, key), help=help_text
            )
        yield DiscoveryHit("Quit", self._quit, help="Exit Archie")

    async def search(self, query: str) -> Hits:
        matcher = self.matcher(query)
        catalog = _get_catalog()
        for key in sorted(catalog):
            model = catalog[key]
            label = f"Change Model → {model.name}"
            score = matcher.match(label)
            if score > 0:
                yield Hit(
                    score,
                    matcher.highlight(label),
                    partial(self._switch, key),
                    help=_cost_summary(model),
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
