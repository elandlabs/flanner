"""Curb's path matching for permission rules and sandbox paths: tables, no files."""

from pathlib import Path, PurePosixPath

import pytest

from flanner import curb_match

CWD = Path("/work/repo")


# --- paths ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("C:\\Users\\Alice\\.aws", "/c/users/alice/.aws"),
        ("\\\\?\\C:\\Users\\a", "/c/users/a"),
        ("/home/alice/.ssh/", "/home/alice/.ssh"),
    ],
)
def test_paths_compare_in_posix_form(path, expected):
    assert curb_match.posix(path) == expected


HOME = PurePosixPath("/home/alice")
ANCHOR = PurePosixPath("/home/alice/.claude")


def _pattern(spec: str, *, deny: bool = True) -> str:
    return curb_match.claude_pattern(spec, anchor=ANCHOR, cwd=CWD, home=HOME, deny=deny)


@pytest.mark.parametrize(
    ("spec", "pattern"),
    [
        ("//etc/secrets/**", "/etc/secrets/**"),
        ("~/.aws/**", "/home/alice/.aws/**"),
        ("/secrets/**", "/home/alice/.claude/secrets/**"),
        ("./.env", "/work/repo/**/.env"),
        (".env", "/work/repo/**/.env"),
        ("secrets/**", "/work/repo/**/secrets/**"),
        ("src/app/**", "/work/repo/src/app/**"),
    ],
)
def test_claude_rule_anchors_follow_the_docs(spec, pattern):
    assert _pattern(spec) == pattern


def test_a_single_segment_directory_is_anchored_for_allow_rules():
    assert _pattern("secrets/**", deny=False) == "/work/repo/secrets/**"


def _denied(path: str, rules: list[tuple[str, str | None, str, PurePosixPath]]) -> str | None:
    return curb_match.claude_read_denied(Path(path), rules, cwd=CWD, home=HOME)


def test_a_bare_read_rule_covers_everything():
    assert _denied("/anything/at/all", [("Read", None, "user", ANCHOR)]) == "Read"


def test_a_rule_naming_a_directory_covers_what_is_inside():
    rules = [("Read", "~/.aws", "user", ANCHOR)]
    assert _denied("/home/alice/.aws/credentials", rules) == "Read(~/.aws)"
    assert _denied("/home/alice/.ssh/id_rsa", rules) is None


def test_a_bare_name_matches_at_any_depth_under_the_working_directory_only():
    rules = [("Read", ".env", "project", CWD)]
    assert _denied("/work/repo/services/api/.env", rules)
    assert _denied("/work/other/.env", rules) is None


def test_a_negation_carves_out_of_earlier_relative_rules_in_the_same_source():
    rules = [("Read", "*.env", "project", CWD), ("Read", "!sample.env", "project", CWD)]
    assert _denied("/work/repo/prod.env", rules)
    assert _denied("/work/repo/sample.env", rules) is None


def test_a_negation_cannot_reach_an_anchored_rule():
    rules = [
        ("Read", "~/notes/**", "user", ANCHOR),
        ("Read", "!~/notes/public/**", "user", ANCHOR),
    ]
    assert _denied("/home/alice/notes/public/a.md", rules)


def test_a_negation_from_another_source_does_not_cancel_a_deny():
    rules = [("Read", "./.env", "managed", CWD), ("Read", "!.env", "project", CWD)]
    assert _denied("/work/repo/.env", rules)


def test_windows_paths_match_rules_without_regard_to_case():
    rules = [("Read", "//c/Users/Alice/.aws/**", "user", ANCHOR)]
    assert curb_match.claude_read_denied(
        Path("C:\\users\\alice\\.aws\\credentials"), rules, cwd=CWD, home=HOME
    )


def test_only_read_rules_decide_reads():
    assert _denied("/home/alice/.aws/credentials", [("Edit", "~/.aws/**", "user", ANCHOR)]) is None


def _sandbox(path: str, deny: list[str], allow: list[str]) -> str | None:
    return curb_match.sandbox_read_denied(
        Path(path),
        deny=[(entry, CWD) for entry in deny],
        allow=[(entry, CWD) for entry in allow],
        home=HOME,
    )


def test_a_narrower_sandbox_allow_reopens_part_of_a_denied_region():
    assert _sandbox("/home/alice/projects/x.py", ["~/"], ["~/projects"]) is None
    assert _sandbox("/home/alice/.aws/credentials", ["~/"], ["~/projects"]) == "~/"


def test_a_sandbox_deny_holds_inside_a_wider_allow():
    assert _sandbox("/home/alice/.env", ["~/.env"], ["~/"]) == "~/.env"
    assert _sandbox("/home/alice/a/b/.env", ["~/**/.env"], ["~/"]) == "~/**/.env"


def test_sandbox_paths_use_ordinary_conventions():
    assert curb_match.sandbox_pattern("/tmp/build", anchor=CWD, home=HOME) == "/tmp/build"
    assert curb_match.sandbox_pattern(".", anchor=CWD, home=HOME) == "/work/repo"
