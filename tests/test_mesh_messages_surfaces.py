"""Messages from the CLI, the web UI and the service layer they share.

The round trip between two devices is `test_mesh_messages`. These check
that every surface reaches the same operation and says the same thing,
on one signed-in device whose teammate is offline.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from click.testing import CliRunner
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from flanner import identity, services
from flanner import session as cache
from flanner.cli import cli
from flanner.database import MeshDeliveryModel, MeshMessageModel, get_session
from flanner.entitlements import MESH_MESSAGES, TEAM_SYNC
from flanner.web import app
from flanner.workflow import EDITOR
from tests.test_mesh_messages import a_roster
from tests.test_peer import an_entitlement, keyring_of

LOCAL_URL = "http://127.0.0.1:8080"
ISSUER = Ed25519PrivateKey.generate()


@pytest.fixture
def signed_in(db, monkeypatch):
    """This device as `you`, with a teammate `bob` whose device is offline.

    FLANNER_HOME is where `db` put the catalog, so the command line finds it.
    """
    monkeypatch.setenv("FLANNER_HOME", str(db.parent))
    me = SimpleNamespace(user="you", role=EDITOR, device_id=identity.device_id())
    bob = SimpleNamespace(user="bob", role=EDITOR, device_id="dev_" + "b" * 16)
    cache.save(
        cache.Session(
            endpoint="https://api.example.test",
            device_id=me.device_id,
            organization_id="org_1",
            user_id="you",
            entitlement=an_entitlement(
                ISSUER,
                device_id=me.device_id,
                role=EDITOR,
                user="you",
                features=(TEAM_SYNC, MESH_MESSAGES),
            ),
            keyring=keyring_of(ISSUER),
            device_keys={me.device_id: identity.device_public_key_b64()},
            roster=a_roster(ISSUER, [me, bob]),
        )
    )
    return me


@pytest.fixture
def client(signed_in):
    return TestClient(app, base_url=LOCAL_URL, follow_redirects=False)


def run(*args, input=None):
    result = CliRunner().invoke(cli, list(args), input=input)
    return result


# --- CLI ------------------------------------------------------------------------


def test_an_empty_inbox_says_so(signed_in):
    result = run("messages", "inbox")
    assert result.exit_code == 0, result.output
    assert "No unread messages" in result.output


def test_send_previews_and_sends_nothing_on_no(signed_in):
    result = run("messages", "send", "bob", "drop the old column?", input="n\n")

    assert result.exit_code == 0, result.output
    assert "@bob (Bob)" in result.output
    assert "Not sent." in result.output
    assert get_session().query(MeshMessageModel).count() == 0


def test_send_on_yes_queues_for_an_offline_teammate(signed_in, monkeypatch):
    from flanner import mesh_delivery, peer

    def unreachable(device_id, workspace_id, held):
        raise peer.PeerError(f"could not reach {device_id}")

    monkeypatch.setattr(mesh_delivery, "dial_device", unreachable)

    result = run("messages", "send", "bob", "are you there?", "--yes")

    assert result.exit_code == 0, result.output
    assert "Queued for @bob" in result.output
    assert get_session().query(MeshDeliveryModel).one().state == "queued"


def test_an_unknown_handle_is_refused(signed_in):
    result = run("messages", "send", "bobb", "hi", "--yes")
    assert result.exit_code == 1
    assert "Did you mean @bob (Bob)?" in result.output


def test_quiet_hours_round_trip_as_json(signed_in):
    assert run("messages", "quiet-hours", "22:00-07:00").exit_code == 0

    shown = json.loads(run("messages", "quiet-hours", "--json").output)

    assert (shown["enabled"], shown["start"], shown["end"]) == (True, "22:00", "07:00")


def test_quiet_hours_that_make_no_sense_are_refused_with_an_example(signed_in):
    result = run("messages", "quiet-hours", "10pm")
    assert result.exit_code == 1
    assert "22:00-07:00" in result.output


def test_watch_shows_markup_in_a_message_rather_than_obeying_it(signed_in, monkeypatch):
    """A teammate's link is printed as the text they wrote, never as a link."""
    from flanner import cli as cli_module

    session = a_message("markup")
    row = session.query(MeshMessageModel).one()
    row.body = "see [link=https://evil.example]the plan[/link]"
    session.commit()
    monkeypatch.setattr(cli_module, "_new_messages_since", lambda _session, _since: [row])

    def stop(_seconds):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli_module.time, "sleep", stop)

    result = run("messages", "watch")

    assert result.exit_code == 0, result.output
    assert "[link=https://evil.example]the plan[/link]" in result.output


def test_mute_for_a_while_then_unmute(signed_in):
    assert "Muted @bob until" in run("messages", "mute", "bob", "--for", "8h").output
    assert "Unmuted @bob" in run("messages", "mute", "bob", "--off").output


# --- the service layer the MCP tools call ----------------------------------------


def test_a_preview_names_its_recipients_and_signs_nothing(signed_in):
    preview = services.mesh_send(body="hi", to=["@bob"])
    assert preview["preview"] is True
    assert preview["to"] == [{"user_id": "bob", "handle": "bob", "name": "Bob"}]


def test_every_refusal_carries_a_code(signed_in):
    refused = services.mesh_send(body="x" * 5000, to=["bob"])
    assert refused == {
        "error": True,
        "code": "body_too_large",
        "message": "Messages are up to 4 KB.",
    }


# --- the MCP tools an agent calls --------------------------------------------------


def _offline(monkeypatch):
    from flanner import mesh_delivery, peer

    def unreachable(device_id, workspace_id, held):
        raise peer.PeerError(f"could not reach {device_id}")

    monkeypatch.setattr(mesh_delivery, "dial_device", unreachable)


def test_an_agent_cannot_send_without_a_preview(signed_in, monkeypatch):
    from flanner import server

    _offline(monkeypatch)
    refused = server.messages_send(body="hi", to=["bob"], confirm=True)

    assert refused["error"] is True
    assert refused["message"].startswith("Preview first")
    assert get_session().query(MeshMessageModel).count() == 0


def test_a_preview_token_sends_only_what_was_previewed(signed_in, monkeypatch):
    from flanner import server

    _offline(monkeypatch)
    token = server.messages_send(body="hi", to=["bob"])["preview_token"]

    switched = server.messages_send(
        body="something else", to=["bob"], confirm=True, preview_token=token
    )
    assert switched["error"] is True
    assert get_session().query(MeshMessageModel).count() == 0

    sent = server.messages_send(body="hi", to=["bob"], confirm=True, preview_token=token)
    assert [d["state"] for d in sent["delivery"]] == ["queued"]


def test_a_preview_token_runs_out(signed_in, monkeypatch):
    from flanner import server

    _offline(monkeypatch)
    monkeypatch.setattr(server, "PREVIEW_SECONDS", -1)
    token = server.messages_send(body="hi", to=["bob"])["preview_token"]

    assert server.messages_send(body="hi", to=["bob"], confirm=True, preview_token=token)["error"]
    assert get_session().query(MeshMessageModel).count() == 0


def test_a_reply_needs_its_own_preview(signed_in, monkeypatch):
    from flanner import server

    _offline(monkeypatch)
    first = server.messages_send(body="hi", to=["bob"])
    thread = server.messages_send(
        body="hi", to=["bob"], confirm=True, preview_token=first["preview_token"]
    )["thread_id"]

    assert server.messages_reply(thread=thread, body="and?", confirm=True)["error"] is True
    token = server.messages_reply(thread=thread, body="and?")["preview_token"]
    replied = server.messages_reply(thread=thread, body="and?", confirm=True, preview_token=token)
    assert replied["thread_id"] == thread


def test_messaging_off_is_said_plainly(db, monkeypatch):
    monkeypatch.setenv("FLANNER_HOME", str(db.parent))
    me = identity.device_id()
    cache.save(
        cache.Session(
            endpoint="https://api.example.test",
            device_id=me,
            organization_id="org_1",
            user_id="you",
            entitlement=an_entitlement(ISSUER, device_id=me, user="you", features=(TEAM_SYNC,)),
            keyring=keyring_of(ISSUER),
        )
    )
    assert services.mesh_inbox()["code"] == "messaging_off"


# --- web UI ---------------------------------------------------------------------


def a_message(mid, *, author="bob", to='["you"]', at=None, outgoing=False):
    """One held message, the way `receive` or `record_outgoing` would leave it."""
    session = get_session()
    moment = at or datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=2)
    session.add(
        MeshMessageModel(
            message_id=f"sha256:{mid}",
            thread_id=f"sha256:{mid}",
            workspace_id="ws_core",
            author_user_id=author,
            author_device_id="dev_b",
            recipients=to,
            body="hi",
            sent_at=moment,
            outgoing=outgoing,
            read_at=moment if outgoing else None,
            envelope="{}",
            payload="{}",
        )
    )
    session.commit()
    return session


def test_an_arrival_is_a_new_message_from_a_teammate_counted_as_a_number(signed_in):
    """The live stream's toast fires for the tenth message too, and not for a send."""
    from flanner.web import _arrived, _messages_signature

    def snapshot():
        return {"messages": _messages_signature(get_session())}

    for n in range(9):
        a_message(f"in{n}")
    nine = snapshot()
    a_message("in9")
    ten = snapshot()
    a_message("out0", author="you", to='["bob"]', outgoing=True)
    sent = snapshot()

    assert _arrived(nine, ten), "the tenth message was not an arrival"
    assert not _arrived(ten, sent), "your own send was taken for an arrival"


def test_the_messages_page_lists_the_roster_and_says_nothing_is_unread(client):
    page = client.get("/mesh/messages")
    assert page.status_code == 200
    assert "Nothing unread" in page.text
    assert 'aria-label="Chats"' in page.text and "Filter chats" in page.text
    assert ">Workspaces<" in page.text and ">People<" in page.text
    assert 'href="/mesh/messages/c/dm-bob"' in page.text and "@bob" in page.text
    assert 'href="/mesh/messages/c/ws-ws_core"' in page.text
    assert ">Unread" not in page.text and ">Groups<" not in page.text
    # The live-update marker sits on the layout root, so list and pane refresh together.
    assert 'class="inbox-layout" data-live-list="messages"' in page.text


def test_the_empty_pane_counts_what_is_unread(client):
    a_message("ab12")
    a_message("ab34", at=datetime(2026, 9, 1))
    page = client.get("/mesh/messages")
    assert "2 unread in 1 chat" in page.text
    assert ">Unread 2<" in page.text
    assert 'class="chat-row unread"' in page.text


def test_a_chat_page_shows_the_time_line_marks_it_read_and_offers_a_composer(client):
    session = a_message("cd34")
    page = client.get("/mesh/messages/c/dm-bob")

    assert page.status_code == 200
    assert 'class="inbox-layout has-chat" data-live-list="messages"' in page.text
    assert 'aria-label="Conversation with @bob"' in page.text
    assert 'aria-current="page"' in page.text
    assert (
        'id="t-cd34"' in page.text and '<blockquote class="msg-quote">hi</blockquote>' in page.text
    )
    assert "Message @bob" in page.text and "Starts a new thread" in page.text
    assert 'action="/mesh/messages"' in page.text and 'name="to" value="bob"' in page.text
    assert 'name="chat" value="dm-bob"' in page.text
    assert "deleted after 90 days" in page.text
    assert 'href="/mesh/messages"' in page.text and "Back to messages" in page.text
    assert "data-save" in page.text
    session.expire_all()
    assert session.get(MeshMessageModel, "sha256:cd34").read_at is not None


def test_a_fresh_thread_is_what_the_composer_continues(client):
    a_message("cd34", at=datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=1))
    page = client.get("/mesh/messages/c/dm-bob")
    assert "Continues the thread" in page.text
    assert 'action="/mesh/messages/cd34/reply"' in page.text


def test_marking_a_chat_read_needs_no_visit(client):
    session = a_message("cd34")
    done = client.post("/mesh/messages/c/dm-bob/read", data={"back": "/mesh/messages"})
    assert done.status_code == 303 and done.headers["location"] == "/mesh/messages"
    session.expire_all()
    assert session.get(MeshMessageModel, "sha256:cd34").read_at is not None
    # The row carries the address the e key posts to, only while it is unread.
    assert 'data-read="/mesh/messages/c/dm-bob/read"' not in client.get("/mesh/messages").text


def test_a_threads_old_address_goes_to_its_chat(client):
    a_message("cd34")
    moved = client.get("/mesh/messages/cd34")
    assert moved.status_code == 303
    assert moved.headers["location"] == "/mesh/messages/c/dm-bob#t-cd34"


def test_an_unknown_chat_is_a_404_that_keeps_the_list(client):
    page = client.get("/mesh/messages/c/grp-000000000000")
    assert page.status_code == 404
    assert "No such chat" in page.text and "Filter chats" in page.text


def test_a_failed_delivery_is_said_under_the_message(client):
    session = a_message("ef56", author="you", to='["bob"]', outgoing=True)
    session.add(
        MeshDeliveryModel(
            message_id="sha256:ef56",
            user_id="bob",
            device_id="dev_b",
            state="failed",
            code="peer_outdated",
            detail="too old",
        )
    )
    session.commit()
    page = client.get("/mesh/messages/c/dm-bob").text
    assert '<span class="pill pill-stale">1 failed</span>' in page
    assert "Not delivered to Bob (@bob): too old" in page
    assert '<p class="msg-body">hi</p>' in page, "your own message is plain, not quoted"


def test_back_to_accepts_a_chat_address_and_refuses_a_scheme():
    from flanner.web import _back_to

    assert _back_to("/mesh/messages/c/dm-ben%40x.com", "/x") == "/mesh/messages/c/dm-ben%40x.com"
    assert _back_to("/mesh/messages/c/dm-ben@x.com", "/x") == "/mesh/messages/c/dm-ben@x.com"
    assert _back_to("/mesh/messages/c/dm-ben:x", "/x") == "/x"
    assert _back_to("//evil/mesh/messages", "/x") == "/x"


def test_ctrl_s_only_submits_a_form_that_opted_in():
    from flanner.web import WEB_DIR

    script = (WEB_DIR / "static" / "js" / "app.js").read_text(encoding="utf-8")
    assert "querySelector('form[data-save]')" in script
    assert "querySelector('form')" not in script, "Ctrl+S would submit the first form on the page"


def test_the_web_form_previews_before_sending(client):
    page = client.post("/mesh/messages", data={"to": "bob", "body": "next release?"})

    assert page.status_code == 200
    assert "Send this?" in page.text and "Bob (@bob)" in page.text
    assert get_session().query(MeshMessageModel).count() == 0


def test_a_body_is_rendered_as_text_never_markup(client, signed_in):
    page = client.post("/mesh/messages", data={"to": "bob", "body": "<script>x()</script>"})
    assert "<script>x()</script>" not in page.text
    assert "&lt;script&gt;" in page.text


def test_an_unknown_thread_is_a_404_that_says_so(client):
    page = client.get("/mesh/messages/abcd1234")
    assert page.status_code == 404
    assert "No thread abcd1234" in page.text


def test_settings_shows_the_handle_and_saves_quiet_hours(client):
    page = client.get("/settings")
    assert "@you" in page.text and "Quiet hours" in page.text

    saved = client.post(
        "/mesh/messages/quiet-hours", data={"enabled": "on", "start": "21:30", "end": "06:45"}
    )

    assert saved.status_code == 303
    assert services.mesh_quiet_hours()["start"] == "21:30"


def test_the_sidebar_counts_unread_messages(client, signed_in):
    session = get_session()
    session.add(
        MeshMessageModel(
            message_id="sha256:ab12",
            thread_id="sha256:ab12",
            workspace_id="ws_core",
            author_user_id="bob",
            author_device_id="dev_b",
            recipients='["you"]',
            body="hi",
            sent_at=datetime(2026, 9, 21),
            envelope="{}",
            payload="{}",
        )
    )
    session.commit()
    page = client.get("/mesh/messages")
    assert "Messages" in page.text and '<span class="n tnum">1</span>' in page.text


def test_a_chat_mutes_and_unmutes_its_sender(client, signed_in):
    a_message("cd34")
    page = client.get("/mesh/messages/c/dm-bob")
    assert '<details class="chat-menu">' in page.text
    assert page.text.count('action="/mesh/messages/mute"') == 1
    assert 'value="/mesh/messages/c/dm-bob"' in page.text and "Mute @bob" in page.text

    done = client.post(
        "/mesh/messages/mute",
        data={"handle": "bob", "until": "8h", "back": "/mesh/messages/c/dm-bob"},
    )
    assert done.status_code == 303
    assert done.headers["location"].startswith("/mesh/messages/c/dm-bob?said=Muted")
    after = client.get("/mesh/messages/c/dm-bob").text
    assert "Unmute @bob" in after
    assert after.count('<span class="pill pill-quiet">muted</span>') == 3, "row, head and message"


def test_the_empty_pane_does_not_call_muted_unread_nothing(client, signed_in):
    """The rail badge counts a muted sender's unread, so the pane says so too."""
    session = get_session()
    session.add(
        MeshMessageModel(
            message_id="sha256:ee55",
            thread_id="sha256:ee55",
            workspace_id="ws_core",
            author_user_id="bob",
            author_device_id="dev_b",
            recipients='["you"]',
            body="still here?",
            sent_at=datetime(2026, 9, 22),
            envelope="{}",
            payload="{}",
        )
    )
    session.commit()
    services.mesh_mute("bob")

    page = client.get("/mesh/messages").text
    assert "except from muted senders" in page
    assert "1 unread from someone you muted" in page
    assert "Unread 1" not in page, "a muted chat never enters the Unread section"


def test_a_new_message_form_starts_a_group_chat(client):
    """The list only shows groups that exist; naming people is how one starts."""
    page = client.get("/mesh/messages").text
    assert 'href="/mesh/messages/compose"' in page

    form = client.get("/mesh/messages/compose")
    assert form.status_code == 200
    assert 'name="to"' in form.text and 'name="workspace"' in form.text
    assert '<option value="bob">' in form.text, "teammates come from the roster"

    refused = client.post("/mesh/messages", data={"to": "nobody", "body": "hi"})
    assert refused.status_code == 400
    assert 'name="to"' in refused.text and 'value="nobody"' in refused.text, (
        "a refused message goes back to its form with what was typed"
    )
