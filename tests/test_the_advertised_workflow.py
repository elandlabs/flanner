"""The workflow the front page describes, from enrolling to opening the file.

Nothing covered this end to end, and that is how a missing production call
survived: `sync_from_peer` stored verified artifacts and never turned them
into anything a person could open. Every sync test asserted
`report.accepted`, which was the number that lied — it counted artifacts
written to a table, and a plan that exists only in that table is invisible
to `flanner list`, to the web UI, and to every agent.

`materialize_version`, the function that would have made it true, was called
by exactly one test file and by no production code at all. Two green test
suites, one broken product.

So this test asserts on the file and the listing rather than on a count, and
it goes through the real surfaces: two device keys, a real signed
entitlement, an http server, and the CLI commands a person actually types.
"""

from __future__ import annotations

import base64
import json
import socket
import subprocess
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from flanner import account, identity, peer
from flanner import session as cache
from flanner.artifacts import canonical_bytes
from flanner.cli import cli
from flanner.database import Base, create_project
from flanner.entitlements import ROSTER, TEAM_SYNC, Claims, WorkspaceCapability, encode_token
from flanner.identity import public_key_b64, sign
from flanner.plan_ops import create_plan
from flanner.workflow import MAINTAINER

WORKSPACE = "ws_team"
PLAN = "architecture"
BODY = "# Architecture\n\nThe decision alice wrote down and bob has to read.\n"

#: `flanner init` offers the directory's name; a blank line accepts it.
ACCEPT_NAME = "\n"


# --- a control plane, reduced to the two things it signs ------------------


@pytest.fixture
def issuer() -> Ed25519PrivateKey:
    """The control plane's signing key. One for both devices, as in life."""
    return Ed25519PrivateKey.generate()


def _entitlement(issuer_key: Ed25519PrivateKey, *, device_id: str, user: str) -> str:
    now = datetime.now(timezone.utc)
    claims = Claims(
        organization_id="org_1",
        user_id=user,
        device_id=device_id,
        key_id="sk_1",
        issued_at=(now - timedelta(minutes=1)).isoformat().replace("+00:00", "Z"),
        expires_at=(now + timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
        # The role says which workspace; the feature says whether syncing is
        # bought at all. A peer refuses without it, so leaving it out would
        # make this test pass on a refusal.
        features=(TEAM_SYNC,),
        workspace_capabilities=(WorkspaceCapability(workspace_id=WORKSPACE, role=MAINTAINER),),
    )
    return encode_token(claims, sign(canonical_bytes(claims.to_dict()), issuer_key))


def _roster(issuer_key: Ed25519PrivateKey, user: str, device_id: str) -> str:
    """The signed roster alice's server holds, naming the teammate who pulls.

    A server refuses a device its roster does not list, so without this the
    pull would stop at the roster rather than at the step under test.
    """
    now = datetime.now(timezone.utc)
    data = canonical_bytes(
        {
            "kind": ROSTER,
            "key_id": "sk_1",
            "organization_id": "org_1",
            "issued_at": now.isoformat().replace("+00:00", "Z"),
            "expires_at": (now + timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
            "workspaces": {
                WORKSPACE: [{"user_id": user, "role": MAINTAINER, "devices": [device_id]}]
            },
        }
    )
    return base64.urlsafe_b64encode(data).decode().rstrip("=") + "." + sign(data, issuer_key)


def _session_for(issuer_key: Ed25519PrivateKey, *, user: str, **extra: Any) -> cache.Session:
    """What this device would hold after enrolling. Reads the real key.

    The device id has to be the one derived from the key on disk under the
    current FLANNER_HOME: `sign_body` refuses to sign when the cached
    session names a different device, which is exactly the mismatch this
    would otherwise introduce silently.
    """
    device_id = identity.device_id()
    return cache.Session(
        endpoint="https://api.example.test",
        device_id=device_id,
        organization_id="org_1",
        user_id=user,
        entitlement=_entitlement(issuer_key, device_id=device_id, user=user),
        keyring={"sk_1": public_key_b64(issuer_key.public_key())},
        **extra,
    )


# --- a device --------------------------------------------------------------


def _repo(where: Path) -> Path:
    where.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(where)], check=True)
    return where


@contextmanager
def _as_device(monkeypatch: Any, home: Path) -> Any:
    """Act as the device that owns this flanner home.

    One process holds one device key, but which one depends on
    FLANNER_HOME: the keychain entry is keyed by it. So two homes really are
    two devices here, with two keys and two ids, rather than one identity
    pretending. The cached session follows the same variable, and is read
    from disk on every call rather than held in memory, so switching homes
    is all it takes to become somebody else.
    """
    monkeypatch.setenv("FLANNER_HOME", str(home))
    yield


@pytest.fixture
def alice(tmp_path, monkeypatch, issuer) -> dict[str, Any]:
    """A teammate with a plan, and an http server that will hand it over.

    Her catalog is bound to its own engine rather than to `get_session`,
    because the server keeps running while the test switches FLANNER_HOME to
    become bob — a server reading ambient state would answer out of the
    wrong database.
    """
    home = tmp_path / "alice-home"
    home.mkdir()
    with _as_device(monkeypatch, home):
        engine = create_engine(f"sqlite:///{tmp_path / 'alice.db'}")
        Base.metadata.create_all(engine)
        maker = sessionmaker(bind=engine)
        session = maker()
        project = create_project(
            session,
            name="alice",
            project_root=str(_repo(tmp_path / "alice-repo")),
            auto_gitignore=False,
        )
        project.workspace_id = WORKSPACE
        session.commit()
        create_plan(session, project=project, name=PLAN, content=BODY, created_by="alice")
        session.commit()

        held = _session_for(issuer, user="alice")
        device_id = held.device_id
        public_key = identity.device_public_key_b64()

    @contextmanager
    def sessions() -> Any:
        made = maker()
        try:
            yield made
        finally:
            made.close()

    port = _free_port()
    stop = _serve(peer.create_peer_app(sessions, lambda: held), port)
    try:
        yield {
            "address": f"http://127.0.0.1:{port}",
            "device_id": device_id,
            "key": public_key,
            "held": held,
        }
    finally:
        stop()


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _serve(app: Any, port: int) -> Any:
    """Run the peer app on loopback until the test is done with it."""
    import uvicorn

    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and not server.started:
        time.sleep(0.05)
    if not server.started:
        raise RuntimeError("the peer server never came up")

    def stop() -> None:
        server.should_exit = True
        thread.join(timeout=10)

    return stop


# --- the workflow ----------------------------------------------------------


def test_enrol_then_pull_and_the_plan_is_there(tmp_path, monkeypatch, issuer, alice) -> None:
    """login -> join -> pull -> a plan somebody can open.

    The last assertion is the one that matters. `pull` reporting `accepted:
    1` was true throughout the defect; a file on disk and a row in `list`
    were not.
    """
    home = tmp_path / "bob-home"
    home.mkdir()
    repo = _repo(tmp_path / "bob-repo")
    runner = CliRunner()

    with _as_device(monkeypatch, home):
        from flanner.database import init_database

        init_database(str(home / "data.db"))
        bob = _session_for(issuer, user="bob")
        alice["held"].roster = _roster(issuer, "bob", bob.device_id)
        monkeypatch.setattr(account, "_post", _control_plane(bob, alice))

        # 1. enrol
        enrolled = runner.invoke(cli, ["login", "code-123", "--endpoint", "https://x.test"])
        assert enrolled.exit_code == 0, enrolled.output
        assert cache.load() is not None, "logging in left nothing cached"
        assert cache.load().device_keys.get(alice["device_id"]) == alice["key"], (
            "enrolling did not learn the teammate whose plan is about to be pulled"
        )

        # 2. a project, bound to the shared workspace
        # The blank line accepts the offered name, which is the directory's.
        started = runner.invoke(
            cli, ["init", "--project-root", str(repo), "--skip-claude"], input=ACCEPT_NAME
        )
        assert started.exit_code == 0, started.output
        joined = runner.invoke(cli, ["join", WORKSPACE, "--project", "bob-repo"])
        assert joined.exit_code == 0, joined.output

        # 3. pull from alice, over http, as the CLI does it
        pulled = runner.invoke(cli, ["peer", "pull", alice["address"], "--project", "bob-repo"])
        assert pulled.exit_code == 0, pulled.output

        # 4. the part nothing tested: is the plan actually here?
        listed = runner.invoke(cli, ["list", "--project", "bob-repo", "--output", "json"])
        assert listed.exit_code == 0, listed.output
        names = [row["name"] for row in json.loads(listed.output)]
        assert PLAN in names, f"pulled and the plan is not in the listing: {listed.output}"

    on_disk = repo / ".plans" / f"{PLAN}_v1.md"
    assert on_disk.exists(), "the listing knows about a file nobody can open"
    assert BODY.strip() in on_disk.read_text(encoding="utf-8"), (
        "the file arrived without alice's content"
    )


def test_a_pull_from_a_stranger_brings_back_nothing(tmp_path, monkeypatch, issuer, alice) -> None:
    """The same path, with the one thing that must stop it.

    An artifact is verified against its author's key, not against whoever
    handed it over. A device that never learned alice cannot accept alice's
    work, and the failure has to be a refusal rather than a plan appearing.
    """
    home = tmp_path / "stranger-home"
    home.mkdir()
    repo = _repo(tmp_path / "stranger-repo")
    runner = CliRunner()

    with _as_device(monkeypatch, home):
        from flanner.database import init_database

        init_database(str(home / "data.db"))
        stranger = _session_for(issuer, user="bob")
        # On the team, so alice serves it; it has just never learned her key.
        alice["held"].roster = _roster(issuer, "bob", stranger.device_id)
        monkeypatch.setattr(account, "_post", _control_plane(stranger, peers=None))

        assert runner.invoke(cli, ["login", "c", "--endpoint", "https://x.test"]).exit_code == 0
        runner.invoke(
            cli, ["init", "--project-root", str(repo), "--skip-claude"], input=ACCEPT_NAME
        )
        joined = runner.invoke(cli, ["join", WORKSPACE, "--project", "stranger-repo"])
        assert joined.exit_code == 0, joined.output

        pulled = runner.invoke(
            cli, ["peer", "pull", alice["address"], "--project", "stranger-repo"]
        )
        # Named, because "nothing arrived" is also what a broken address, an
        # unjoined project or a refused entitlement look like. This test is
        # worth nothing unless it fails at the signature check specifically.
        # Whitespace is collapsed first: Rich wraps to the terminal width, so
        # the phrase arrives with a newline somewhere inside it.
        said = " ".join(pulled.output.lower().split())
        assert "no known key for device" in said, (
            f"refused, but not for the reason under test: {pulled.output}"
        )

        listed = runner.invoke(cli, ["list", "--project", "stranger-repo", "--output", "json"])
        assert json.loads(listed.output) == [], "a plan was accepted from an unknown author"

    assert not (repo / ".plans" / f"{PLAN}_v1.md").exists()


def _control_plane(held: cache.Session, peers: dict[str, Any] | None) -> Any:
    """Stand in for the two endpoints enrolling actually calls."""
    keyring = {peers["device_id"]: peers["key"]} if peers else {}

    def post(
        endpoint: str, path: str, payload: Any, *, repeatable: bool = False
    ) -> dict[str, Any]:
        if path == "/v1/devices/keyring":
            return {"devices": keyring}
        return {
            "device_id": held.device_id,
            "organization_id": held.organization_id,
            "user_id": held.user_id,
            "entitlement": held.entitlement,
            "keyring": held.keyring,
        }

    return post
