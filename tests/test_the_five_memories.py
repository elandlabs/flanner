"""The whole point of memory, as one test.

    Save five pieces of project context today, and retrieve the right one
    by keyword in a new session next week.

Everything else in the memory domain is in service of that sentence. It is
written the way a person would meet it: through the command line, in
separate processes, with the catalog deleted in between to prove the files
are the record rather than a cache of one.

Separate processes matter. Every other memory test shares one interpreter,
one database singleton and one warm search index with the code it is
testing, which is exactly the arrangement that hides a bug where something
is held in memory rather than written down.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

#: What somebody would actually save on a Tuesday, and the words they would
#: reach for on the following Monday having forgotten writing any of it.
SESSION_ONE = [
    (
        "decision",
        "Use SQLite advisory locking rather than Redis for the job queue, "
        "because the tool has to keep working with no network.",
    ),
    (
        "constraint",
        "The application must stay usable offline for seven days before it "
        "asks anybody to reconnect.",
    ),
    (
        "lesson",
        "The peer transport test failed behind a corporate firewall that "
        "blocks UDP, not because of anything in our code.",
    ),
    (
        "fact",
        "The production API is rate limited to twenty requests a second, "
        "measured per organisation rather than per device.",
    ),
    (
        "relationship",
        "The authorization design doc is implemented by authz.py and "
        "entitlements.py, and neither one is complete on its own.",
    ),
]

#: The question, and the memory that must come back first for it. None of
#: these repeat the memory's own wording exactly, because somebody who
#: remembered the exact wording would not need to search.
NEXT_WEEK = [
    ("redis queue", "advisory locking"),
    ("how long can it run offline", "seven days"),
    ("firewall UDP", "corporate firewall"),
    ("rate limit", "twenty requests"),
    ("where is authorization implemented", "authz.py"),
]


def _run(args: list[str], *, cwd: Path, home: Path) -> subprocess.CompletedProcess[str]:
    """One flanner command, in its own process with its own environment."""
    env = {**os.environ, "FLANNER_HOME": str(home)}
    env.pop("FLANNER_DB_PATH", None)
    return subprocess.run(  # noqa: S603
        [sys.executable, "-m", "flanner", *args],
        cwd=cwd,
        env=env,
        # Closed, not inherited. `init` asks for a project name and takes
        # the directory when there is nobody to ask, and an inherited
        # terminal would make that depend on how the suite was launched.
        input="",
        capture_output=True,
        text=True,
        timeout=180,
    )


@pytest.fixture
def machine(tmp_path):
    """A repository somebody has adopted, and nothing remembered yet."""
    home = tmp_path / "home"
    home.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)  # noqa: S603,S607

    adopted = _run(["init", "--skip-claude", "--project-root", str(repo)], cwd=repo, home=home)
    assert adopted.returncode == 0, adopted.stdout + adopted.stderr
    return repo, home


def test_five_memories_survive_a_new_session_and_a_lost_database(machine):
    """The acceptance criterion, end to end and across processes."""
    repo, home = machine

    # Tuesday.
    for category, content in SESSION_ONE:
        saved = _run(["mem", "remember", content, "--category", category], cwd=repo, home=home)
        assert saved.returncode == 0, saved.stdout + saved.stderr

    files = sorted((repo / ".flanner" / "memory").glob("*.md"))
    assert len(files) == 5, [f.name for f in files]

    # The catalog is a cache of these files, so losing it must cost nothing
    # but the time to rebuild. Deleted rather than corrupted, because that
    # is the failure people actually have.
    (home / "data.db").unlink()

    readopted = _run(["init", "--skip-claude", "--project-root", str(repo)], cwd=repo, home=home)
    assert readopted.returncode == 0, readopted.stdout + readopted.stderr
    rebuilt = _run(["mem", "rebuild"], cwd=repo, home=home)
    assert rebuilt.returncode == 0, rebuilt.stdout + rebuilt.stderr
    assert "5 adopted" in rebuilt.stdout

    # The following Monday, in a new process, having forgotten the wording.
    for question, expected in NEXT_WEEK:
        found = _run(["mem", "recall", question, "--output", "json"], cwd=repo, home=home)
        assert found.returncode == 0, found.stdout + found.stderr
        answer = json.loads(found.stdout)
        assert answer["memories"], f"nothing came back for {question!r}"
        top = answer["memories"][0]
        body = top.get("body") or top["summary"]
        assert expected in body, (
            f"{question!r} returned {top['title']!r}, which does not contain {expected!r}"
        )


def test_every_result_says_why_it_is_here(machine):
    """A memory a person cannot judge is one they have to go and verify,
    which is the work it was supposed to save."""
    repo, home = machine
    _run(["mem", "remember", SESSION_ONE[0][1], "--category", "decision"], cwd=repo, home=home)

    found = _run(["mem", "recall", "redis", "--output", "json"], cwd=repo, home=home)
    answer = json.loads(found.stdout)

    top = answer["memories"][0]
    for field in ("id", "created_by", "created_at", "scope", "confidence", "match_reason"):
        assert top.get(field), f"{field} was missing from a recall result"
    assert top["match_reason"]


def test_recall_says_it_is_data_rather_than_instructions(machine):
    """The whole prompt-injection surface of the feature. A memory body is
    written by a past self, an agent, or an imported document."""
    repo, home = machine
    _run(["mem", "remember", SESSION_ONE[1][1], "--category", "constraint"], cwd=repo, home=home)

    found = _run(["mem", "recall", "offline", "--output", "json"], cwd=repo, home=home)
    answer = json.loads(found.stdout)

    assert "not instructions" in answer["handling"]


def test_another_project_is_invisible_from_here(machine, tmp_path):
    """Scope, asserted from outside. A memory leaking between repositories
    is worse than no memory, because nobody would think to look for it."""
    repo, home = machine
    other = tmp_path / "other"
    other.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=other, check=True)  # noqa: S603,S607
    adopted = _run(["init", "--skip-claude", "--project-root", str(other)], cwd=other, home=home)
    assert adopted.returncode == 0, adopted.stdout + adopted.stderr

    _run(
        ["mem", "remember", "The staging database is wiped every Sunday.", "--category", "fact"],
        cwd=other,
        home=home,
    )

    found = _run(["mem", "recall", "staging database", "--output", "json"], cwd=repo, home=home)
    answer = json.loads(found.stdout)

    assert answer["memories"] == [], "a memory from another project was recalled"


def test_personal_memory_follows_you_between_projects(machine, tmp_path):
    """The other half of scope: a preference is about the person, not the
    repository, and having to repeat it in every project is the problem
    this feature exists to solve."""
    repo, home = machine
    _run(
        [
            "mem",
            "remember",
            "Prefer reviews that report blockers over ones that report nitpicks.",
            "--category",
            "preference",
            "--scope",
            "personal",
        ],
        cwd=repo,
        home=home,
    )

    other = tmp_path / "other"
    other.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=other, check=True)  # noqa: S603,S607
    _run(["init", "--skip-claude", "--project-root", str(other)], cwd=other, home=home)

    found = _run(["mem", "recall", "blockers nitpicks", "--output", "json"], cwd=other, home=home)
    answer = json.loads(found.stdout)

    assert answer["memories"], "a personal preference did not follow to another project"
    assert answer["memories"][0]["scope"] == "personal"


def test_a_credential_never_reaches_the_disk(machine):
    """Asserted from outside the process that refused it, because the file
    is written before the row and a leak would be a file nobody queried."""
    repo, home = machine

    refused = _run(
        [
            "mem",
            "remember",
            "the deploy key is AKIAIOSFODNN7EXAMPLE, do not lose it",
            "--category",
            "fact",
        ],
        cwd=repo,
        home=home,
    )

    assert refused.returncode == 1
    assert list((repo / ".flanner" / "memory").glob("*.md")) == []
    written = "\n".join(
        path.read_text(encoding="utf-8", errors="replace")
        for path in home.rglob("*")
        if path.is_file() and path.suffix in {".md", ".log", ".json"}
    )
    assert "AKIAIOSFODNN7EXAMPLE" not in written


def test_memory_files_are_kept_out_of_commits(machine):
    """A memory is meant to be private to the machine unless somebody
    decides otherwise, and committing it is deciding by accident."""
    repo, home = machine
    _run(["mem", "remember", SESSION_ONE[0][1], "--category", "decision"], cwd=repo, home=home)

    status = subprocess.run(  # noqa: S603,S607
        ["git", "status", "--porcelain", "--ignored"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )

    assert ".flanner/memory/" in (repo / ".gitignore").read_text(encoding="utf-8")
    tracked = [line for line in status.stdout.splitlines() if line.startswith("??")]
    assert not any(".flanner/memory" in line for line in tracked)
