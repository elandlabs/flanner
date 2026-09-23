"""Messages between teammates, driven in a real browser.

One signed-in device with three invented teammates, seeded by `flanner demo
seed --signed-in`: an unread chat with @ben, a group chat with @ben and
@chen, and a workspace message from @dana, who is muted. Their devices never
answer, so everything sent here is queued, which is what a person sees when
a teammate is offline.

The tests share one server. Only the first reads the seed's counts; every
later one leaves state it does not assert on, or puts back what it changed.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
from playwright.sync_api import Page, expect

from tests.browser import helpers
from tests.browser.pages.messages import Messages


@pytest.fixture
def messages(page: Page, signed_in_server: str) -> Messages:
    return Messages(page, signed_in_server)


def test_the_list_counts_unread_marks_the_muted_and_filters_as_you_type(
    messages: Messages,
) -> None:
    messages.open()

    expect(messages.section("Unread").locator("h2")).to_have_text(
        re.compile(r"Unread 3 · 1 muted")
    )
    expect(messages.row("@ben")).to_contain_text("1 unread")
    expect(messages.row("@ben, @chen")).to_contain_text("2 unread")
    expect(messages.row("@dana")).to_contain_text("muted")
    expect(messages.pane).to_contain_text("3 unread in 2 chats")

    messages.filter.fill("chen")
    expect(messages.row("@ben, @chen")).to_be_visible()
    expect(messages.row("@chen")).to_be_visible()
    expect(messages.row("@ben")).to_be_hidden()
    expect(messages.section("Workspaces")).to_be_hidden()

    messages.filter.fill("")
    expect(messages.row("@ben")).to_be_visible()
    expect(messages.section("Workspaces")).to_be_visible()


def test_j_and_k_move_between_chats_and_e_marks_one_read(messages: Messages, page: Page) -> None:
    messages.open()
    messages.row("@ben").focus()

    page.keyboard.press("j")
    expect(messages.row("@ben, @chen")).to_be_focused()
    page.keyboard.press("k")
    expect(messages.row("@ben")).to_be_focused()

    page.keyboard.press("e")
    expect(messages.row("@ben")).not_to_have_class(re.compile(r"\bunread\b"))
    expect(messages.row("@ben")).not_to_contain_text("unread")


def test_a_group_reply_goes_to_everyone_on_the_thread_but_you_after_a_review(
    messages: Messages,
) -> None:
    messages.open()
    messages.open_chat("@ben, @chen")

    expect(messages.quoted("Are we dropping the old webhook column")).to_be_visible()
    expect(messages.quoted("Next release. Two consumers still read it.")).to_be_visible()
    expect(messages.pane).to_contain_text("Continues the thread")

    messages.review("Agreed: next release, once both consumers move.")
    expect(messages.preview).to_contain_text("Ben Otieno (@ben)")
    expect(messages.preview).to_contain_text("Chen Wu (@chen)")
    expect(messages.preview).not_to_contain_text("demo")

    messages.send()
    expect(messages.said).to_contain_text("Queued for Ben Otieno (@ben)")
    expect(messages.said).to_contain_text("Queued for Chen Wu (@chen)")
    expect(messages.mine("Agreed: next release")).to_contain_text("queued")


def test_a_new_message_to_a_workspace_goes_to_every_teammate_after_a_review(
    messages: Messages, page: Page
) -> None:
    messages.open()
    page.get_by_role("link", name="New message").click()
    page.get_by_label("Or everyone in a workspace").select_option("ws_demo")
    page.get_by_label("Message", exact=True).fill("Standup moves to 10:00 tomorrow.")
    page.get_by_role("button", name="Review and send").click()

    expect(messages.preview).to_contain_text("everyone in ws_demo (3 people)")
    messages.send()

    expect(page).to_have_url(re.compile(r"/mesh/messages/c/ws-ws_demo"))
    for teammate in ("Ben Otieno (@ben)", "Chen Wu (@chen)", "Dana Reyes (@dana)"):
        expect(messages.said).to_contain_text(f"Queued for {teammate}")
    expect(messages.mine("Standup moves to 10:00 tomorrow.")).to_be_visible()


def test_a_teammate_is_muted_from_their_chat_and_unmuted_again(messages: Messages) -> None:
    messages.open()
    messages.open_chat("@ben")

    messages.mute("ben")
    expect(messages.said).to_have_text("Muted @ben.")
    expect(messages.title).to_contain_text("muted")

    messages.unmute("ben")
    expect(messages.said).to_have_text("Unmuted @ben.")
    expect(messages.title).not_to_contain_text("muted")


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_every_messages_view_is_readable_and_fits_a_phone(
    messages: Messages, page: Page, theme: str
) -> None:
    """The list, each chat, a message under review and the new-message form."""
    messages.open()
    chats: list[str] = messages.chats.locator("a.chat-row").evaluate_all(
        "rows => rows.map(r => r.getAttribute('href'))"
    )
    views = ["/mesh/messages", "/mesh/messages/compose", *chats]
    unreadable: list[str] = []
    for width in (1280, 375):
        page.set_viewport_size({"width": width, "height": 900})
        for path in views:
            messages.open(path)
            helpers.set_theme(page, theme)
            unreadable += [f"{path} {width}px: {f}" for f in helpers.contrast_failures(page)]
            overflow = page.evaluate(helpers.OVERFLOW_JS)
            if overflow["overflow"] > 0:
                unreadable.append(f"{path} {width}px scrolls sideways: {overflow['offenders']}")
    messages.open("/mesh/messages/c/dm-chen")
    helpers.set_theme(page, theme)
    messages.review("A message under review.")
    expect(messages.preview).to_be_visible()
    unreadable += [f"review {theme}: {f}" for f in helpers.contrast_failures(page)]

    assert not unreadable, "\n".join(unreadable)


def _arrive(home: Path, author: str, body: str) -> None:
    """A message from a teammate lands, as `receive` leaves it in the database."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    from flanner.database import MeshMessageModel

    engine = create_engine(f"sqlite:///{home / 'data.db'}")
    mid = f"sha256:{uuid.uuid4().hex}"
    with Session(engine) as session:
        session.add(
            MeshMessageModel(
                message_id=mid,
                thread_id=mid,
                workspace_id="ws_demo",
                author_user_id=author,
                author_device_id="dev_" + "c" * 16,
                recipients='["demo"]',
                body=body,
                refs="[]",
                sent_at=datetime.now(timezone.utc).replace(tzinfo=None),
                outgoing=False,
                envelope="{}",
                payload="{}",
            )
        )
        session.commit()
    engine.dispose()


def test_an_arrival_updates_the_chat_but_never_under_a_half_written_reply(
    messages: Messages, page: Page, signed_in_home: Path
) -> None:
    messages.open("/mesh/messages/c/dm-chen")
    messages.composer.fill("Half a thought")

    _arrive(signed_in_home, "chen", "Is the freeze still on?")
    expect(page.locator("[data-live-banner]")).to_contain_text("New messages")
    expect(page.locator(".toast-region")).to_contain_text("sent a message")
    expect(messages.composer).to_have_value("Half a thought")
    expect(messages.quoted("Is the freeze still on?")).to_have_count(0)

    messages.composer.fill("")
    _arrive(signed_in_home, "chen", "Never mind, found it.")
    expect(messages.quoted("Never mind, found it.")).to_be_visible()
