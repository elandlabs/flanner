"""What a project will let be remembered, and what it may not decide alone.

The merge rule is the reason this module exists. A repository is something
you clone from somebody else, so a policy file that could loosen would be a
way to talk a stranger's machine into capturing more than they agreed to.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from flanner import memory_policy
from flanner.exceptions import ValidationError


@pytest.fixture
def places(tmp_path):
    """A flanner home and a project, either of which may hold a policy."""
    home = tmp_path / "home"
    home.mkdir()
    repo = tmp_path / "repo"
    (repo / ".flanner").mkdir(parents=True)
    return home, repo


def _write(where: Path, data: dict) -> None:
    where.parent.mkdir(parents=True, exist_ok=True)
    where.write_text(yaml.dump(data), encoding="utf-8")


def _global(home: Path, data: dict) -> None:
    _write(home / memory_policy.POLICY_FILENAME, {"version": 1, **data})


def _project(repo: Path, data: dict) -> None:
    _write(repo / ".flanner" / memory_policy.POLICY_FILENAME, {"version": 1, **data})


# --- defaults ----------------------------------------------------------------


def test_no_files_at_all_gives_the_shipped_defaults(places):
    home, repo = places

    policy = memory_policy.load(repo, home=home)

    assert policy.capture_mode == memory_policy.SUGGEST
    assert policy.allow_personal is False
    assert policy.allow_workspace is False
    assert policy.secrets == "reject"


def test_the_default_is_to_suggest_rather_than_to_capture(places):
    """Somebody who installs this and says nothing has not agreed to
    anything being written without them."""
    home, repo = places

    policy = memory_policy.load(repo, home=home)

    assert policy.suggests
    assert not policy.captures_automatically


def test_no_project_at_all_still_loads(places):
    """Personal memory has no repository to read a policy from."""
    home, _repo = places

    assert memory_policy.load(None, home=home).capture_mode == memory_policy.SUGGEST


# --- the merge ----------------------------------------------------------------


def test_a_global_file_sets_the_defaults_everywhere(places):
    home, repo = places
    _global(home, {"capture_mode": "auto_safe", "scope": {"allow_personal": True}})

    policy = memory_policy.load(repo, home=home)

    assert policy.capture_mode == "auto_safe"
    assert policy.allow_personal is True
    assert policy.provenance["capture_mode"] == "global"


def test_a_project_may_tighten(places):
    home, repo = places
    _global(home, {"capture_mode": "auto_safe"})
    _project(repo, {"capture_mode": "explicit"})

    policy = memory_policy.load(repo, home=home)

    assert policy.capture_mode == "explicit"
    assert policy.provenance["capture_mode"] == "project"


def test_a_project_may_not_loosen(places):
    """The rule this module exists for. A cloned repository must not be
    able to turn on automatic capture on somebody else's machine."""
    home, repo = places
    _global(home, {"capture_mode": "explicit"})
    _project(repo, {"capture_mode": "auto_safe"})

    policy = memory_policy.load(repo, home=home)

    assert policy.capture_mode == "explicit"
    assert policy.refused
    assert "auto_safe" in policy.refused[0]


def test_a_refusal_is_reported_rather_than_swallowed(places):
    """Ignoring a setting silently is how somebody spends an afternoon
    wondering why their file does nothing."""
    home, repo = places
    _global(home, {"scope": {"allow_personal": False}})
    _project(repo, {"scope": {"allow_personal": True}})

    policy = memory_policy.load(repo, home=home)

    assert policy.allow_personal is False
    assert any("allow_personal" in refusal for refusal in policy.refused)


def test_a_project_may_not_turn_on_sharing(places):
    home, repo = places
    _project(repo, {"scope": {"allow_workspace": True}})

    policy = memory_policy.load(repo, home=home)

    assert policy.allow_workspace is False
    assert policy.refused


def test_a_project_may_lower_a_quota_but_not_raise_one(places):
    home, repo = places
    _global(home, {"capture": {"max_auto_commits_per_day": 10}})
    _project(repo, {"capture": {"max_auto_commits_per_day": 3}})
    assert memory_policy.load(repo, home=home).max_auto_commits_per_day == 3

    _project(repo, {"capture": {"max_auto_commits_per_day": 99}})
    assert memory_policy.load(repo, home=home).max_auto_commits_per_day == 10


def test_a_project_may_narrow_the_categories_but_not_widen_them(places):
    home, repo = places
    _global(home, {"capture": {"allow_categories": ["decision", "constraint"]}})

    _project(repo, {"capture": {"allow_categories": ["decision"]}})
    assert memory_policy.load(repo, home=home).allow_categories == ("decision",)

    _project(repo, {"capture": {"allow_categories": ["decision", "constraint", "lesson"]}})
    policy = memory_policy.load(repo, home=home)
    assert policy.allow_categories == ("decision", "constraint")
    assert policy.refused


def test_a_project_may_set_a_preference_either_way(places):
    """Not everything is a permission. Which categories need approval and
    how long task context lives are choices a project owns."""
    home, repo = places
    _global(home, {"capture": {"require_approval": ["preference"]}})
    _project(repo, {"capture": {"require_approval": ["preference", "relationship", "fact"]}})

    policy = memory_policy.load(repo, home=home)

    assert set(policy.require_approval) == {"preference", "relationship", "fact"}
    assert not policy.refused


def test_secrets_cannot_be_anything_but_reject(places):
    """The one setting worth attacking. Storing a credential is never a
    policy choice, so it is not one the file can express."""
    home, repo = places
    _project(repo, {"sensitivity": {"secrets": "allow"}})

    with pytest.raises(ValidationError, match="never a policy choice"):
        memory_policy.load(repo, home=home)


# --- bad files ----------------------------------------------------------------


def test_a_file_that_is_not_yaml_says_so(places):
    home, repo = places
    (repo / ".flanner" / memory_policy.POLICY_FILENAME).write_text(
        "capture_mode: [unclosed", encoding="utf-8"
    )

    with pytest.raises(ValidationError, match="not valid YAML"):
        memory_policy.load(repo, home=home)


def test_an_unknown_capture_mode_is_refused(places):
    home, repo = places
    _project(repo, {"capture_mode": "always"})

    with pytest.raises(ValidationError, match="capture_mode must be one of"):
        memory_policy.load(repo, home=home)


def test_an_unknown_category_is_refused(places):
    """A rule naming a category that does not exist matches nothing, which
    is worse than an error because it looks like it is working."""
    home, repo = places
    _project(repo, {"capture": {"allow_categories": ["decision", "vibes"]}})

    with pytest.raises(ValidationError, match="unknown categor"):
        memory_policy.load(repo, home=home)


def test_a_wrong_type_is_refused(places):
    home, repo = places
    _project(repo, {"retrieval": {"max_results": "eight"}})

    with pytest.raises(ValidationError, match="whole number"):
        memory_policy.load(repo, home=home)


def test_a_future_version_is_refused_rather_than_guessed_at(places):
    home, repo = places
    _write(repo / ".flanner" / memory_policy.POLICY_FILENAME, {"version": 2})

    with pytest.raises(ValidationError, match="understands 1"):
        memory_policy.load(repo, home=home)


def test_an_empty_file_is_not_an_error(places):
    """A file somebody started and left blank should not break the tool."""
    home, repo = places
    (repo / ".flanner" / memory_policy.POLICY_FILENAME).write_text("", encoding="utf-8")

    assert memory_policy.load(repo, home=home).capture_mode == memory_policy.SUGGEST


def test_validate_reports_every_problem_at_once(places):
    """One error per run means finding three problems takes three runs."""
    problems = memory_policy.validate(
        {"capture_mode": "always", "capture": {"allow_categories": ["vibes"]}},
        where="project policy",
    )

    assert len(problems) == 2
    assert all("project policy" in problem for problem in problems)


# --- what a person is shown ---------------------------------------------------


def test_every_setting_says_which_file_decided_it(places):
    """Somebody who edits a file and sees no change needs to know which
    file is winning, not to be left guessing."""
    home, repo = places
    _global(home, {"capture_mode": "auto_safe"})
    _project(repo, {"retrieval": {"max_results": 3}})

    sources = {
        name: source
        for name, _value, source in memory_policy.explain(memory_policy.load(repo, home=home))
    }

    assert sources["capture_mode"] == "global"
    assert sources["max_results"] == "project"
    assert sources["task_context_days"] == "default"


def test_the_example_file_is_valid_and_matches_the_defaults(places):
    """It is what `policy init` writes, so a shipped file that the loader
    refuses would be an embarrassing first experience."""
    home, repo = places
    (repo / ".flanner" / memory_policy.POLICY_FILENAME).write_text(
        memory_policy.example(), encoding="utf-8"
    )

    policy = memory_policy.load(repo, home=home)

    assert policy.capture_mode == memory_policy.SUGGEST
    assert not policy.refused
    assert policy.allow_categories == memory_policy.CATEGORIES


def test_a_setting_in_the_wrong_section_is_named(places):
    """Found by writing this file: `max_results` lives under `retrieval`,
    and putting it under `capture` did nothing and said nothing. A setting
    that reads as configured and is ignored is the same failure as a rule
    naming a category that does not exist."""
    home, repo = places
    _project(repo, {"capture": {"max_results": 3}})

    with pytest.raises(ValidationError, match="capture.max_results is not a setting"):
        memory_policy.load(repo, home=home)


def test_a_misspelled_section_is_named(places):
    home, repo = places
    _project(repo, {"retreival": {"max_results": 3}})

    with pytest.raises(ValidationError, match="'retreival' is not a setting"):
        memory_policy.load(repo, home=home)
