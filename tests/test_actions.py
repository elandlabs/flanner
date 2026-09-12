"""One action history, and the actions an agent may only ask for.

Two promises. Something done through any surface is recorded once, under
one id and one person, with no content copied into the record. And an
agent's request to share, install, roll back or restore changes nothing:
a person applies it from the command line or the web UI, and only while
the preview still describes what is there.
"""

from __future__ import annotations

import getpass
import subprocess
from pathlib import Path

import pytest

from flanner import actions, requested_actions, skills_manage
from flanner.database import create_project, get_memory, get_session
from flanner.services import dispatch

SECRET_BODY = "Use advisory locks; the tool must work offline."


@pytest.fixture
def project(db, tmp_path, monkeypatch):
    monkeypatch.setenv("FLANNER_HOME", str(tmp_path))
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)  # noqa: S603,S607
    monkeypatch.chdir(root)
    session = get_session()
    proj = create_project(session, name="p", project_root=str(root), auto_gitignore=False)
    return session, proj, root


def remembered(proj) -> dict:
    return dispatch(
        "memory_remember",
        {"content": SECRET_BODY, "category": "decision", "project_id": str(proj.id)},
        surface=actions.CLI,
    )


# --- the history ---------------------------------------------------------------------


def test_a_write_is_recorded_once_with_its_surface_and_person(project):
    session, proj, _ = project
    done = dispatch(
        "memory_remember",
        {"content": SECRET_BODY, "category": "decision", "project_id": str(proj.id)},
        surface=actions.AGENT,
    )

    rows = actions.recent(session)
    shown = actions.view(actions.get(session, done["action_id"]))

    assert len(rows) == 1
    assert shown["surface"] == "agent"
    assert shown["person"] == getpass.getuser()
    assert shown["operation"] == "memory_remember"
    assert shown["state"] == actions.DONE


def test_the_record_keeps_ids_and_never_content(project):
    session, proj, _ = project
    done = remembered(proj)

    row = actions.get(session, done["action_id"])

    assert SECRET_BODY not in row.detail
    assert actions.view(row)["arguments"] == {"category": "decision", "project_id": str(proj.id)}


def test_a_refused_write_is_recorded_as_failed(project):
    session, _, _ = project
    refused = dispatch(
        "memory_forget", {"memory_id": "00000000-0000-0000-0000-000000000000"}, surface="web"
    )

    assert refused["error"] is True
    assert actions.view(actions.get(session, refused["action_id"]))["state"] == actions.FAILED


def test_the_cli_and_web_ui_record_writes_as_their_own(monkeypatch):
    """A write with no surface would be recorded as unknown."""
    from flanner import cli, services, web

    seen = []
    monkeypatch.setattr(services, "dispatch", lambda op, args, **kw: seen.append(kw) or {})
    cli.dispatch("memory_forget", {})

    assert seen == [{"surface": "cli"}]
    assert web.dispatch.keywords == {"surface": "web"}
    package = Path(__file__).resolve().parent.parent / "flanner"
    for name in ("cli.py", "web.py", "server.py"):
        source = (package / name).read_text(encoding="utf-8")
        assert "from .services import dispatch\n" not in source, name


def test_the_mcp_server_dispatches_as_the_agent():
    from flanner import server

    assert server.dispatch.keywords == {"surface": actions.AGENT}
    assert server.dispatch_optional.keywords == {"surface": actions.AGENT}


# --- asking for a risky action ------------------------------------------------------------


def forgotten(session, proj) -> str:
    memory_id = remembered(proj)["id"]
    dispatch("memory_forget", {"memory_id": memory_id}, surface=actions.CLI)
    assert get_memory(session, __import__("uuid").UUID(memory_id)).status != "active"
    return memory_id


def test_an_agent_request_changes_nothing_until_a_person_applies_it(project):
    session, proj, _ = project
    memory_id = forgotten(session, proj)

    asked = dispatch(
        "request_action",
        {"operation": "memory_restore", "arguments": {"memory_id": memory_id}},
        surface=actions.AGENT,
    )
    assert asked["state"] == actions.PENDING
    assert asked["surface"] == "agent"
    assert asked["preview"]["summary"].startswith("Bring back the memory")
    session.expire_all()
    assert get_memory(session, __import__("uuid").UUID(memory_id)).status != "active"

    applied = dispatch(
        "decide_action",
        {"action_id": asked["id"], "approve": True, "surface": "cli", "at_a_terminal": True},
    )

    assert applied["id"] == asked["id"], "one action, one id, from request to decision"
    assert applied["state"] == actions.APPLIED
    assert applied["decided_by"] == getpass.getuser()
    assert applied["decided_surface"] == "cli"
    session.expire_all()
    assert get_memory(session, __import__("uuid").UUID(memory_id)).status == "active"


def test_an_agent_cannot_decide_a_request(project):
    session, proj, _ = project
    asked = requested_actions.request(
        session, "memory_restore", {"memory_id": forgotten(session, proj)}
    )

    with pytest.raises(PermissionError):
        requested_actions.decide(session, asked["id"], approve=True, surface=actions.AGENT)


def test_declining_changes_nothing(project):
    session, proj, _ = project
    memory_id = forgotten(session, proj)
    asked = requested_actions.request(session, "memory_restore", {"memory_id": memory_id})

    declined = requested_actions.decide(session, asked["id"], approve=False, surface="web")

    assert declined["state"] == actions.DECLINED
    assert get_memory(session, __import__("uuid").UUID(memory_id)).status != "active"


def test_a_preview_that_no_longer_holds_is_not_applied(project):
    session, proj, _ = project
    memory_id = forgotten(session, proj)
    asked = requested_actions.request(session, "memory_restore", {"memory_id": memory_id})
    dispatch("memory_restore", {"memory_id": memory_id}, surface=actions.CLI)

    decided = requested_actions.decide(
        session, asked["id"], approve=True, surface="cli", at_a_terminal=True
    )

    assert decided["state"] == actions.STALE


def test_an_install_request_previews_the_target_and_applies_only_when_approved(project):
    session, proj, root = project
    source = root / "drafts" / "release-notes"
    source.mkdir(parents=True)
    (source / "SKILL.md").write_text(
        "---\nname: release-notes\ndescription: Write release notes\n---\nSteps.\n",
        encoding="utf-8",
    )
    stored = skills_manage.snapshot(source).manifest_hash
    target = root / ".claude" / "skills" / "release-notes"

    asked = dispatch(
        "request_action",
        {"operation": "skills_install", "arguments": {"manifest_hash": stored}},
        surface=actions.AGENT,
    )
    assert "error" not in asked, asked
    assert str(target) in " ".join(asked["preview"]["changes"])
    assert not target.exists()

    applied = dispatch(
        "decide_action",
        {"action_id": asked["id"], "approve": True, "surface": "cli", "at_a_terminal": True},
    )

    assert applied["state"] == actions.APPLIED, applied
    assert (target / "SKILL.md").exists()


def test_an_install_is_stale_when_the_target_changed_after_the_preview(project):
    session, proj, root = project
    source = root / "drafts" / "release-notes"
    source.mkdir(parents=True)
    (source / "SKILL.md").write_text("---\nname: release-notes\n---\nSteps.\n", encoding="utf-8")
    stored = skills_manage.snapshot(source).manifest_hash
    asked = requested_actions.request(
        session, "skills_install", {"manifest_hash": stored, "project_root": str(root)}
    )
    target = root / ".claude" / "skills" / "release-notes"
    target.mkdir(parents=True)
    (target / "SKILL.md").write_text("---\nname: release-notes\n---\nHand edited.\n", "utf-8")

    decided = requested_actions.decide(
        session, asked["id"], approve=True, surface="cli", at_a_terminal=True
    )

    assert decided["state"] == actions.STALE
    assert "Hand edited." in (target / "SKILL.md").read_text(encoding="utf-8")
