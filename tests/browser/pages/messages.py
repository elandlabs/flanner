"""The Messages page: a list of chats beside one chat, and a review before every send."""

from __future__ import annotations

import re

from playwright.sync_api import Locator, Page, expect


class Messages:
    def __init__(self, page: Page, base_url: str) -> None:
        self.page = page
        self.base_url = base_url

    def open(self, path: str = "/mesh/messages") -> None:
        self.page.goto(self.base_url + path, wait_until="domcontentloaded")

    # --- the list -----------------------------------------------------------

    @property
    def chats(self) -> Locator:
        return self.page.get_by_role("navigation", name="Chats")

    def row(self, title: str) -> Locator:
        """A chat by its title exactly, so `@ben` is not also `@ben, @chen`."""
        name = self.page.locator(".name", has_text=re.compile(rf"^{re.escape(title)}$"))
        return self.chats.locator("a.chat-row").filter(has=name)

    def section(self, label: str) -> Locator:
        heading = self.page.locator("h2.rail-group", has_text=re.compile(rf"^{label}\b"))
        return self.chats.locator(".chat-sect").filter(has=heading)

    @property
    def filter(self) -> Locator:
        return self.page.get_by_label("Filter chats")

    # --- one chat -----------------------------------------------------------

    @property
    def pane(self) -> Locator:
        return self.page.locator("section.inbox-main")

    @property
    def title(self) -> Locator:
        return self.pane.locator("h2.page-title")

    def open_chat(self, title: str) -> None:
        """Click a chat. The app swaps the page in place, so wait for its title."""
        self.row(title).click()
        expect(self.title).to_contain_text(title)

    def quoted(self, text: str) -> Locator:
        """A teammate's words: always a quote, never the app speaking."""
        return self.pane.locator("blockquote.msg-quote", has_text=text)

    def mine(self, text: str) -> Locator:
        return self.pane.locator(".msg-line.mine", has_text=text)

    # --- writing ------------------------------------------------------------

    @property
    def composer(self) -> Locator:
        return self.page.locator("#reply-body")

    def review(self, body: str) -> None:
        """Write in the chat's composer and ask to review it."""
        self.composer.fill(body)
        self.page.get_by_role("button", name="Review and send").click()

    @property
    def preview(self) -> Locator:
        heading = self.page.get_by_role("heading", name="Send this?")
        return self.page.locator(".card").filter(has=heading)

    def send(self) -> None:
        """The plain yes on the review card."""
        self.preview.get_by_role("button", name="Send", exact=True).click()

    @property
    def said(self) -> Locator:
        return self.page.locator(".notice.good")

    # --- muting -------------------------------------------------------------

    def mute(self, handle: str) -> None:
        self.page.locator("details.chat-menu summary").click()
        self.page.get_by_label("For").select_option(label="Until I unmute")
        self.page.get_by_role("button", name=f"Mute @{handle}").click()

    def unmute(self, handle: str) -> None:
        self.page.locator("details.chat-menu summary").click()
        self.page.get_by_role("button", name=f"Unmute @{handle}").click()
