"""What `flanner login` says once it has worked.

It printed one line and stopped. Enrolling is the middle of a setup, not
the end of one: the device now has an identity and no project, and the
person most likely to be running it is somebody setting up a team for the
first time, who has the least idea what to type next.

What to say depends on what the account holds, which is why this is not a
fixed block of text. A device with no workspace access cannot join
anything yet and needs to hear about the console; a device that holds a
grant needs `init` and `join` in the repository it cares about.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from click.testing import CliRunner
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from flanner import account
from flanner import session as cache
from flanner.artifacts import canonical_bytes
from flanner.cli import cli
from flanner.entitlements import TEAM_SYNC, Claims, WorkspaceCapability, encode_token
from flanner.identity import public_key_b64, sign
from flanner.workflow import MAINTAINER


def _entitlement(*, workspaces: tuple[tuple[str, str], ...]) -> tuple[str, dict[str, str]]:
    key = Ed25519PrivateKey.generate()
    now = datetime.now(timezone.utc)
    claims = Claims(
        organization_id="org_1",
        user_id="maria",
        device_id="dev_abc",
        key_id="sk_1",
        issued_at=(now - timedelta(minutes=1)).isoformat().replace("+00:00", "Z"),
        expires_at=(now + timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
        features=(TEAM_SYNC,),
        workspace_capabilities=tuple(
            WorkspaceCapability(workspace_id=w, role=r) for w, r in workspaces
        ),
    )
    token = encode_token(claims, sign(canonical_bytes(claims.to_dict()), key))
    return token, {"sk_1": public_key_b64(key.public_key())}


def _enrol(monkeypatch, *, workspaces: tuple[tuple[str, str], ...], org_role: str = ""):
    """Stand in for the control plane, so `login` runs its real path."""
    token, keyring = _entitlement(workspaces=workspaces)

    def post(endpoint, path, payload, *, repeatable=False):
        if path == "/v1/devices/keyring":
            return {"devices": {}}
        return {
            "device_id": "dev_abc",
            "organization_id": "org_1",
            "user_id": "maria",
            "entitlement": token,
            "keyring": keyring,
            "org_role": org_role,
        }

    monkeypatch.setattr(account, "_post", post)


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("FLANNER_HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()


def test_enrolling_still_says_who_you_are(monkeypatch):
    _enrol(monkeypatch, workspaces=(("ws_core", MAINTAINER),))

    result = CliRunner().invoke(cli, ["login", "code", "--endpoint", "https://x.test"])

    assert result.exit_code == 0, result.output
    assert "Enrolled as maria" in result.output
    assert cache.load() is not None


def test_enrolling_leaves_a_machine_that_can_run_what_it_was_just_told_to(monkeypatch, tmp_path):
    """`login` and `accept` enrol a device identically, so they must leave it
    in the same state. Only `accept` created the store, so whether the very
    next instruction worked depended on which command you had been sent to.
    """
    _enrol(monkeypatch, workspaces=(("ws_core", MAINTAINER),))

    CliRunner().invoke(cli, ["login", "code", "--endpoint", "https://x.test"])

    assert (tmp_path / "home" / "data.db").exists(), "login enrolled but made no store"


def test_the_command_login_recommends_is_not_refused_for_want_of_a_store(monkeypatch):
    """The failure this fixes, asserted where a user met it: `join` printed
    by `login`, then refused because nothing had made a database."""
    _enrol(monkeypatch, workspaces=(("ws_core", MAINTAINER),))
    runner = CliRunner()

    assert (
        "flanner init"
        in runner.invoke(cli, ["login", "code", "--endpoint", "https://x.test"]).output
    )
    refusal = runner.invoke(cli, ["join", "ws_core"]).output

    assert "no flanner store yet" not in refusal, refusal


def test_an_account_with_no_workspace_is_told_where_one_comes_from(monkeypatch):
    """The state a first admin is in, and the one that used to end in silence.

    They cannot `join` anything yet, so naming `join` here would send them
    at a command that refuses. The console is where a workspace is made.
    """
    _enrol(monkeypatch, workspaces=())

    output = CliRunner().invoke(cli, ["login", "code", "--endpoint", "https://x.test"]).output
    said = " ".join(output.split())

    assert "No workspace access yet" in said
    assert "whoami --refresh" in said
    assert "flanner join" not in said, "pointed at a command that would refuse"


def test_an_account_that_holds_access_is_given_the_three_commands(monkeypatch):
    """Enrolled with a grant: the next moves are in a repository."""
    _enrol(monkeypatch, workspaces=(("ws_core", MAINTAINER),))

    output = CliRunner().invoke(cli, ["login", "code", "--endpoint", "https://x.test"]).output
    said = " ".join(output.split())

    assert "ws_core" in said and "maintainer" in said
    for command in ("flanner init", "flanner join", "flanner peer serve"):
        assert command in said, f"{command} was not offered"


def test_every_workspace_held_is_named(monkeypatch):
    """Two teams is the case where "which id do I join?" is a real question."""
    _enrol(monkeypatch, workspaces=(("ws_core", MAINTAINER), ("ws_infra", "reader")))

    said = " ".join(
        CliRunner().invoke(cli, ["login", "code", "--endpoint", "https://x.test"]).output.split()
    )

    assert "ws_core" in said and "ws_infra" in said


def _said(monkeypatch, **enrol) -> str:
    _enrol(monkeypatch, **enrol)
    output = CliRunner().invoke(cli, ["login", "code", "--endpoint", "https://x.test"]).output
    return " ".join(output.split())


def test_an_admin_with_no_workspace_is_sent_to_the_page_that_makes_one(monkeypatch):
    said = _said(monkeypatch, workspaces=(), org_role="admin")

    assert "https://x.test/workspaces" in said
    assert "flanner invite" in said and "flanner members" in said


def test_a_member_with_no_workspace_is_told_to_ask_rather_than_create(monkeypatch):
    """A member cannot make a workspace, so the console link would refuse."""
    said = _said(monkeypatch, workspaces=(), org_role="member")

    assert "Ask an admin" in said
    assert "/workspaces" not in said
    assert "flanner invite" not in said, "admin-only advice shown to a member"


def test_somebody_who_may_decide_on_proposals_is_told_where_they_are(monkeypatch):
    assert "flanner review status" in _said(monkeypatch, workspaces=(("ws_core", MAINTAINER),))


def test_a_reader_is_not_pointed_at_reviews_it_cannot_decide(monkeypatch):
    assert "flanner review status" not in _said(monkeypatch, workspaces=(("ws_core", "reader"),))


def test_the_role_is_kept_with_the_session(monkeypatch):
    """Later commands shape their advice from the cache, not a new request."""
    _enrol(monkeypatch, workspaces=(), org_role="admin")
    CliRunner().invoke(cli, ["login", "code", "--endpoint", "https://x.test"])
    current = cache.load()
    assert current is not None and current.org_role == "admin"
