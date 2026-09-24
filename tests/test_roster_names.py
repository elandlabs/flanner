"""Teammates by name: the handle and name the control plane signs into the roster.

Mesh messaging addresses people as `@ben` and shows them as `@ben (Ben
Otieno)`. Both come from the signed roster, never from anything a teammate
sends, so a name cannot be forged by a peer. A roster from a control plane
older than messaging carries neither, and must still work.
"""

from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone

from click.testing import CliRunner
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from flanner import session as cache
from flanner.artifacts import canonical_bytes
from flanner.entitlements import (
    DEFAULT_MESSAGE_RETENTION_DAYS,
    ROSTER,
    Member,
    verify_roster,
)
from flanner.identity import public_key_b64, sign

ISSUER = Ed25519PrivateKey.generate()
KEYRING = {"sk_1": public_key_b64(ISSUER.public_key())}


def _stamp(offset: timedelta = timedelta()) -> str:
    return (datetime.now(timezone.utc) + offset).isoformat().replace("+00:00", "Z")


def roster(members: list[dict], **extra) -> str:
    data = canonical_bytes(
        {
            "kind": ROSTER,
            "organization_id": "org_1",
            "issued_at": _stamp(),
            "expires_at": _stamp(timedelta(hours=1)),
            "workspaces": {"ws_core": members},
            "key_id": "sk_1",
            **extra,
        }
    )
    return base64.urlsafe_b64encode(data).decode().rstrip("=") + "." + sign(data, ISSUER)


BEN = {"user_id": "usr_ben", "role": "editor", "devices": [], "handle": "ben", "name": "Ben O"}


def test_the_roster_carries_handles_names_and_retention():
    parsed = verify_roster(roster([BEN], message_retention_days=30), KEYRING)

    (member,) = parsed.members("ws_core")
    assert (member.handle, member.name) == ("ben", "Ben O")
    assert parsed.message_retention_days == 30


def test_a_roster_from_before_messaging_still_verifies_with_defaults():
    old = {"user_id": "usr_ben", "role": "editor", "devices": []}

    parsed = verify_roster(roster([old]), KEYRING)

    (member,) = parsed.members("ws_core")
    assert (member.handle, member.name) == ("", "")
    assert parsed.message_retention_days == DEFAULT_MESSAGE_RETENTION_DAYS


def test_a_member_is_shown_as_handle_and_name():
    assert Member("usr_ben", "editor", handle="ben", name="Ben O").label == "@ben (Ben O)"


def test_without_a_handle_the_id_stands_in_for_one():
    assert Member("usr_ben1234567", "editor").label == "@usr_ben1"


def test_someone_not_on_the_roster_is_shown_by_id():
    """A departed teammate is no longer listed; their id is still true."""
    parsed = verify_roster(roster([BEN]), KEYRING)
    assert parsed.label_for("usr_gone") == "usr_gone"
    assert parsed.label_for("usr_ben") == "@ben (Ben O)"


def _signed_in(team: str) -> None:
    cache.save(
        cache.Session(
            endpoint="https://api.example.test",
            device_id="dev_1",
            organization_id="org_1",
            user_id="usr_me",
            entitlement="unused",
            keyring=KEYRING,
            roster=team,
        )
    )


def test_teammate_labels_read_the_cached_roster():
    _signed_in(roster([BEN]))
    assert cache.teammate_labels()("usr_ben") == "@ben (Ben O)"


def test_teammate_labels_show_ids_when_the_roster_does_not_verify():
    """A name nobody signed for is not worth showing."""
    forged = roster([BEN]).rsplit(".", 1)[0] + ".bm90LWEtc2lnbmF0dXJl"
    _signed_in(forged)
    assert cache.teammate_labels()("usr_ben") == "usr_ben"


def test_teammate_labels_without_a_session_show_ids():
    assert cache.teammate_labels()("usr_ben") == "usr_ben"


def test_flanner_members_shows_handles_and_names(monkeypatch):
    from flanner import account
    from flanner.cli import cli

    monkeypatch.setattr(
        account,
        "list_members",
        lambda: {
            "seats": 1,
            "members": [
                {
                    "user_id": "usr_ben",
                    "handle": "ben",
                    "name": "Ben O",
                    "email": "ben@x.test",
                    "role": "member",
                    "state": "active",
                },
            ],
        },
    )

    result = CliRunner().invoke(cli, ["members"])

    assert result.exit_code == 0, result.output
    assert "@ben" in result.output and "Ben O" in result.output


#: A name as a hostile teammate might set it: an escape sequence that writes
#: the clipboard, a bidirectional override, and Rich markup for a link.
HOSTILE_NAME = "\x1b]52;c;cHduZWQ=\x07Ben‮O [link=https://evil.example]x[/link]"


def test_a_name_from_the_roster_cannot_act_on_the_screen_showing_it():
    """Signed by the control plane, but typed by the member, so not trusted."""
    parsed = verify_roster(roster([{**BEN, "name": HOSTILE_NAME}]), KEYRING)

    (member,) = parsed.members("ws_core")
    assert "\x1b" not in member.name and "\x07" not in member.name
    assert "‮" not in member.name
    assert "Ben" in member.name


def test_flanner_members_shows_markup_in_a_name_rather_than_obeying_it(monkeypatch):
    from flanner import account
    from flanner.cli import cli

    member = {
        "user_id": "usr_ben",
        "handle": "ben",
        "name": HOSTILE_NAME,
        "email": "ben@x.test",
        "role": "member",
        "state": "active",
    }
    monkeypatch.setattr(account, "list_members", lambda: {"seats": 1, "members": [member]})

    result = CliRunner().invoke(cli, ["members"], env={"COLUMNS": "200"})

    assert result.exit_code == 0, result.output
    assert "[link=https://evil.example]" in result.output
    assert "\x1b]52" not in result.output
