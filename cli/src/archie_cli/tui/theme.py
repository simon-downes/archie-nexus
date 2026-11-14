"""Unified colour theme for the Archie TUI.

All colour values are defined once as module constants. The Textual Theme
references these constants, making them available as $variables in TCSS.
Rich markup imports the constants via `from archie_cli.tui import theme` and
accesses them as `theme.PRIMARY`, `theme.MUTED`, etc.
"""

from textual.theme import Theme

# --- Semantic colours (Textual builtin slots) ---

PRIMARY = "#6871ff"  # accent — focus, model name, assistant header
SECONDARY = "#ff76ff"  # user messages
WARNING = "#fefb67"  # context 60-85%
ERROR = "#ff6d67"  # context >85%, errors
SUCCESS = "#67c26d"  # success indicators
SURFACE = "#1e1e1e"  # chrome background

# --- Custom colours (available as $variables in TCSS) ---

MUTED = "#676767"  # de-emphasised text (captions, separators)
BRIGHT = "#feffff"  # emphasised values (context %)
POSITIVE = "#67c26d"  # positive values (input tokens)
POSITIVE_BRIGHT = "#5ff967"  # bright positive (output tokens)
COST = "#c7c400"  # monetary values
ANSWER = "#5fd7d7"  # question option labels
PANE = "#171717"  # the session pane's transcript: a step darker than the surface
BAND = "#313356"  # sidebar workspace bands: the primary at about 25% over the surface
TARGET = "#9a9a9a"  # what a row acts on (repo names, tool targets): between muted and normal
TOOL_NAME = "#5fafff"  # tool names on tool rows: bright blue, from the xterm-256 range of ANSWER
LINK = "#5fafff"  # the sidebar's web link

# --- Textual Theme instance ---

THEME = Theme(
    name="archie",
    primary=PRIMARY,
    secondary=SECONDARY,
    warning=WARNING,
    error=ERROR,
    success=SUCCESS,
    surface=SURFACE,
    dark=True,
    variables={
        "muted": MUTED,
        "bright": BRIGHT,
        "positive": POSITIVE,
        "positive-bright": POSITIVE_BRIGHT,
        "cost": COST,
        "answer": ANSWER,
        "pane": PANE,
        "band": BAND,
        "target": TARGET,
        "tool-name": TOOL_NAME,
        "link": LINK,
    },
)
