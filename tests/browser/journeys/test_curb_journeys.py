"""The parts of the Curb section that a request alone cannot check (Curb PRD §11.3).

A review that opens over the page, a phone-width layout, and the same
review with scripts off. Contrast and sideways scrolling are checked with
every other page, in `test_accessibility.py`.

The pages show whatever agents the machine running the suite has, or none.
So nothing here asserts on an agent: only on what every state shares.
"""

from __future__ import annotations

import re

from playwright.sync_api import Browser, Page, expect

CURB_PAGES = (
    "/curb",
    "/curb/machine/agents",
    "/curb/machine/leaks",
    "/curb/machine/fixes",
    "/curb/machine/tests",
    "/curb/machine/activity",
    "/curb/projects/reach",
    "/curb/projects/ci",
    "/curb/projects/apps",
    "/curb/projects/commits",
    "/curb/team/policy",
    "/curb/team/devices",
    "/curb/team/alerts",
)

#: The four scope tabs inside the viewport, and each control too short for a thumb.
PHONE_JS = r"""
() => {
  const width = document.documentElement.clientWidth;
  const tabs = [...document.querySelectorAll('.curb-areas .tab')]
    .map(t => t.getBoundingClientRect());
  const controls = '.curb a.btn, .curb button.btn, .curb .segmented a, .curb select, '
    + '.curb input.input, .curb .row-more > summary, .curb-areas .tab, .reveal-btn';
  const short = [];
  for (const el of document.querySelectorAll(controls)) {
    const r = el.getBoundingClientRect();
    if (r.width > 0 && r.height < 39.5) {
      const name = el.textContent || el.getAttribute('aria-label') || el.className;
      short.push([name.trim().slice(0, 30), Math.round(r.height)]);
    }
  }
  return {
    tabs: tabs.length,
    in_view: tabs.every(r => r.left >= 0 && r.right <= width + 1 && r.width > 0),
    short,
  };
}
"""


def test_a_review_opens_over_the_page_and_cancel_leaves_nothing_behind(page: Page) -> None:
    page.goto("/curb/machine/leaks")
    page.get_by_role("link", name="Show names and locations").first.click()

    dialog = page.locator("dialog[data-curb-dialog]")
    expect(dialog).to_be_visible()
    assert page.evaluate("document.querySelector('dialog[data-curb-dialog]').matches(':modal')")
    # Over the page, not a new one: the address has not moved.
    assert page.url.endswith("/curb/machine/leaks")

    dialog.locator("[data-curb-close]").click()
    expect(dialog).to_have_count(0)
    assert page.url.endswith("/curb/machine/leaks")


def test_on_a_phone_every_scope_is_in_view_and_every_control_fits_a_thumb(page: Page) -> None:
    page.set_viewport_size({"width": 320, "height": 700})
    wrong: list[str] = []
    for path in CURB_PAGES:
        page.goto(path, wait_until="domcontentloaded")
        measured = page.evaluate(PHONE_JS)
        if measured["tabs"] != 4 or not measured["in_view"]:
            wrong.append(f"{path}: not all four scope tabs are in view")
        wrong += [f"{path}: “{name}” is {height}px tall" for name, height in measured["short"]]
    assert not wrong, "at 320px:\n" + "\n".join(wrong)


def test_with_scripts_off_a_review_is_the_same_page_with_the_review_open(
    browser: Browser, server: str
) -> None:
    context = browser.new_context(base_url=server, java_script_enabled=False)
    try:
        page = context.new_page()
        page.goto("/curb")
        page.get_by_role("link", name="Show names and locations").first.click()

        expect(page).to_have_url(re.compile(r"/curb\?review=reveal$"))
        dialog = page.locator("dialog[data-curb-dialog][open]")
        expect(dialog).to_be_visible()
        expect(dialog.locator("[data-curb-close]")).to_have_attribute("href", "/curb")
    finally:
        context.close()
