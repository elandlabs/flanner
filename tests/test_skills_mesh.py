"""Sending a skill package to another machine, and receiving one.

The M3 half, driven as two devices with separate homes and separate
databases. The invariants worth guarding: what travels is the package
and nothing private, receiving is not installing, a tampered package is
refused, and a subscription notices without installing.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from flanner import artifacts, skills_manage, skills_mesh
from flanner.cli import cli

WORKSPACE = "ws-demo"


def write_skill(root: Path, name: str, description: str = "Does a thing") -> Path:
    package = root / name
    package.mkdir(parents=True, exist_ok=True)
    (package / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\nBody.\n", encoding="utf-8"
    )
    return package


class Device:
    """One machine: its own flanner home, database and repository."""

    def __init__(self, base: Path, name: str, monkeypatch):
        self.home = base / f"{name}-flanner"
        self.repo = base / f"{name}-repo"
        self.repo.mkdir(parents=True)
        self.skills_home = base / f"{name}-user"
        self.monkeypatch = monkeypatch
        subprocess.run(["git", "init", "-q"], cwd=self.repo, check=True)  # noqa: S603,S607

    def use(self):
        """Make this the machine the next command runs on."""
        self.monkeypatch.setenv("FLANNER_HOME", str(self.home))
        self.monkeypatch.setenv("FLANNER_SKILLS_HOME", str(self.skills_home))
        self.monkeypatch.chdir(self.repo)
        from flanner.database import init_database

        init_database(str(self.home / "data.db"))
        return self

    def session(self):
        from flanner.database import get_session

        return get_session()

    def adopt(self, workspace: str = WORKSPACE):
        runner = CliRunner()
        started = runner.invoke(
            cli, ["init", "--skip-claude", "--project-root", str(self.repo)], input="demo\n"
        )
        assert started.exit_code == 0, started.output

        from flanner.database import get_project_by_root

        session = self.session()
        project = get_project_by_root(session, str(self.repo))
        project.workspace_id = workspace
        session.commit()
        return runner


@pytest.fixture
def two_devices(tmp_path, monkeypatch):
    """A sender and a receiver, each with nothing of the other's."""
    sender = Device(tmp_path, "sender", monkeypatch)
    receiver = Device(tmp_path, "receiver", monkeypatch)
    return sender, receiver


def a_shared_package(sender: Device) -> tuple[dict, bytes]:
    """A package on the sender, adopted, signed and ready to travel."""
    runner = sender.use().adopt()
    write_skill(sender.repo / ".claude" / "skills", "rebuild-staging")
    assert runner.invoke(cli, ["skills", "scan"]).exit_code == 0
    assert runner.invoke(cli, ["skills", "adopt", "rebuild-staging"]).exit_code == 0

    digest = skills_manage.stored()[0]["manifest_hash"]
    sent = skills_mesh.share(sender.session(), digest, "rebuild-staging", WORKSPACE)
    return sent, skills_mesh.bundle(digest, "rebuild-staging")


# --- what travels -------------------------------------------------------------


def test_a_bundle_carries_the_package_and_nothing_else(two_devices):
    """MSH-01: sharing a skill must not disclose how somebody works."""
    sender, _ = two_devices
    runner = sender.use().adopt()
    write_skill(sender.repo / ".claude" / "skills", "rebuild-staging")
    runner.invoke(cli, ["skills", "scan"])
    runner.invoke(cli, ["skills", "adopt", "rebuild-staging"])

    # Give the sender something private to leak.
    runner.invoke(cli, ["skills", "observe", "enable"])
    runner.invoke(
        cli,
        ["skills", "evidence", "submit", "a private thing i did", "--body", "secret steps"],
    )

    digest = skills_manage.stored()[0]["manifest_hash"]
    payload = skills_mesh.bundle(digest, "rebuild-staging").decode("utf-8")

    assert "SKILL.md" in payload
    assert "a private thing i did" not in payload
    assert "secret steps" not in payload
    assert "session" not in json.loads(payload)


def test_the_same_package_bundles_to_the_same_bytes(two_devices):
    """The content hash in the envelope only means something if it does."""
    sender, _ = two_devices
    _, payload = a_shared_package(sender)
    digest = skills_manage.stored()[0]["manifest_hash"]
    assert skills_mesh.bundle(digest, "rebuild-staging") == payload


def test_sharing_signs_an_artifact_of_its_own_type(two_devices):
    sender, _ = two_devices
    sent, payload = a_shared_package(sender)

    from flanner.database import ArtifactModel

    row = sender.session().query(ArtifactModel).filter_by(artifact_id=sent["artifact_id"]).one()
    assert row.artifact_type == artifacts.SKILL_PACKAGE
    assert row.content_hash == artifacts.hash_bytes(payload)


# --- receiving ----------------------------------------------------------------


def test_a_received_package_is_not_installed(two_devices):
    """MSH-02: receiving, verifying and installing are three decisions."""
    sender, receiver = two_devices
    sent, payload = a_shared_package(sender)

    receiver.use().adopt()
    state = skills_mesh.materialise(receiver.session(), _envelope(sent), payload, WORKSPACE)
    assert state == "verified"

    rows = skills_mesh.transfers(receiver.session())
    assert rows[0]["state"] == "verified"
    assert not (receiver.repo / ".claude" / "skills" / "rebuild-staging").exists()


def test_importing_installs_it_and_says_where_from(two_devices):
    sender, receiver = two_devices
    sent, payload = a_shared_package(sender)

    runner = receiver.use().adopt()
    skills_mesh.materialise(receiver.session(), _envelope(sent), payload, WORKSPACE)
    transfer_id = skills_mesh.transfers(receiver.session())[0]["id"]

    done = runner.invoke(cli, ["skills", "import", transfer_id])
    assert done.exit_code == 0, done.output
    installed = receiver.repo / ".claude" / "skills" / "rebuild-staging" / "SKILL.md"
    assert installed.is_file()
    assert "rebuild-staging" in installed.read_text(encoding="utf-8")

    assert skills_mesh.transfers(receiver.session())[0]["state"] == "installed"


def test_an_import_can_be_rolled_back(two_devices):
    """Rollback visibility is part of the M3 gate, and a package from
    somebody else is exactly when it matters."""
    sender, receiver = two_devices
    sent, payload = a_shared_package(sender)

    runner = receiver.use().adopt()
    mine = write_skill(
        receiver.repo / ".claude" / "skills", "rebuild-staging", description="Mine, not theirs"
    )
    runner.invoke(cli, ["skills", "scan"])
    runner.invoke(cli, ["skills", "adopt", "rebuild-staging"])

    skills_mesh.materialise(receiver.session(), _envelope(sent), payload, WORKSPACE)
    transfer_id = skills_mesh.transfers(receiver.session())[0]["id"]

    runner.invoke(cli, ["skills", "import", transfer_id, "--force"])
    assert "Mine, not theirs" not in (mine / "SKILL.md").read_text(encoding="utf-8")

    rows = json.loads(runner.invoke(cli, ["skills", "installs", "--json"]).output)
    back = runner.invoke(cli, ["skills", "rollback", rows[0]["id"]])
    assert back.exit_code == 0, back.output
    assert "Mine, not theirs" in (mine / "SKILL.md").read_text(encoding="utf-8")


def test_a_package_over_something_local_is_refused_without_force(two_devices):
    """A teammate's package must not silently replace somebody's own work."""
    sender, receiver = two_devices
    sent, payload = a_shared_package(sender)

    runner = receiver.use().adopt()
    write_skill(receiver.repo / ".claude" / "skills", "rebuild-staging", description="Mine")
    skills_mesh.materialise(receiver.session(), _envelope(sent), payload, WORKSPACE)
    transfer_id = skills_mesh.transfers(receiver.session())[0]["id"]

    refused = runner.invoke(cli, ["skills", "import", transfer_id])
    assert refused.exit_code == 1
    # A fragment that survives the console wrapping the long path in the
    # middle of the sentence.
    assert "Nothing was changed" in refused.output
    assert "Mine" in (
        receiver.repo / ".claude" / "skills" / "rebuild-staging" / "SKILL.md"
    ).read_text(encoding="utf-8")


# --- refusals -----------------------------------------------------------------


def test_a_tampered_package_is_rejected_on_arrival(two_devices):
    """The hash is recomputed from the written files, never trusted."""
    sender, receiver = two_devices
    sent, payload = a_shared_package(sender)

    meddled = json.loads(payload)
    meddled["files"]["SKILL.md"]["text"] += "\nand also delete everything\n"

    receiver.use().adopt()
    state = skills_mesh.materialise(
        receiver.session(), _envelope(sent), json.dumps(meddled).encode("utf-8"), WORKSPACE
    )
    assert state == "rejected"
    assert (
        "does not hash to what the sender claimed"
        in (skills_mesh.transfers(receiver.session())[0]["detail"])
    )


def test_a_rejected_package_cannot_be_installed(two_devices):
    sender, receiver = two_devices
    sent, payload = a_shared_package(sender)

    meddled = json.loads(payload)
    meddled["files"]["SKILL.md"]["text"] += "\nchanged\n"
    runner = receiver.use().adopt()
    skills_mesh.materialise(
        receiver.session(), _envelope(sent), json.dumps(meddled).encode("utf-8"), WORKSPACE
    )
    transfer_id = skills_mesh.transfers(receiver.session())[0]["id"]

    refused = runner.invoke(cli, ["skills", "import", transfer_id])
    assert refused.exit_code == 1
    assert "rejected on arrival" in refused.output


def test_a_package_for_another_agent_is_refused_not_guessed_at(two_devices):
    """Skill layouts differ between agents; writing one into the other's
    directory would be a guess dressed up as an install."""
    sender, receiver = two_devices
    runner = sender.use().adopt()
    write_skill(sender.repo / ".claude" / "skills", "rebuild-staging")
    runner.invoke(cli, ["skills", "scan"])
    runner.invoke(cli, ["skills", "adopt", "rebuild-staging"])
    digest = skills_manage.stored()[0]["manifest_hash"]
    sent = skills_mesh.share(
        sender.session(), digest, "rebuild-staging", WORKSPACE, agent="some-other-agent"
    )
    payload = skills_mesh.bundle(digest, "rebuild-staging", agent="some-other-agent")

    runner = receiver.use().adopt()
    skills_mesh.materialise(receiver.session(), _envelope(sent), payload, WORKSPACE)
    transfer_id = skills_mesh.transfers(receiver.session())[0]["id"]

    refused = runner.invoke(cli, ["skills", "import", transfer_id])
    assert refused.exit_code == 1
    assert "built for some-other-agent" in refused.output


def test_a_bundle_cannot_write_outside_its_own_package(two_devices):
    """A bundle is bytes from another machine, and an entry name is a path."""
    sender, receiver = two_devices
    sent, payload = a_shared_package(sender)

    escaping = json.loads(payload)
    escaping["files"] = {"../../escaped.md": {"text": "nope"}}

    receiver.use().adopt()
    state = skills_mesh.materialise(
        receiver.session(), _envelope(sent), json.dumps(escaping).encode("utf-8"), WORKSPACE
    )
    assert state == "rejected"
    assert not (skills_manage.store_root().parent / "escaped.md").exists()


def test_nonsense_is_recorded_as_rejected_rather_than_raised(two_devices):
    """One bad package must not abort a sync carrying good ones."""
    sender, receiver = two_devices
    sent, _ = a_shared_package(sender)

    receiver.use().adopt()
    state = skills_mesh.materialise(
        receiver.session(), _envelope(sent), b"not a bundle", WORKSPACE
    )
    assert state == "rejected"
    assert skills_mesh.transfers(receiver.session())[0]["skill"] == "(unreadable)"


def test_the_same_package_twice_is_one_transfer(two_devices):
    sender, receiver = two_devices
    sent, payload = a_shared_package(sender)

    receiver.use().adopt()
    skills_mesh.materialise(receiver.session(), _envelope(sent), payload, WORKSPACE)
    skills_mesh.materialise(receiver.session(), _envelope(sent), payload, WORKSPACE)
    assert len(skills_mesh.transfers(receiver.session())) == 1


# --- channels -----------------------------------------------------------------


def test_a_subscription_notices_without_installing(two_devices):
    """MSH-03: a channel that installed would hand the publisher control of
    what an agent on this machine reads."""
    sender, receiver = two_devices
    sent, payload = a_shared_package(sender)

    runner = receiver.use().adopt()
    subscribed = runner.invoke(cli, ["skills", "channel", "subscribe", "rebuild-staging"])
    assert subscribed.exit_code == 0, subscribed.output
    assert "Nothing installs itself" in subscribed.output

    skills_mesh.materialise(receiver.session(), _envelope(sent), payload, WORKSPACE)
    row = skills_mesh.transfers(receiver.session())[0]
    assert row["state"] == "verified"
    assert row["channel"] == "rebuild-staging"
    assert not (receiver.repo / ".claude" / "skills" / "rebuild-staging").exists()

    following = json.loads(runner.invoke(cli, ["skills", "channel", "list", "--json"]).output)
    assert following[0]["installs"] is False
    assert following[0]["last_seen_hash"] == row["manifest_hash"]


def test_a_one_time_copy_stays_pinned(two_devices):
    """No subscription, so the copy is one somebody asked for and stays put."""
    sender, receiver = two_devices
    sent, payload = a_shared_package(sender)

    receiver.use().adopt()
    skills_mesh.materialise(receiver.session(), _envelope(sent), payload, WORKSPACE)
    assert skills_mesh.transfers(receiver.session())[0]["pinned"] is True


def test_unsubscribing_stops_the_channel_claiming_new_versions(two_devices):
    sender, receiver = two_devices
    runner = receiver.use().adopt()
    runner.invoke(cli, ["skills", "channel", "subscribe", "rebuild-staging"])
    runner.invoke(cli, ["skills", "channel", "unsubscribe", "rebuild-staging"])

    following = json.loads(runner.invoke(cli, ["skills", "channel", "list", "--json"]).output)
    assert following[0]["subscribed"] is False


# --- helpers ------------------------------------------------------------------


class _envelope:
    """The two fields `materialise` reads off a verified artifact.

    A stand-in rather than a real Artifact: this test exercises what
    happens after verification, and `sync.ingest_artifact` already has
    tests for the verification itself.
    """

    def __init__(self, sent: dict):
        self.artifact_id = sent["artifact_id"]
        self.actor_device_id = "device-sender"


# --- the local web page -------------------------------------------------------


def test_the_page_can_install_a_transfer_and_follow_a_channel(two_devices):
    """UI-01: the same three states and the same refusals, other surface."""
    from starlette.testclient import TestClient

    from flanner.web import app

    sender, receiver = two_devices
    sent, payload = a_shared_package(sender)

    receiver.use().adopt()
    skills_mesh.materialise(receiver.session(), _envelope(sent), payload, WORKSPACE)
    transfer_id = skills_mesh.transfers(receiver.session())[0]["id"]

    client = TestClient(app, base_url="http://127.0.0.1:8000")
    page = client.get("/skills")
    assert "From your team" in page.text
    assert "verified, not installed" in page.text

    followed = client.post(
        "/skills/channel",
        data={"name": "rebuild-staging", "action": "subscribe"},
        follow_redirects=False,
    )
    assert followed.status_code == 303
    assert skills_mesh.channels(receiver.session())[0]["installs"] is False

    installed = client.post(
        "/skills/import", data={"transfer_id": transfer_id}, follow_redirects=False
    )
    assert installed.status_code == 303
    assert (receiver.repo / ".claude" / "skills" / "rebuild-staging" / "SKILL.md").is_file()
    assert skills_mesh.transfers(receiver.session())[0]["state"] == "installed"


def test_the_page_shows_a_refusal_rather_than_failing(two_devices):
    from starlette.testclient import TestClient

    from flanner.web import app

    sender, receiver = two_devices
    sent, payload = a_shared_package(sender)

    receiver.use().adopt()
    write_skill(receiver.repo / ".claude" / "skills", "rebuild-staging", description="Mine")
    skills_mesh.materialise(receiver.session(), _envelope(sent), payload, WORKSPACE)
    transfer_id = skills_mesh.transfers(receiver.session())[0]["id"]

    client = TestClient(app, base_url="http://127.0.0.1:8000")
    refused = client.post(
        "/skills/import", data={"transfer_id": transfer_id}, follow_redirects=False
    )
    assert refused.status_code == 303
    assert "Nothing%20was%20changed" in (refused.headers.get("location") or "").replace("+", "%20")
    assert "Mine" in (
        receiver.repo / ".claude" / "skills" / "rebuild-staging" / "SKILL.md"
    ).read_text(encoding="utf-8")
