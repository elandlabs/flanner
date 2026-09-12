"""Applying what an agent asked for, when that agent could type the command too.

An agent with a shell runs `flanner actions apply` as easily as a person.
Signed in, applying therefore waits for a confirmation the console signs for
a browser signed in as that person, bound to this action and its preview.
Not signed in, nothing can prove a person, so the command line refuses inside
a shell an agent host started. These tests hold both, and the limits.
"""

from __future__ import annotations

import base64
import subprocess
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from click.testing import CliRunner
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from flanner import identity, requested_actions
from flanner import session as cache
from flanner.artifacts import canonical_bytes
from flanner.cli import cli
from flanner.database import create_project, get_memory, get_session
from flanner.entitlements import APPROVAL, Claims, approval_subject, encode_token
from flanner.identity import public_key_b64, sign
from flanner.services import dispatch

ISSUER = Ed25519PrivateKey.generate()
KEYRING = {"sk_1": public_key_b64(ISSUER.public_key())}


def _stamp(offset: timedelta = timedelta()) -> str:
    return (datetime.now(timezone.utc) + offset).isoformat().replace("+00:00", "Z")


def sign_in() -> None:
    device = identity.device_id()
    claims = Claims(
        organization_id="org_1",
        user_id="usr_me",
        device_id=device,
        key_id="sk_1",
        issued_at=_stamp(-timedelta(minutes=1)),
        expires_at=_stamp(timedelta(hours=1)),
    )
    cache.save(
        cache.Session(
            endpoint="https://api.example.test",
            device_id=device,
            organization_id="org_1",
            user_id="usr_me",
            entitlement=encode_token(claims, sign(canonical_bytes(claims.to_dict()), ISSUER)),
            keyring=KEYRING,
        )
    )


def confirmation(action: dict) -> str:
    """What the console signs once the person confirms this action."""
    data = canonical_bytes(
        {
            "kind": APPROVAL,
            "key_id": "sk_1",
            "organization_id": "org_1",
            "workspace_id": "",
            "user_id": "usr_me",
            "device_id": identity.device_id(),
            "subject": approval_subject(action["id"], action["preview"]["fingerprint"]),
            "confirmed_at": _stamp(),
        }
    )
    return base64.urlsafe_b64encode(data).decode().rstrip("=") + "." + sign(data, ISSUER)


@pytest.fixture
def asked(db, tmp_path, monkeypatch):
    """A forgotten memory, and an agent's pending request to bring it back."""
    monkeypatch.setenv("FLANNER_HOME", str(tmp_path))
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)  # noqa: S603,S607
    monkeypatch.chdir(root)
    session = get_session()
    proj = create_project(session, name="p", project_root=str(root), auto_gitignore=False)
    memory_id = dispatch(
        "memory_remember",
        {"content": "Deploys go out on Tuesdays.", "category": "fact", "project_id": str(proj.id)},
        surface="cli",
    )["id"]
    dispatch("memory_forget", {"memory_id": memory_id}, surface="cli")
    action = dispatch(
        "request_action",
        {"operation": "memory_restore", "arguments": {"memory_id": memory_id}},
        surface="agent",
    )
    return session, memory_id, action


def restored(session, memory_id: str) -> bool:
    session.expire_all()
    return get_memory(session, uuid.UUID(memory_id)).status == "active"


# --- signed in ---------------------------------------------------------------------------


def test_signed_in_an_apply_without_a_console_confirmation_is_refused(asked):
    session, memory_id, action = asked
    sign_in()

    with pytest.raises(PermissionError, match="console"):
        requested_actions.decide(session, action["id"], approve=True, surface="cli")

    assert not restored(session, memory_id)


def test_signed_in_a_confirmed_apply_goes_through(asked):
    session, memory_id, action = asked
    sign_in()

    decided = requested_actions.decide(
        session, action["id"], approve=True, surface="cli", confirmation=confirmation(action)
    )

    assert decided["state"] == "applied"
    assert restored(session, memory_id)


def test_a_confirmation_for_another_action_does_not_carry_over(asked):
    session, memory_id, action = asked
    sign_in()
    another = {**action, "id": str(uuid.uuid4())}

    with pytest.raises(PermissionError, match="console"):
        requested_actions.decide(
            session, action["id"], approve=True, surface="cli", confirmation=confirmation(another)
        )


def test_signed_in_the_web_ui_sends_you_to_the_terminal(asked):
    session, _, action = asked
    sign_in()

    with pytest.raises(PermissionError, match="terminal"):
        requested_actions.decide(session, action["id"], approve=True, surface="web")


def test_declining_needs_no_confirmation(asked):
    session, memory_id, action = asked
    sign_in()

    decided = requested_actions.decide(session, action["id"], approve=False, surface="web")

    assert decided["state"] == "declined"
    assert not restored(session, memory_id)


def test_the_command_waits_for_the_console_then_applies(asked, monkeypatch):
    session, memory_id, action = asked
    sign_in()
    from flanner import account
    from flanner import cli as cli_module

    monkeypatch.setattr(cli_module, "APPROVAL_POLL_SECONDS", 0)
    monkeypatch.setattr(
        account,
        "request_approval",
        lambda **_: {
            "request_id": "apr_1",
            "url": "https://console.test/approvals",
            "code": "K7QD-42XM",
        },
    )
    replies = iter(
        [{"state": "pending"}, {"state": "confirmed", "confirmation": confirmation(action)}]
    )
    monkeypatch.setattr(account, "approval_status", lambda _: next(replies))

    ran = CliRunner().invoke(cli, ["actions", "apply", action["id"][:8]])

    assert ran.exit_code == 0, ran.output
    assert "K7QD-42XM" in ran.output
    assert restored(session, memory_id)


# --- not signed in -------------------------------------------------------------------------


def test_inside_an_agent_shell_the_command_line_refuses(asked, monkeypatch):
    session, memory_id, action = asked
    monkeypatch.setenv("CLAUDECODE", "1")

    with pytest.raises(PermissionError, match="CLAUDECODE"):
        requested_actions.decide(session, action["id"], approve=True, surface="cli")

    assert not restored(session, memory_id)


def test_without_an_account_the_web_ui_still_applies_which_proves_nobody(asked, monkeypatch):
    """The limit, kept as a test so nobody mistakes it for a guarantee."""
    session, memory_id, action = asked
    monkeypatch.setenv("CLAUDECODE", "1")

    decided = requested_actions.decide(session, action["id"], approve=True, surface="web")

    assert decided["state"] == "applied"
