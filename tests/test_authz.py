"""Who may act, and what happens when nobody can say.

The interesting cases are not the happy ones. A project that has joined a
team must never become *more* permissive because its entitlement lapsed,
and that is the property most of these tests are about.
"""

from datetime import datetime, timedelta, timezone

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from flanner import authz, workflow
from flanner import session as cache
from flanner.database import ProjectModel
from flanner.entitlements import Claims, WorkspaceCapability, encode_token
from flanner.identity import public_key_b64, sign
from flanner.workflow import EDITOR, MAINTAINER, READER

WORKSPACE = "ws_core"


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("FLANNER_HOME", str(tmp_path))
    return tmp_path


@pytest.fixture
def solo():
    return ProjectModel(name="solo", workspace_id=None)


@pytest.fixture
def joined():
    return ProjectModel(name="joined", workspace_id=WORKSPACE)


def sign_in(role=EDITOR, *, workspace=WORKSPACE, user="maria", expires_in=timedelta(hours=1)):
    """Cache a genuinely signed entitlement for this device."""
    key = Ed25519PrivateKey.generate()
    now = datetime.now(timezone.utc)
    claims = Claims(
        organization_id="org_1",
        user_id=user,
        device_id="dev_abc",
        key_id="sk_1",
        issued_at=(now - timedelta(minutes=1)).isoformat().replace("+00:00", "Z"),
        expires_at=(now + expires_in).isoformat().replace("+00:00", "Z"),
        workspace_capabilities=(
            (WorkspaceCapability(workspace_id=workspace, role=role),) if role else ()
        ),
    )
    from flanner.artifacts import canonical_bytes

    token = encode_token(claims, sign(canonical_bytes(claims.to_dict()), key))
    session = cache.Session(
        endpoint="https://api.example.test",
        device_id="dev_abc",
        organization_id="org_1",
        user_id=user,
        entitlement=token,
        keyring={"sk_1": public_key_b64(key.public_key())},
    )
    cache.save(session)
    return session


# --- solo ------------------------------------------------------------------


def test_a_project_with_no_workspace_stays_advisory(home, solo):
    result = authz.resolve(solo)
    assert result.source == authz.LOCAL
    assert result.enforced is False
    assert result.roles.get("anyone") == MAINTAINER


def test_an_entitlement_does_not_reach_into_an_unjoined_project(home, solo):
    """A local workspace id can appear in no entitlement, so none applies."""
    sign_in(MAINTAINER)
    assert authz.resolve(solo).source == authz.LOCAL


def test_the_local_workspace_id_is_derived_from_the_project(home, solo):
    assert authz.resolve(solo).workspace_id == f"local:{solo.id}"


# --- joined ----------------------------------------------------------------


def test_a_signed_capability_becomes_the_role_map(home, joined):
    sign_in(EDITOR)
    result = authz.resolve(joined)

    assert result.enforced is True
    assert result.actor == "maria"
    assert result.roles == {"maria": EDITOR}


def test_the_actor_defaults_to_whoever_the_entitlement_names(home, joined):
    """Keeping the local placeholder would key the map to a user nobody is."""
    sign_in(MAINTAINER, user="raj")
    assert authz.resolve(joined).actor == "raj"


def test_a_name_that_is_not_the_signed_in_user_is_refused(home, joined):
    """It used to be honoured, which handed this user's role to any string:
    `actor="agent"` got the reader's authority under the name "agent"."""
    sign_in(READER)
    result = authz.resolve(joined, actor="agent")

    assert result.roles == {}
    assert result.enforced is True
    assert "signed in as" in result.reason


def test_naming_yourself_is_the_same_as_naming_nobody(home, joined):
    sign_in(READER, user="raj")
    assert authz.resolve(joined, actor="raj").roles == authz.resolve(joined).roles


def test_a_capability_for_another_workspace_grants_nothing_here(home, joined):
    sign_in(MAINTAINER, workspace="ws_somewhere_else")
    result = authz.resolve(joined)

    assert result.roles == {}
    assert "no role in this workspace" in result.reason


# --- the failure direction -------------------------------------------------


def test_a_joined_project_with_no_session_authorizes_nobody(home, joined):
    result = authz.resolve(joined)
    assert result.roles == {}
    assert result.enforced is True, "silence must not read as permission"


@pytest.mark.parametrize(
    "case",
    [
        pytest.param("expired", id="past the grace window"),
        pytest.param("tampered", id="signature does not verify"),
        pytest.param("unknown_key", id="signed by a key we do not trust"),
    ],
)
def test_an_unusable_entitlement_never_falls_back_to_local(home, joined, case):
    """The one way this could be worse than no authorization at all.

    If a lapsed entitlement fell back to the local placeholder, expiring
    would *grant* maintainer to everyone. Failure has to close, not open.
    """
    session = sign_in(MAINTAINER, expires_in=timedelta(days=-30))
    if case == "tampered":
        session.entitlement = sign_in(MAINTAINER).entitlement[:-4] + "AAAA"
    elif case == "unknown_key":
        session = sign_in(MAINTAINER)
        session.keyring = {"sk_other": session.keyring["sk_1"]}
    cache.save(session)

    result = authz.resolve(joined)
    assert result.roles == {}
    assert result.source == authz.ENTITLEMENT
    assert result.roles.get("anyone") is None


def test_a_corrupt_cache_authorizes_nobody_on_a_joined_project(home, joined):
    cache.session_path().write_text("{ not json", encoding="utf-8")
    assert authz.resolve(joined).roles == {}


def test_grace_still_authorizes(home, joined):
    """Being offline over a weekend is not a security problem (§11.4)."""
    sign_in(MAINTAINER, expires_in=timedelta(hours=-1))
    result = authz.resolve(joined)
    assert result.roles == {"maria": MAINTAINER}


def test_past_the_grace_window_it_stops(home, joined):
    sign_in(MAINTAINER, expires_in=timedelta(hours=-1))
    later = datetime.now(timezone.utc) + timedelta(days=30)
    assert authz.resolve(joined, now=later).roles == {}


# --- the two surfaces must agree -------------------------------------------


def test_resolving_twice_gives_the_same_answer(home, joined):
    """review and assurance each resolve; a difference would be a bug."""
    sign_in(EDITOR)
    assert authz.resolve(joined) == authz.resolve(joined)


def test_local_roles_answers_for_every_actor(home, solo):
    """Pinned because `roles or {}` silently discards it: it is empty."""
    roles = authz.resolve(solo).roles
    assert not len(roles)
    assert roles["nobody-in-particular"] == workflow.MAINTAINER
