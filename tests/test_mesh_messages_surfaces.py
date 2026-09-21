"""Messages from the CLI, the web UI and the service layer they share.

The round trip between two devices is `test_mesh_messages`. These check
that every surface reaches the same operation and says the same thing,
on one signed-in device whose teammate is offline.
"""

from __future__ import annotations

import json
from datetime import datetime
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
    result = run("mesh", "inbox")
    assert result.exit_code == 0, result.output
    assert "No unread messages" in result.output


def test_send_previews_and_sends_nothing_on_no(signed_in):
    result = run("mesh", "send", "bob", "drop the old column?", input="n\n")

    assert result.exit_code == 0, result.output
    assert "@bob (Bob)" in result.output
    assert "Not sent." in result.output
    assert get_session().query(MeshMessageModel).count() == 0


def test_send_on_yes_queues_for_an_offline_teammate(signed_in, monkeypatch):
    from flanner import mesh_delivery, peer

    def unreachable(device_id, workspace_id, held):
        raise peer.PeerError(f"could not reach {device_id}")

    monkeypatch.setattr(mesh_delivery, "dial_device", unreachable)

    result = run("mesh", "send", "bob", "are you there?", "--yes")

    assert result.exit_code == 0, result.output
    assert "Queued for @bob" in result.output
    assert get_session().query(MeshDeliveryModel).one().state == "queued"


def test_an_unknown_handle_is_refused(signed_in):
    result = run("mesh", "send", "bobb", "hi", "--yes")
    assert result.exit_code == 1
    assert "Did you mean @bob (Bob)?" in result.output


def test_quiet_hours_round_trip_as_json(signed_in):
    assert run("mesh", "quiet-hours", "22:00-07:00").exit_code == 0

    shown = json.loads(run("mesh", "quiet-hours", "--json").output)

    assert (shown["enabled"], shown["start"], shown["end"]) == (True, "22:00", "07:00")


def test_quiet_hours_that_make_no_sense_are_refused_with_an_example(signed_in):
    result = run("mesh", "quiet-hours", "10pm")
    assert result.exit_code == 1
    assert "22:00-07:00" in result.output


def test_mute_for_a_while_then_unmute(signed_in):
    assert "Muted @bob until" in run("mesh", "mute", "bob", "--for", "8h").output
    assert "Unmuted @bob" in run("mesh", "mute", "bob", "--off").output


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


def test_the_messages_page_renders_empty(client):
    page = client.get("/mesh/messages")
    assert page.status_code == 200
    assert "No messages yet" in page.text
    assert 'data-live-list="messages"' in page.text


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


def test_a_thread_mutes_and_unmutes_its_sender(client, signed_in):
    session = get_session()
    session.add(
        MeshMessageModel(
            message_id="sha256:cd34",
            thread_id="sha256:cd34",
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
    page = client.get("/mesh/messages/cd34")
    assert page.text.count('action="/mesh/messages/mute"') == 1
    assert 'value="/mesh/messages/cd34"' in page.text

    done = client.post(
        "/mesh/messages/mute",
        data={"handle": "bob", "until": "8h", "back": "/mesh/messages/cd34"},
    )
    assert done.status_code == 303
    assert done.headers["location"].startswith("/mesh/messages/cd34?said=Muted")
    assert "Unmute" in client.get("/mesh/messages/cd34").text
