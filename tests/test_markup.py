"""archie_shared.markup: escaping and parsing agree with Rich, the TUI's renderer."""

import random

import pytest
from archie_shared.markup import escape, parse, plain_text
from archie_shared.tool_summaries import format_tool_complete, format_tool_pending
from archie_shared.transcript import ToolNode, tool_headline
from rich.style import Style
from rich.text import Text

TEMPLATES = ("[bold]{}[/]", "[bold]{}[/] after", "x [dim]{}[/]\n[bold]{}[/]")
PLAIN_TEMPLATES = ("{}", "{} after", "a {} [bold]b[/]")

CASES = [
    "[slug]",
    r"\[slug\]",
    r"tenants/\\[slug\\]/a.ts",
    "[1, 2]",
    "a]b",
    "end\\",
    "\\",
    r"C:\dir\[x]",
    r"\\[bold]",
    "[/]",
    "[",
    "]",
    r"a\b",
    "[#fff]x",
    "[@click]",
    "list[str]",
    "[exit: 1]",
]


def _random_cases(count: int) -> list[str]:
    rnd = random.Random(1234)
    alphabet = "ab[]\\/#@ 1x"
    return ["".join(rnd.choices(alphabet, k=rnd.randint(1, 14))) for _ in range(count)]


def _render(template: str, text: str, tag_follows: bool = True) -> Text:
    return Text.from_markup(template.replace("{}", escape(text, tag_follows=tag_follows)))


def _expected(template: str, text: str) -> str:
    return (template.replace("[bold]", "").replace("[dim]", "").replace("[/]", "")).replace(
        "{}", text
    )


@pytest.mark.parametrize("text", CASES + _random_cases(1000))
def test_escape_round_trips_through_rich(text):
    for template in TEMPLATES:
        rendered = _render(template, text)
        assert rendered.plain == _expected(template, text), (template, text)
        assert {str(span.style) for span in rendered.spans} <= {"bold", "dim"}
    for template in PLAIN_TEMPLATES:
        rendered = _render(template, text, tag_follows=False)
        assert rendered.plain == _expected(template, text), (template, text)
        assert {str(span.style) for span in rendered.spans} <= {"bold"}


@pytest.mark.parametrize("text", CASES + _random_cases(1000))
def test_parse_matches_rich(text):
    for template in TEMPLATES + PLAIN_TEMPLATES:
        markup = template.replace("{}", escape(text, tag_follows=template in TEMPLATES))
        rendered = Text.from_markup(markup)
        assert plain_text(markup) == rendered.plain
        # Unescaped text too: parse reads raw strings the way Rich does.
        try:
            raw = Text.from_markup(template.replace("{}", text))
        except Exception:  # noqa: BLE001 — Rich rejects unmatched closing tags
            continue
        assert plain_text(template.replace("{}", text)) == raw.plain


def test_parse_reports_nested_styles():
    assert parse("a [bold]b [dim]c[/] d[/] e") == [
        ("a ", ()),
        ("b ", ("bold",)),
        ("c", ("bold", "dim")),
        (" d", ("bold",)),
        (" e", ()),
    ]


def test_shell_command_with_escaped_brackets_renders_as_valid_styles():
    """Regression: `rg ... \\[slug\\]` produced a span styled 'slug\\\\' and crashed Textual."""
    command = r'rg -n "x" app/api/tenants/\\[slug\\]/storefront-api-key.post.ts | head -5'
    tool = ToolNode("t", "t", "shell", {"command": command}, status="ok", result="[exit: 0]\n")
    for markup in (
        tool_headline(tool),
        format_tool_pending("shell", {"command": command}),
        format_tool_complete("shell", {"command": command}, "[exit: 0]\n", False, 5),
    ):
        rendered = Text.from_markup(markup)
        assert command in rendered.plain
        for span in rendered.spans:
            Style.parse(str(span.style))  # raises for an invalid style


def test_tui_markup_falls_back_to_plain_text_for_invalid_styles():
    from archie_cli.tui.transcript_view import safe_markup

    markup = r"[bold]x[/] \\[slug\\] y"  # Rich reads a style named "slug\\"
    rendered = safe_markup(markup)
    assert rendered.spans == []
    assert rendered.plain == plain_text(markup) == Text.from_markup(markup).plain
