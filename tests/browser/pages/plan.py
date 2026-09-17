"""Reading a plan, revising it, and its history."""

from __future__ import annotations

from playwright.sync_api import Locator, Page


class PlanPage:
    def __init__(self, page: Page) -> None:
        self.page = page

    def open(self, plan_id: str, *, version: int | None = None) -> None:
        query = f"?version={version}" if version else ""
        self.page.goto(f"/plans/{plan_id}{query}")

    @property
    def body(self) -> Locator:
        """The rendered markdown, not the raw file."""
        return self.page.get_by_role("article")

    @property
    def version_selector(self) -> Locator:
        return self.page.get_by_label("Version", exact=True)

    @property
    def edit(self) -> Locator:
        return self.page.get_by_role("link", name="Edit")

    @property
    def history(self) -> Locator:
        return self.page.get_by_role("link", name="History")

    @property
    def download(self) -> Locator:
        return self.page.get_by_role("link", name="Download")

    @property
    def live_banner(self) -> Locator:
        """What the SSE feed raises when a version arrives from elsewhere."""
        return self.page.get_by_text("of this plan arrived")


class PlanEditPage:
    """The editor. CodeMirror mounts over the textarea, so the textarea the
    template renders is hidden by the time a person can type into it.

    `.CodeMirror` is a CSS class and the suite otherwise refuses those — but
    it is CodeMirror's own class on an element CodeMirror created, not one of
    this app's styling hooks, and there is no role or label on it to use
    instead. Restyling the app cannot break it.
    """

    def __init__(self, page: Page) -> None:
        self.page = page

    def open(self, plan_id: str) -> None:
        self.page.goto(f"/plans/{plan_id}/edit")

    @property
    def editor(self) -> Locator:
        return self.page.locator(".CodeMirror")

    def append(self, text: str) -> None:
        self.editor.click()
        self.page.keyboard.press("Control+End")
        self.page.keyboard.type(text)

    def content(self) -> str:
        typed: str = self.page.evaluate(
            "() => document.querySelector('.CodeMirror').CodeMirror.getValue()"
        )
        return typed

    @property
    def notes(self) -> Locator:
        return self.page.get_by_label("What changed")

    @property
    def save(self) -> Locator:
        return self.page.get_by_role("button", name="Save new version")


class PlanHistoryPage:
    def __init__(self, page: Page) -> None:
        self.page = page

    def open(self, plan_id: str) -> None:
        self.page.goto(f"/plans/{plan_id}/history")

    def version(self, number: int) -> Locator:
        return self.page.get_by_role("link", name=f"v{number}", exact=True)

    @property
    def rows(self) -> Locator:
        return self.page.get_by_role("link", name="v")
