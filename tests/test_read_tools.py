"""The read-only tools an agent reaches for before it acts.

Each has two promises to keep: it answers the question, and it changes
nothing. Asking what is installed is not adopting it, and asking where you
are must not mint a device key or reach the network.
"""

import asyncio

import pytest

from flanner import identity
from flanner.database import SkillModel, get_session
from flanner.server import (
    create_project_tool,
    mesh_status,
    project_context,
    skills_report,
    skills_usage,
)

SKILL = "flanner-read-tool-probe"
TOOLS = {"project_context", "skills_report", "skills_usage", "mesh_status"}


@pytest.fixture
def repo(db, git_repo, monkeypatch):
    monkeypatch.chdir(git_repo)
    return git_repo


@pytest.fixture
def adopted(repo):
    result = create_project_tool(name="probe", project_root=str(repo))
    assert not result.get("error"), result
    return result


@pytest.fixture
def a_skill(repo):
    folder = repo / ".claude" / "skills" / SKILL
    folder.mkdir(parents=True)
    manifest = [
        "---",
        f"name: {SKILL}",
        "description: A probe for the read tools.",
        "---",
        "",
        "# Probe",
        "",
    ]
    (folder / "SKILL.md").write_text("\n".join(manifest), encoding="utf-8")
    return folder


def test_the_four_tools_are_advertised():
    from flanner import server

    names = {tool.name for tool in asyncio.run(server._mcp.list_tools())}
    assert names >= TOOLS


# --- project_context ---------------------------------------------------------


def test_context_outside_a_project_says_how_to_adopt_one(repo):
    context = project_context()

    assert context["project"] is None
    assert "initialize_project_tool" in context["next"]
    assert context["features"] == {"integrations": False}
    assert context["signed_in"] is False


def test_context_inside_a_project_names_it_and_its_authority(adopted):
    context = project_context()

    assert context["project"]["id"] == adopted["id"]
    # Solo: review is recorded and binds nobody, which the agent needs to know.
    assert context["review"]["enforced"] is False
    assert context["memory"]["capture_mode"] == "suggest"
    assert context["skills_watching"] == []


def test_asking_where_you_are_mints_no_device_key(repo):
    """identity.device_id() creates a key on first use. None of these call it."""
    key, marker = identity.device_key_path(), identity.keychain_marker_path()
    assert not key.exists() and not marker.exists()

    project_context()
    mesh_status()

    assert not key.exists() and not marker.exists()


# --- mesh_status -------------------------------------------------------------


def test_mesh_status_signed_out_says_so(repo):
    status = mesh_status()

    assert status["signed_in"] is False
    assert "flanner login" in status["message"]


# --- skills_report -----------------------------------------------------------


def test_skills_report_finds_a_skill_and_which_copy_loads(repo, a_skill):
    report = skills_report()

    assert {"summary", "findings", "packages"} <= set(report)
    rows = [p for p in report["packages"] if p["name"] == SKILL]
    assert rows, "the probe skill was not found"
    assert rows[0]["loads"] is True


def test_naming_a_skill_returns_its_copies_and_files(repo, a_skill):
    detail = skills_report(name=SKILL)

    assert [f["path"] for f in detail["files"]] == ["SKILL.md"]
    assert detail["copies"][0]["directory"].endswith(SKILL)
    assert detail["differences"] == []


def test_an_unknown_skill_is_an_answer_not_an_exception(repo, a_skill):
    detail = skills_report(name="no-such-skill-anywhere")

    assert detail["error"] is True
    assert SKILL in detail["known"]


def test_reading_skills_records_nothing(repo, a_skill):
    """The Skills page records the scan it runs. An agent asking must not."""
    before = get_session().query(SkillModel).count()

    skills_report()
    skills_report(name=SKILL)

    assert get_session().query(SkillModel).count() == before


# --- skills_usage ------------------------------------------------------------


def test_usage_says_whether_anything_was_watching(adopted):
    usage = skills_usage(days=7)

    assert usage["watching"] == []
    assert usage["usage"]["coverage"]["watching"] is False
    assert isinstance(usage["attention"], list)


def test_usage_clamps_an_absurd_window(adopted):
    assert skills_usage(days=100000)["usage"]["window_days"] == 365
    assert skills_usage(days=0)["usage"]["window_days"] == 1
