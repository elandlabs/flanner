"""How the command line looks.

One place for the palette, the table shape and the status glyphs, so twelve
commands cannot drift into twelve different looks. Everything here is
presentation: no command should import this for anything but printing.

The colours are the ones in section 1a of the Flanner mockups, which are in
turn the GitHub dark terminal palette. They are given as hex rather than the
sixteen ANSI names on purpose — a terminal's idea of "green" varies wildly by
theme, and freshness is the one thing here that must not be ambiguous.

Rich degrades this on its own: a terminal without truecolor gets the nearest
match, and one without unicode gets ASCII in place of the glyphs.
"""

from __future__ import annotations

from typing import Any

from rich.console import Console
from rich.table import Table
from rich.text import Text
from rich.theme import Theme

# --- palette ------------------------------------------------------------------

THEME = Theme(
    {
        # Structure
        "muted": "#8b949e",
        "label": "#8b949e",
        "value": "bold #e6edf3",
        "accent": "#58a6ff",
        "code": "#7ee787",
        # Outcomes
        "ok": "#3fb950",
        "warn": "#d29922",
        "bad": "#f85149",
        # Freshness, in the vocabulary the whole product uses
        "fresh": "#3fb950",
        "aging": "#d29922",
        "suspect": "#ff8c42",
        "stale": "#f85149",
        "unknown": "#8b949e",
    }
)

console = Console(theme=THEME, highlight=False)

#: `--quiet`. The flag promised "only show errors" and changed nothing a
#: person could see. Quiet drops the chrome - the ✓ lines, the notes, the
#: hints, the spinner - and keeps the data and anything that went wrong,
#: which is what a script wrapping a command wants from it.
QUIET = False


def _encodable(glyph: str) -> bool:
    """Whether this terminal can actually print a character.

    A Windows console still running cp1252 raises UnicodeEncodeError on a
    filled circle, which would crash the command rather than merely look
    plain. Asking the encoder is cheaper than maintaining a list of
    terminals, and it is the thing that actually decides.
    """
    encoding = getattr(console.file, "encoding", None) or "utf-8"
    try:
        glyph.encode(encoding)
    except (UnicodeEncodeError, LookupError):
        return False
    return True


def _glyph(preferred: str, fallback: str) -> str:
    return preferred if _encodable(preferred) else fallback


DOT = _glyph("●", "*")
TICK = _glyph("✓", "+")
CROSS = _glyph("✗", "x")
BANG = "!"
MIDDOT = _glyph("·", "-")
DASH = _glyph("—", "--")
ARROW = _glyph("→", "->")

#: Freshness in the order it degrades, so summaries always read the same way.
FRESHNESS_ORDER = ("fresh", "aging", "suspect", "stale")


# --- tables ---------------------------------------------------------------------

# Borderless. A grid of pipes and plus signs draws the eye to the furniture
# rather than the data, and the mockups have none of it. Deliberately `None`
# rather than a custom Box: Rich substitutes a custom box for an ASCII one on
# a legacy Windows console, which puts the grid straight back.


def table(*columns: str | tuple[str, dict[str, Any]], **kwargs: Any) -> Table:
    """A table in the house style.

    Columns are names, or ``(name, options)`` when one needs alignment or a
    width. Headers are dim and upper case, which reads as a label rather than
    competing with the first row of data.
    """
    built = Table(
        box=None,
        show_header=True,
        header_style="muted",
        border_style="#30363d",
        pad_edge=False,
        padding=(0, 2, 0, 0),
        **kwargs,
    )
    for column in columns:
        if isinstance(column, tuple):
            name, options = column
            built.add_column(name.upper(), **options)
        else:
            built.add_column(column.upper())
    return built


def listing(table: Table, *, footer: str | None = None) -> None:
    """Print a listing, through the pager when it would not fit the screen.

    A list longer than the terminal scrolls off the top before anybody reads
    its first row. When stdout is a terminal and the table is taller than
    it, this pipes the output through the system pager, the way git does
    with a log. Piped or redirected output is printed straight through, so
    scripts see exactly what they always did. ``footer`` is the line that
    says the list was cut short and which flag lifts the cap.
    """
    # Header, the blank lines around the table, the footer, and a line
    # for the prompt: what the terminal has to hold beyond the rows.
    chrome = 6
    fits = not console.is_terminal or table.row_count + chrome <= (console.height or 0)

    def emit() -> None:
        console.print()
        console.print(table)
        if footer:
            console.print(footer, style="muted")
        console.print()

    if fits:
        emit()
        return
    # Plain text in the pager: `more` on Windows and a bare `less` both print
    # escape codes literally, and a listing is read for its rows, not its
    # colours.
    with console.pager(styles=False):
        emit()


def fields(pairs: list[tuple[str, Any]], *, width: int = 14) -> Table:
    """A block of label/value rows, for the status-style screens.

    Two columns with no header: the label dim and fixed width so the values
    line up, the value left to whatever style the caller gave it.
    """
    built = Table(box=None, show_header=False, pad_edge=False, padding=(0, 2, 0, 0))
    built.add_column(style="label", width=width, no_wrap=True)
    built.add_column(overflow="fold")
    for label, value in pairs:
        built.add_row(label, value)
    return built


# --- lines ------------------------------------------------------------------------


def _line(glyph: str, style: str, message: str, indent: int = 0) -> Text:
    text = Text(" " * indent)
    text.append(f"{glyph} ", style=style)
    text.append_text(Text.from_markup(message))
    return text


# `indent` puts a line under the one above it: evidence under a verdict,
# a path under the reason that cites it. Two spaces per level.


def ok(message: str, *, indent: int = 0) -> None:
    """A step that worked."""
    if QUIET:
        return
    console.print(_line(TICK, "ok", message, indent))


def bad(message: str, *, indent: int = 0) -> None:
    """A step that did not."""
    console.print(_line(CROSS, "bad", message, indent))


def warn(message: str, *, indent: int = 0) -> None:
    """Worth knowing, but nothing failed."""
    console.print(_line(BANG, "warn", message, indent))


def note(message: str, *, indent: int = 0) -> None:
    """Context under a result. Dim, because it is never the point."""
    if QUIET:
        return
    console.print(Text.from_markup(f"[muted]{' ' * indent}{message}[/muted]"))


def dot(status: str, *, label: str | None = None) -> Text:
    """A coloured ● and its word, for freshness and for running/stopped."""
    style = status if status in THEME.styles else "unknown"
    text = Text()
    text.append(f"{DOT} ", style=style)
    text.append(label if label is not None else status, style=style)
    return text


def tally(counts: dict[str, int], *, noun: str = "plan") -> Text:
    """`4 plans · 1 fresh · 1 aging · 1 suspect · 1 stale`.

    Only the statuses actually present are listed. A row of zeroes is noise,
    and it makes the one number that matters harder to find.
    """
    total = sum(counts.values())
    text = Text()
    text.append(f"{total} {noun}{'' if total == 1 else 's'}", style="value")
    for status in FRESHNESS_ORDER:
        if counts.get(status):
            text.append(f" {MIDDOT} ", style="muted")
            text.append(f"{counts[status]} {status}", style=status)
    return text


def size(count: int) -> str:
    """Bytes as something a person reads, at one decimal place."""
    if count < 1024:
        return f"{count} B"
    value = float(count)
    for unit in ("KB", "MB", "GB"):
        value /= 1024
        if value < 1024 or unit == "GB":
            return f"{value:.1f} {unit}"
    return f"{count} B"


def hint(message: str) -> None:
    """The next command worth running, offered rather than insisted on."""
    if QUIET:
        return
    console.print(Text.from_markup(f"[muted]{message}[/muted]"))


class Working:
    """A spinner for a command that takes a while, saying how far it has got.

    `flanner freshness` reads git for every plan and took thirty seconds on
    a cold cache, during which the terminal showed nothing. A blank screen
    for that long reads as a hang, and Ctrl-C is what people reach for.

    Only drawn on a terminal: piped or redirected output, and `--json`, see
    nothing. `say(...)` replaces the line; the count is the caller's. The
    Ctrl-C note is on the line because a wait long enough to need a spinner
    is long enough to be abandoned, and nothing these commands do is half
    done if it is.
    """

    def __init__(self, label: str) -> None:
        self._label = label
        self._status: Any = None

    def __enter__(self) -> Working:
        if console.is_terminal and not QUIET:
            self._status = console.status(self._text(self._label), spinner="dots")
            self._status.__enter__()
        return self

    def __exit__(self, *exc: Any) -> None:
        if self._status is not None:
            self._status.__exit__(*exc)

    def say(self, label: str) -> None:
        if self._status is not None:
            self._status.update(self._text(label))

    @staticmethod
    def _text(label: str) -> Text:
        text = Text(label, style="muted")
        text.append(f"  {MIDDOT} Ctrl-C stops; nothing is written", style="muted")
        return text


def working(label: str) -> Working:
    """`with tui.working("judging plans") as w: ... w.say("judging 3 of 23 plans")`."""
    return Working(label)


def command(text: str) -> str:
    """Markup for a command someone could type."""
    return f"[accent]{text}[/accent]"


def code(text: str) -> str:
    """Markup for an identifier: a commit, a path, a symbol."""
    return f"[code]{text}[/code]"
