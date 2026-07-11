"""Command palette provider for Archie.

Textual's built-in CommandPalette is triggered by Ctrl+P. We provide a
custom Provider that surfaces model switching and app commands.

The Provider class implements two methods:
- discover(): yields all commands (shown when palette opens with no query)
- search(): filters commands as the user types (fuzzy matching via matcher)
"""

from __future__ import annotations

from functools import partial

from archie_shared.models import load_models
from textual.command import DiscoveryHit, Hit, Hits, Provider

# Module-level cache — catalog is static for the session lifetime
_catalog_cache: dict | None = None


def _get_catalog() -> dict:
    """Return the model catalog, caching on first call."""
    global _catalog_cache
    if _catalog_cache is None:
        _catalog_cache = load_models()
    return _catalog_cache


class ModelProvider(Provider):
    """Textual command palette provider for model switching and app commands."""

    async def discover(self) -> Hits:
        """Yield all commands — shown when the palette first opens."""
        catalog = _get_catalog()
        for key in sorted(catalog):
            model = catalog[key]
            help_text = f"${model.cost.input:.2f}/${model.cost.output:.2f} per M tokens"
            yield DiscoveryHit(
                f"Change Model → {model.name}",
                partial(self._switch, key),
                help=help_text,
            )
        yield DiscoveryHit(
            "Quit",
            self._quit,
            help="Exit Archie",
        )

    async def search(self, query: str) -> Hits:
        """Yield matching commands from the catalog."""
        matcher = self.matcher(query)
        catalog = _get_catalog()

        for key in sorted(catalog):
            model = catalog[key]
            label = f"Change Model → {model.name}"
            score = matcher.match(label)
            if score > 0:
                help_text = f"${model.cost.input:.2f}/${model.cost.output:.2f} per M tokens"
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
        """Callback invoked when a model is selected from the palette."""
        self.app.switch_model(model_key)

    async def _quit(self) -> None:
        """Callback for the Quit command."""
        await self.app.action_quit()
