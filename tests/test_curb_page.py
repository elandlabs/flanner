"""The words and rows the web UI's Curb pages are built from (§11.3)."""

import time

from flanner import curb_alerts, curb_ci, curb_page


def test_terminal_phrasing_becomes_a_sentence():
    assert curb_page.sentence("8 credential source(s) have no Read deny rule") == (
        "8 credential sources have no Read deny rule."
    )
    assert curb_page.sentence("1 MCP server(s); run `gh auth token`") == (
        "1 MCP server; run gh auth token."
    )
    assert curb_page.sentence("Add rules for the files (shown in the window)") == (
        "Add rules for the files."
    )
    assert curb_page.sentence("") == ""


def test_times_are_said_as_a_person_would():
    now = time.time()
    assert curb_page.when(None) == "never"
    assert curb_page.when(now - 5, now=now) == "just now"
    assert curb_page.when(now - 120, now=now) == "2 minutes ago"
    assert curb_page.when(now - 3 * 3600, now=now) == "3 hours ago"
    assert curb_page.when(now - 86400, now=now) == "1 day ago"
    assert curb_page.when(now - 30 * 86400, now=now).startswith("on ")


def test_search_takes_every_word_and_only_what_a_row_shows():
    rows = [{"find": "github token claude code transcript"}, {"find": "aws key codex session"}]
    assert curb_page.search(rows, "claude token") == rows[:1]
    assert curb_page.search(rows, "") == rows
    assert curb_page.search(rows, "quokka") == []


def test_a_revision_is_a_commit_or_a_range_and_never_an_option():
    for good in ("HEAD", "main..HEAD", "v0.16.0...feat/curb-r1", "HEAD~3", "a1b2c3d"):
        assert curb_page.REVISION.fullmatch(good), good
    for bad in ("--output=x", "-n1", "main..HEAD;rm", "a b", ""):
        assert not curb_page.REVISION.fullmatch(bad), bad


def test_an_alert_row_says_what_changed_and_where_to_act():
    found = {"type": curb_alerts.SANDBOX_OFF, "agent": "codex", "digest": "", "severity": "high"}
    curb_alerts.raise_alerts([{**found, "location": "Codex sandbox"}], device_id="d")
    rows = curb_page.alerts(curb_alerts.history())
    assert rows[0]["what"] == "The sandbox was turned off." and rows[0]["who"] == "Codex"
    assert rows[0]["part"] == "machine/fixes" and rows[0]["told"].startswith("You. ")


def test_a_safe_ci_fix_can_be_named_without_being_made(tmp_path):
    workflow = tmp_path / "triage.yml"
    text = "steps:\n  - run: claude --dangerously-skip-permissions -p hi\n"
    workflow.write_text(text, encoding="utf-8")
    assert curb_ci.fix(workflow, write=False) == ["Claude Code asks again"]
    assert workflow.read_text(encoding="utf-8") == text
    assert curb_ci.fix(workflow) == ["Claude Code asks again"]
    assert "--dangerously-skip-permissions" not in workflow.read_text(encoding="utf-8")
