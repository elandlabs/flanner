"""Reading an agent's skills: discovery, precedence, health and the surfaces.

Built around a fake `~/.claude` tree rather than the developer's real one,
because the interesting cases — a plugin revision that is no longer
installed, the same skill name in three places, a manifest with no
frontmatter — are exactly the ones a healthy machine does not have.
"""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from flanner import skills_adapters as adapters
from flanner import skills_ops
from flanner.cli import cli


def write_skill(root, name, *, description="Does a thing", frontmatter=True, extra=None):
    """One skill package on disk, as an author would lay it out."""
    package = root / name
    package.mkdir(parents=True, exist_ok=True)
    body = (
        f"---\nname: {name}\ndescription: {description}\n---\n\nBody.\n"
        if frontmatter
        else "No frontmatter here.\n"
    )
    (package / "SKILL.md").write_text(body, encoding="utf-8")
    if extra:
        (package / "reference.md").write_text(extra, encoding="utf-8")
    return package


@pytest.fixture
def machine(tmp_path, monkeypatch):
    """A home directory with user skills and two plugin revisions.

    `beta` exists at both revisions and the older one is not the installed
    one, which is the case a scan has to get right: both look identical on
    disk and only `installed_plugins.json` says which the agent loads.
    """
    home = tmp_path / "home"
    monkeypatch.setenv("FLANNER_SKILLS_HOME", str(home))
    claude = home / ".claude"

    write_skill(claude / "skills", "alpha", description="Yours")
    write_skill(claude / "skills", "beta", description="Yours too")

    cache = claude / "plugins" / "cache" / "market" / "kit"
    write_skill(cache / "1.0.0" / "skills", "beta", description="Old plugin copy")
    write_skill(cache / "2.0.0" / "skills", "beta", description="New plugin copy")
    write_skill(cache / "2.0.0" / "skills", "gamma", description="Only in the plugin")

    (claude / "plugins" / "installed_plugins.json").write_text(
        json.dumps({"plugins": {"kit@market": [{"version": "2.0.0"}]}}), encoding="utf-8"
    )
    return tmp_path


@pytest.fixture
def project(tmp_path):
    """A repository with one skill of its own."""
    where = tmp_path / "repo"
    write_skill(where / ".claude" / "skills", "alpha", description="This project's")
    return where


@pytest.fixture
def codex_machine(tmp_path, monkeypatch):
    """The same home, with Codex's own layout beside Claude Code's.

    `.agents/skills` rather than `.claude/skills`, and no plugin cache:
    Codex has no equivalent, which is why its adapter is three
    directories rather than a walk.
    """
    home = tmp_path / "home"
    monkeypatch.setenv("FLANNER_SKILLS_HOME", str(home))
    write_skill(home / ".agents" / "skills", "review", description="Yours, for Codex")
    return tmp_path


@pytest.fixture
def codex_project(tmp_path):
    where = tmp_path / "repo"
    write_skill(where / ".agents" / "skills", "deploy", description="This project's, for Codex")
    return where


# --- discovery ----------------------------------------------------------------


def test_every_layout_on_disk_is_found(machine, project):
    packages = skills_ops.scan(project)
    found = {(p.name, p.scope, p.revision) for p in packages}
    assert found == {
        ("alpha", "project", None),
        ("alpha", "user", None),
        ("beta", "user", None),
        ("beta", "plugin", "1.0.0"),
        ("beta", "plugin", "2.0.0"),
        ("gamma", "plugin", "2.0.0"),
    }


def test_a_marketplace_checkout_is_a_root_too(machine):
    """The layout that was two thirds of a real machine's packages."""
    home = machine / "home"
    write_skill(home / ".claude" / "plugins" / "marketplaces" / "market" / "skills", "delta")
    assert any(p.name == "delta" for p in skills_ops.scan())


def test_a_skill_one_level_deeper_is_still_found(machine):
    """Some plugins interpose a version directory under `skills/`."""
    home = machine / "home"
    cache = home / ".claude" / "plugins" / "cache" / "market" / "kit" / "2.0.0" / "skills"
    write_skill(cache / "v1", "nested")
    assert any(p.name == "nested" for p in skills_ops.scan())


def test_a_vendored_copy_for_another_harness_is_not_counted(machine):
    """`<revision>/.openclaw/skills` is a copy Claude Code never loads."""
    home = machine / "home"
    revision = home / ".claude" / "plugins" / "cache" / "market" / "kit" / "2.0.0"
    write_skill(revision / ".openclaw" / "skills", "vendored")
    assert not any(p.name == "vendored" for p in skills_ops.scan())


def test_an_unknown_agent_is_empty_rather_than_an_error(machine):
    assert skills_ops.scan(agent="no-such-agent") == []


# --- precedence ---------------------------------------------------------------


def test_the_adapter_says_what_it_cannot_do(machine):
    """Declared, not assumed. A surface that reports zero usage where it
    simply cannot observe is worse than one that says it does not know.

    Claude Code can be observed — a hook on its own settings does it — and
    the note that mattered is the one about what such a hook can see. The
    page prints these, so a stale one is the page telling somebody their
    machine cannot do something it does.
    """
    capability = adapters.adapter_for("claude-code").capability()
    assert capability.discover and capability.resolve_precedence
    assert capability.observe
    assert any("explicit invocations" in note for note in capability.notes)
    assert not any("not implemented" in note for note in capability.notes)


def test_codex_declares_that_it_resolves_nothing():
    """Its docs say two skills sharing a name are not merged, so an
    inventory that named a winner would be describing a rule Codex does
    not have."""
    capability = adapters.adapter_for("codex").capability()
    assert capability.discover and not capability.resolve_precedence
    assert any("share a name" in note for note in capability.notes)
    # The two places it deliberately does not look are on the record.
    assert any("bundled" in note for note in capability.notes)


def test_codex_skills_are_found_and_carry_their_agent(codex_machine, codex_project):
    packages = skills_ops.scan(codex_project)
    codex = {(p.name, p.scope) for p in packages if p.agent == "codex"}
    assert codex == {("deploy", "project"), ("review", "user")}
    assert all(
        p.agent == "codex" for p in packages if "deploy" in p.name and ".agents" in p.directory
    )


def test_codex_offers_every_copy_rather_than_shadowing(codex_machine, codex_project):
    """Both copies of a name are in effect, because both reach the selector."""
    write_skill(codex_project / ".agents" / "skills", "review", description="This project's")

    copies = [
        p for p in skills_ops.scan(codex_project) if p.agent == "codex" and p.name == "review"
    ]
    assert len(copies) == 2
    assert all(p.effective for p in copies)

    codes = {f.code for f in skills_ops.diagnose(copies)}
    assert "ambiguous_package" in codes and "shadowed_package" not in codes


def test_one_name_under_two_agents_is_not_a_collision(machine, project, codex_machine):
    """Claude Code and Codex each loading an `alpha` is two skills that
    share a name, not two copies of one.

    The fixture's `alpha` already sits in two Claude Code roots, so there
    is a genuine collision to report. What must not happen is Codex's copy
    being counted into it and somebody being sent to delete a file the
    other agent needs.
    """
    codex_copy = write_skill(project / ".agents" / "skills", "alpha", description="Codex's own")

    packages = skills_ops.scan(project)
    assert {p.agent for p in packages if p.name == "alpha"} == {"claude-code", "codex"}
    # Codex's copy loads, whatever Claude Code's copies do to each other.
    assert next(p for p in packages if p.name == "alpha" and p.agent == "codex").effective

    collisions = [f for f in skills_ops.diagnose(packages) if f.skill == "alpha"]
    assert [f.code for f in collisions] == ["shadowed_package"]
    assert "2 copies differ" in collisions[0].detail
    assert str(codex_copy) not in collisions[0].evidence


def test_a_scan_covers_every_agent_and_says_which(machine, codex_machine, project):
    report = skills_ops.report(project)
    assert report["agents"] == ["claude-code", "codex"]
    assert report["summary"]["by_agent"]["codex"] >= 1
    assert report["summary"]["by_agent"]["claude-code"] >= 1
    # Every root says whose it is, and each note names its agent.
    assert {r["agent"] for r in report["coverage"]["roots"]} == {"claude-code", "codex"}
    assert all(note.split(":")[0] in adapters.ADAPTERS for note in report["coverage"]["notes"])


def test_asking_for_one_agent_leaves_the_other_out(machine, codex_machine, project):
    only = skills_ops.scan(project, agent="codex")
    assert only and {p.agent for p in only} == {"codex"}


def test_the_narrowest_scope_wins(machine, project):
    effective = {p.name: p for p in skills_ops.scan(project) if p.effective}
    assert effective["alpha"].scope == "project"
    assert effective["beta"].scope == "user"
    assert effective["gamma"].scope == "plugin"
    assert len(effective) == 3


def test_an_uninstalled_revision_never_wins(machine):
    """Two plugin copies, and the one the agent installed is the one loaded."""
    home = machine / "home"
    (home / ".claude" / "skills" / "beta" / "SKILL.md").unlink()
    (home / ".claude" / "skills" / "beta").rmdir()

    winner = next(p for p in skills_ops.scan() if p.name == "beta" and p.effective)
    assert winner.revision == "2.0.0"


def test_precedence_is_stable_between_runs(machine, project):
    first = [(p.name, p.directory, p.effective) for p in skills_ops.scan(project)]
    assert first == [(p.name, p.directory, p.effective) for p in skills_ops.scan(project)]


# --- hashes -------------------------------------------------------------------


def test_the_hash_covers_the_whole_package(machine, tmp_path):
    package = write_skill(tmp_path / "one", "thing", extra="a")
    before, files, _size, _cut = skills_ops.manifest_hash(package)
    (package / "reference.md").write_text("b", encoding="utf-8")
    after, _files, _size, _cut = skills_ops.manifest_hash(package)
    assert files == 2
    assert before != after


def test_moving_a_file_changes_the_hash(machine, tmp_path):
    package = write_skill(tmp_path / "two", "thing", extra="a")
    before, *_ = skills_ops.manifest_hash(package)
    (package / "reference.md").rename(package / "notes.md")
    after, *_ = skills_ops.manifest_hash(package)
    assert before != after


# --- health -------------------------------------------------------------------


def test_a_manifest_with_no_frontmatter_is_reported(machine):
    home = machine / "home"
    write_skill(home / ".claude" / "skills", "broken", frontmatter=False)
    findings = skills_ops.diagnose(skills_ops.scan())
    assert any(f.code == "manifest_invalid" and f.skill == "broken" for f in findings)


def test_a_name_that_disagrees_with_its_directory_is_reported(machine):
    home = machine / "home"
    package = write_skill(home / ".claude" / "skills", "mislabelled")
    (package / "SKILL.md").write_text(
        "---\nname: something-else\ndescription: x\n---\n", encoding="utf-8"
    )
    findings = skills_ops.diagnose(skills_ops.scan())
    assert any(f.code == "manifest_invalid" and f.skill == "mislabelled" for f in findings)


def test_duplicate_copies_are_reported_once_per_name(machine, project):
    findings = skills_ops.diagnose(skills_ops.scan(project))
    named = [f for f in findings if f.skill == "beta" and f.code == "shadowed_package"]
    assert len(named) == 1


def test_copies_from_one_plugin_are_advice_not_a_defect(tmp_path, monkeypatch):
    """Several differing revisions of one plugin is how an agent stores a
    plugin it has updated. Calling that a defect would fail `doctor` on
    every machine that has ever updated a plugin, which makes the gate
    worthless — the check has to fire on the case that actually costs
    somebody an afternoon."""
    home = tmp_path / "one-plugin"
    monkeypatch.setenv("FLANNER_SKILLS_HOME", str(home))
    cache = home / ".claude" / "plugins" / "cache" / "market" / "kit"
    write_skill(cache / "1.0.0" / "skills", "shared", description="Old")
    write_skill(cache / "2.0.0" / "skills", "shared", description="New")

    findings = skills_ops.diagnose(skills_ops.scan())
    shadowed = [f for f in findings if f.code == "shadowed_package"]
    assert shadowed and all(f.severity == skills_ops.ADVICE for f in shadowed)


def test_copies_from_different_places_are_a_defect(machine, project):
    """A project copy and a plugin copy of one name, differing: this is the
    'I edited it and nothing changed' case."""
    findings = skills_ops.diagnose(skills_ops.scan(project))
    beta = next(f for f in findings if f.skill == "beta" and f.code == "shadowed_package")
    assert beta.severity == skills_ops.DEFECT


def test_an_uninstalled_revision_is_advice_not_a_defect(machine):
    findings = skills_ops.diagnose(skills_ops.scan())
    stale = [f for f in findings if f.code == "stale_plugin_revision"]
    assert stale and all(f.severity == skills_ops.ADVICE for f in stale)


def test_a_healthy_machine_has_nothing_to_report(tmp_path, monkeypatch):
    home = tmp_path / "clean"
    monkeypatch.setenv("FLANNER_SKILLS_HOME", str(home))
    write_skill(home / ".claude" / "skills", "only-one")
    assert skills_ops.diagnose(skills_ops.scan()) == []


# --- the report ---------------------------------------------------------------


def test_the_report_says_what_it_could_not_do(machine):
    report = skills_ops.report()
    assert report["coverage"]["observation"] == "unsupported"
    assert report["coverage"]["notes"]
    assert report["coverage"]["roots"]


def test_the_report_is_json(machine, project):
    payload = json.loads(skills_ops.to_json(skills_ops.report(project)))
    assert payload["summary"]["effective"] == 3
    assert payload["summary"]["packages"] == 6


def test_handing_back_a_scan_gives_the_same_report(machine, project):
    packages = skills_ops.scan(project)
    once = skills_ops.report(project, packages=packages)
    twice = skills_ops.report(project)
    assert once["packages"] == twice["packages"]
    assert once["summary"] == twice["summary"]


# --- the catalog --------------------------------------------------------------


def test_recording_twice_adds_nothing_the_second_time(machine, tmp_path):
    from flanner.database import SkillModel, SkillVersionModel, get_session, init_database

    init_database(str(tmp_path / "catalog.db"))
    session = get_session()
    packages = skills_ops.scan()

    first = skills_ops.record(session, packages)
    second = skills_ops.record(session, packages)

    assert first["skills_added"] == len(packages)
    assert second == {"skills_added": 0, "versions_recorded": 0}
    assert session.query(SkillModel).count() == len(packages)
    assert session.query(SkillVersionModel).count() == len(packages)


def test_changed_bytes_add_a_version_and_keep_the_old_one(machine, tmp_path):
    from flanner.database import SkillVersionModel, get_session, init_database

    init_database(str(tmp_path / "history.db"))
    session = get_session()
    skills_ops.record(session, skills_ops.scan())

    manifest = machine / "home" / ".claude" / "skills" / "alpha" / "SKILL.md"
    manifest.write_text(
        "---\nname: alpha\ndescription: Changed\n---\n\nDifferent.\n", encoding="utf-8"
    )
    before = {v.manifest_hash for v in session.query(SkillVersionModel).all()}
    written = skills_ops.record(session, skills_ops.scan())
    after = {v.manifest_hash for v in session.query(SkillVersionModel).all()}

    assert written == {"skills_added": 0, "versions_recorded": 1}
    # The point of appending: the hash from before the edit is still on file,
    # so "when did this change" has an answer.
    assert before < after


# --- the command line ---------------------------------------------------------


@pytest.fixture
def runner(machine, monkeypatch, tmp_path):
    monkeypatch.setenv("FLANNER_HOME", str(tmp_path / "flanner-home"))
    monkeypatch.chdir(tmp_path)
    return CliRunner()


def test_scan_prints_the_counts(runner):
    result = runner.invoke(cli, ["skills", "scan", "--no-record"])
    assert result.exit_code == 0, result.output
    assert "3 skills in effect" in result.output


def test_scan_as_json_is_parseable(runner):
    result = runner.invoke(cli, ["skills", "scan", "--no-record", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["summary"]["effective"] == 3


def test_list_hides_shadowed_copies_until_asked(runner):
    plain = runner.invoke(cli, ["skills", "list"])
    every = runner.invoke(cli, ["skills", "list", "--all"])
    assert plain.exit_code == 0, plain.output
    assert "shadowed" not in plain.output
    assert "shadowed" in every.output


def test_list_filters_by_scope(runner):
    result = runner.invoke(cli, ["skills", "list", "--scope", "plugin"])
    assert result.exit_code == 0, result.output
    assert "gamma" in result.output
    assert "alpha" not in result.output


def test_doctor_fails_on_a_defect(runner, machine):
    write_skill(machine / "home" / ".claude" / "skills", "broken", frontmatter=False)
    result = runner.invoke(cli, ["skills", "doctor"])
    assert result.exit_code == 1
    assert "manifest_invalid" in result.output


def test_doctor_passes_when_there_is_only_advice(tmp_path, monkeypatch):
    """Advice alone must not break somebody's build.

    A machine with an old plugin revision still cached, and no two packages
    sharing a name: everything the agent loads is unambiguous, and the
    leftover revision is worth mentioning and nothing more.
    """
    home = tmp_path / "advice-only"
    monkeypatch.setenv("FLANNER_SKILLS_HOME", str(home))
    monkeypatch.setenv("FLANNER_HOME", str(tmp_path / "flanner-home"))
    monkeypatch.chdir(tmp_path)

    cache = home / ".claude" / "plugins" / "cache" / "market" / "kit"
    write_skill(cache / "1.0.0" / "skills", "was-here")
    write_skill(cache / "2.0.0" / "skills", "still-here")
    (home / ".claude" / "plugins" / "installed_plugins.json").write_text(
        json.dumps({"plugins": {"kit@market": [{"version": "2.0.0"}]}}), encoding="utf-8"
    )

    result = CliRunner().invoke(cli, ["skills", "doctor"])
    assert result.exit_code == 0, result.output
    assert "stale_plugin_revision" in result.output


def test_inspect_shows_every_copy(runner):
    result = runner.invoke(cli, ["skills", "inspect", "beta"])
    assert result.exit_code == 0, result.output
    assert result.output.count("sha256:") == 3


def test_inspect_says_so_when_there_is_no_such_skill(runner):
    result = runner.invoke(cli, ["skills", "inspect", "not-a-skill"])
    assert result.exit_code == 1
    assert "No skill named" in result.output


# --- the local web page -------------------------------------------------------


def test_the_web_page_renders_the_same_report(machine, tmp_path, monkeypatch):
    from starlette.testclient import TestClient

    monkeypatch.setenv("FLANNER_HOME", str(tmp_path / "web-home"))
    monkeypatch.chdir(tmp_path)
    from flanner.web import app

    client = TestClient(app, base_url="http://127.0.0.1:8000")
    page = client.get("/skills")
    assert page.status_code == 200
    assert "gamma" in page.text
    assert page.text.count('cols-skills" data-list-item') == 3

    with_shadowed = client.get("/skills?shadowed=1")
    assert with_shadowed.text.count('cols-skills" data-list-item') == 5
