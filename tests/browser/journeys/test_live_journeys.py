"""The parts of the UI that move after first paint.

None of this is reachable through `TestClient`: a stream, a server-sent
event, a shell swapped without a document load, and a dialog behind a hotkey.
"""

from __future__ import annotations

import re
import urllib.parse
import urllib.request
from typing import Any

from playwright.sync_api import Page, expect

from tests.browser.pages.plan import PlanPage
from tests.browser.pages.shell import Shell


def test_the_freshness_scan_settles_on_its_verdicts(page: Page) -> None:
    """Assert the settled state, never an intermediate tally.

    Rows arrive one at a time over NDJSON and the four counts move with
    them, so every number on this page is wrong for a moment by design.
    Playwright's retrying assertions wait for the last one.
    """
    page.goto("/freshness")

    stale_row = page.get_by_role("link", name="webhook-delivery.md")
    expect(stale_row).to_be_visible()
    expect(page.get_by_text("cites 1 reference that no longer exists")).to_be_visible()

    # Hidden again is the scan's own "done": `app.js` clears it on the final
    # line and on nothing else.
    expect(page.locator("[data-scan-progress]")).to_be_hidden()
    expect(page.locator('[data-tally="stale"]')).to_have_text("1")


def test_a_version_written_by_another_client_appears_without_a_reload(
    page: Page, server: str, catalog: dict[str, Any]
) -> None:
    """The SSE feed, end to end.

    The second client is a plain HTTP post with no `Origin`, which is what
    the middleware treats as "not a browser carrying somebody's cookies" —
    the same door the MCP server and a terminal come through.
    """
    plan_id = catalog["plans"]["rate-limiting"]
    plan = PlanPage(page)
    plan.open(plan_id)
    expect(plan.body).to_contain_text("A token bucket per API key.")

    body = urllib.parse.urlencode(
        {
            "content": "# Rate limiting\n\nA leaky bucket per API key.\n",
            "notes": "written by a second client",
        }
    ).encode()
    request = urllib.request.Request(  # noqa: S310
        f"{server}/plans/{plan_id}/edit",
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
        assert response.status == 200

    # No reload, no polling loop in the test: the page hears about it.
    expect(plan.live_banner).to_be_visible()
    expect(plan.live_banner).to_contain_text("Version 2 of this plan arrived")


def test_a_rail_link_swaps_the_shell_and_updates_the_url(page: Page, shell: Shell) -> None:
    """Boosted navigation: asserted on content and on the document surviving.

    Never on a load event — there is no navigation to wait for, which is the
    whole point of the feature.
    """
    page.goto("/")
    page.evaluate("() => { window.__same_document = true; }")

    shell.go("Plans")

    expect(shell.heading).to_have_text("Plans")
    expect(page).to_have_url(re.compile(r"/plans$"))
    assert page.evaluate("() => window.__same_document") is True, (
        "the browser did a full navigation; the shell swap did not happen"
    )

    shell.go("Projects")
    expect(shell.heading).to_have_text("Projects")
    expect(page).to_have_url(re.compile(r"/projects$"))
    assert page.evaluate("() => window.__same_document") is True


def test_the_command_palette_opens_on_the_hotkey_and_navigates(
    page: Page, shell: Shell, catalog: dict[str, Any]
) -> None:
    page.goto("/")
    shell.open_palette()

    expect(shell.palette).to_be_visible()
    shell.palette_input.fill("jwt")
    expect(shell.palette_result("jwt-key-rotation")).to_be_visible()

    page.keyboard.press("Enter")
    expect(page).to_have_url(re.compile(re.escape(catalog["plans"]["jwt-key-rotation"]) + "$"))
    expect(PlanPage(page).body).to_contain_text("Rotate the signing key every 30 days.")
