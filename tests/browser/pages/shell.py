"""The frame every page is inside: the rail, the topbar and the palette."""

from __future__ import annotations

from playwright.sync_api import Locator, Page


class Shell:
    def __init__(self, page: Page) -> None:
        self.page = page

    # --- navigation ---------------------------------------------------------

    def nav(self, name: str) -> Locator:
        return self.page.get_by_role("navigation", name="Main").get_by_role("link", name=name)

    def go(self, name: str) -> None:
        """Click a rail link.

        Deliberately no `expect_navigation`: the app fetches the next page and
        swaps the shell, so there is no document load to wait for. The caller
        waits for content.
        """
        self.nav(name).click()

    @property
    def heading(self) -> Locator:
        return self.page.get_by_role("heading", level=1)

    @property
    def main(self) -> Locator:
        return self.page.get_by_role("main")

    # --- command palette ----------------------------------------------------

    @property
    def palette(self) -> Locator:
        return self.page.get_by_role("dialog", name="Search projects and plans")

    @property
    def palette_input(self) -> Locator:
        return self.page.get_by_role("combobox")

    def open_palette(self) -> None:
        self.page.keyboard.press("Control+k")

    def palette_result(self, name: str) -> Locator:
        return self.page.get_by_role("option").filter(has_text=name)

    # --- notices ------------------------------------------------------------

    def notice(self, text: str) -> Locator:
        """A flash message. Assert on it promptly: the good ones retire at 5s."""
        return self.page.get_by_role("status").filter(has_text=text)
