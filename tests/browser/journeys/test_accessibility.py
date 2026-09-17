"""Contrast, overflow, landmarks and the keyboard — on every seeded page.

This is `scripts/audit_ui.py`, which nobody ran, turned into tests that run
on every push. The script is deleted in the same commit: two ways to answer
the same question is one way too many, and the one that is not in CI rots.
"""

from __future__ import annotations

from typing import Any

import pytest
from playwright.sync_api import Page, expect

from tests.browser import helpers
from tests.browser.pages.shell import Shell

#: Every page the seeded catalog can show. Enumerated rather than crawled:
#: a crawl finds a different set on a different day, and an accessibility
#: gate that changes what it checks is not a gate.
STATIC_PAGES = (
    "/",
    "/projects",
    "/projects/new",
    "/plans",
    "/freshness",
    "/memory",
    "/memory/pending",
    "/skills",
    "/skills/proposals",
    "/mesh",
    "/review",
    "/actions",
    "/settings",
    "/setup",
)

WIDTHS = (1280, 1024, 375)

#: Contrast the app does not currently meet, as `(theme, selector, text)`.
#:
#: A ratchet, like the coverage floor: anything not on this list fails, and
#: anything on it that starts passing fails too, so a fix cannot leave a
#: stale entry behind. Every one is a near miss in the app's own palette
#: rather than a bug in a journey — the small `--text-4` count chips at
#: 3.94 and 4.47, and the accent link on a tint at 4.42. Fixing them is a
#: change to `tokens.css`, which is a colour decision and not this suite's
#: to make. Delete the entry with the fix.
KNOWN_CONTRAST_MISSES = frozenset(
    {
        ("light", "span.note", "3"),
        ("dark", "span.note", "3"),
        ("light", "a", "Review"),
        ("light", "a", "Decide on it"),
    }
)


@pytest.fixture(scope="session")
def seeded_pages(catalog: dict[str, Any]) -> tuple[str, ...]:
    """The static routes, plus one detail page of each kind that has data."""
    projects = [f"/projects/{p['id']}" for p in catalog["projects"].values()]
    plans = [f"/plans/{plan_id}" for plan_id in catalog["plans"].values()]
    memories = [f"/memory/{memory_id}" for memory_id in catalog["memories"].values()]
    return (*STATIC_PAGES, *projects, *plans, *memories)


@pytest.fixture
def every_page(seeded_pages: tuple[str, ...]) -> tuple[str, ...]:
    return seeded_pages


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_every_seeded_page_meets_wcag_aa(
    page: Page, every_page: tuple[str, ...], theme: str
) -> None:
    """Light mode fails in different places from dark, so both are checked."""
    unexpected: list[str] = []
    seen: set[tuple[str, str, str]] = set()
    for path in every_page:
        page.goto(path, wait_until="domcontentloaded")
        helpers.set_theme(page, theme)
        for failure in helpers.contrast_failures(page):
            key = (theme, failure["selector"], failure["text"])
            if key in KNOWN_CONTRAST_MISSES:
                seen.add(key)
                continue
            unexpected.append(
                f"{path} {theme}: {failure['ratio']} < {failure['need']} "
                f"on {failure['selector']} — {failure['text']!r}"
            )
    assert not unexpected, "text that cannot be read:\n" + "\n".join(unexpected)

    fixed = {k for k in KNOWN_CONTRAST_MISSES if k[0] == theme} - seen
    assert not fixed, "these now pass — delete them from KNOWN_CONTRAST_MISSES:\n" + "\n".join(
        str(k) for k in sorted(fixed)
    )


@pytest.mark.parametrize("width", WIDTHS)
def test_no_seeded_page_scrolls_sideways(
    page: Page, every_page: tuple[str, ...], width: int
) -> None:
    """Wide content scrolls inside its own frame. The page never does."""
    page.set_viewport_size({"width": width, "height": 900})
    offenders: list[str] = []
    for path in every_page:
        page.goto(path, wait_until="domcontentloaded")
        measured = helpers.horizontal_overflow(page)
        if measured["overflow"] > 0:
            offenders.append(
                f"{path} @{width}: +{measured['overflow']}px — {', '.join(measured['offenders'])}"
            )
    assert not offenders, "pages that scroll sideways:\n" + "\n".join(offenders)


def test_every_seeded_page_has_one_h1_and_one_main(
    page: Page, shell: Shell, every_page: tuple[str, ...]
) -> None:
    """One landmark and one first-level heading, so a screen reader has a
    place to start and only one of them."""
    wrong: list[str] = []
    for path in every_page:
        page.goto(path, wait_until="domcontentloaded")
        headings = shell.heading.count()
        mains = shell.main.count()
        if headings != 1 or mains != 1:
            wrong.append(f"{path}: {headings} h1, {mains} main")
    assert not wrong, "pages with the wrong landmarks:\n" + "\n".join(wrong)


def test_the_primary_action_of_a_journey_is_reachable_by_keyboard(page: Page) -> None:
    """Tab to "New project" from the top of the page, and see the focus.

    Not a click in disguise: this presses Tab until the accessible name
    matches, which fails if the control is unreachable or out of order.
    """
    page.goto("/projects")
    page.get_by_role("link", name="Skip to content").focus()

    assert helpers.tab_to(page, "New project"), (
        f"never tabbed onto New project; focus ended on {helpers.focused_description(page)}"
    )
    assert helpers.focus_is_visible(page), "the focused control draws no visible ring"

    page.keyboard.press("Enter")
    expect(page.get_by_role("heading", name="New project")).to_be_visible()
