"""The web UI's Curb section (Curb PRD §11.3).

Every page is redacted as the terminal is. A browser sees names and
locations only while it holds a reveal the operating system approved, and
nothing changes from a page without that same approval.
"""

import json
import re
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from flanner import (
    curb_alerts,
    curb_approval,
    curb_attribution,
    curb_fix,
    curb_kingfisher,
    curb_live,
    curb_policy,
    curb_reveal,
    curb_store,
    curb_tester,
)
from flanner.database import create_project, get_session
from flanner.web import app
from tests.test_curb_policy import ISSUER_RING, ORG, accept_authority, policy, receive

LOCAL_URL = "http://127.0.0.1:8080"
SECRET = "sk-test-0123456789abcdefQUOKKA"  # noqa: S105 - a planted fake
PAGES = (
    "/curb",
    "/curb/machine/agents",
    "/curb/machine/leaks",
    "/curb/machine/fixes",
    "/curb/machine/tests",
    "/curb/machine/activity",
    "/curb/projects/reach",
    "/curb/projects/ci",
    "/curb/projects/apps",
    "/curb/projects/commits",
    "/curb/team/policy",
    "/curb/team/devices",
    "/curb/team/alerts",
)


class Yes:
    """A prompt that says yes, and keeps what it was asked."""

    name, weak, shows_reason = "test prompt", False, True

    def __init__(self):
        self.asked = []

    def available(self):
        return True

    def confirm(self, reason):
        self.asked.append(reason)
        return True


class No(Yes):
    def confirm(self, reason):
        self.asked.append(reason)
        return False


class Silent(Yes):
    """A prompt that cannot show its reason, as a Linux desktop's cannot."""

    shows_reason = False


@pytest.fixture
def page(db, tmp_path, monkeypatch):
    """One agent, one credential and one project, on a machine with no approval method yet."""
    curb_live.reset()
    monkeypatch.setattr(curb_reveal, "REVEALS", curb_reveal.Reveals())
    claude = tmp_path / "claude-config"
    claude.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "no-codex"))
    monkeypatch.setattr("flanner.curb_inventory.shutil.which", lambda name: None)
    monkeypatch.setattr("flanner.curb_report.shutil.which", lambda name: None)
    monkeypatch.setattr("flanner.curb_inventory.run", lambda argv: None)
    monkeypatch.setattr(curb_approval, "process_chain", lambda: [])
    monkeypatch.setattr(curb_approval, "method", lambda: None)
    monkeypatch.setattr(curb_kingfisher, "unavailable", lambda: "Kingfisher is not installed")
    aws = Path.home() / ".aws" / "credentials"
    aws.parent.mkdir(parents=True, exist_ok=True)
    aws.write_text("[quokka-prod]\naws_access_key_id = x\n", encoding="utf-8")
    project = tmp_path / "shop-api"
    project.mkdir()
    monkeypatch.chdir(project)
    create_project(get_session(), "shop-api", project_root=str(project))
    yield TestClient(app, base_url=LOCAL_URL), claude, project
    curb_live.reset()


def ready(client):
    """Open the overview and wait for the first check of this machine."""
    client.get("/curb")
    curb_live.MEMO.settle()


def approve(monkeypatch, prompt=None):
    prompt = prompt or Yes()
    monkeypatch.setattr(curb_approval, "method", lambda: prompt)
    curb_live.changed()
    return prompt


def reveal(client, monkeypatch, prompt=None):
    """Ask for names as a person does: read the code, then say yes to the prompt."""
    prompt = approve(monkeypatch, prompt)
    box = client.get("/curb?review=reveal").text
    code = re.search(r'class="match-code tnum">(\d{4})<', box)
    landed = client.post("/curb/reveal", data={"back": "/curb"}).text
    return prompt, code.group(1) if code else "", landed


def project_id(client):
    return re.search(r'option value="([0-9a-f-]{36})"', client.get("/curb/projects/reach").text)[1]


# --- reading ----------------------------------------------------------------------------


def test_the_first_visit_says_it_is_checking_then_shows_each_agent(page):
    client, _, _ = page
    first = client.get("/curb")
    assert first.status_code == 200
    assert "Checking what your agents can reach" in first.text
    curb_live.MEMO.settle()
    done = client.get("/curb").text
    assert "Claude Code" in done and "High risk" in done
    assert "can read your credentials" in done


def test_every_page_renders_and_names_nothing_without_a_reveal(page):
    client, _, _ = page
    ready(client)
    for address in PAGES:
        response = client.get(address)
        assert response.status_code == 200, address
        assert "quokka" not in response.text.lower(), address
        assert ".aws" not in response.text, address
        assert "no-store" not in response.headers.get("cache-control", ""), address
        assert response.headers["x-frame-options"] == "DENY", address


def test_each_part_has_at_most_one_main_button(page):
    client, _, _ = page
    ready(client)
    for address in PAGES:
        assert client.get(address).text.count("btn btn-primary") <= 1, address


def test_one_project_shows_its_reach_and_hides_the_credential(page):
    client, _, _ = page
    ready(client)
    address = f"/curb/projects/reach?project={project_id(client)}"
    client.get(address)
    curb_live.MEMO.settle()
    text = client.get(address).text
    assert "Claude Code in shop-api" in text
    assert "Credentials in reach" in text and "hidden" in text
    assert "quokka" not in text.lower()


def test_a_message_cannot_be_put_in_curbs_mouth_by_a_link(page):
    client, _, _ = page
    ready(client)
    text = client.get("/curb/machine/fixes?success=Pwned&error=Pwned&said=Pwned").text
    assert "Pwned" not in text


# --- names and locations ----------------------------------------------------------------


def test_names_show_after_the_systems_yes_to_the_pages_code(page, monkeypatch):
    client, _, _ = page
    ready(client)
    prompt, code, _ = reveal(client, monkeypatch)
    assert code and f"Code {code}" in prompt.asked[0]
    address = f"/curb/projects/reach?project={project_id(client)}"
    client.get(address)
    curb_live.MEMO.settle()
    shown = client.get(address)
    assert "quokka-prod" in shown.text
    assert shown.headers["cache-control"] == "no-store"
    assert "Hide names" in shown.text


def test_another_browser_sees_no_names(page, monkeypatch):
    client, _, _ = page
    ready(client)
    reveal(client, monkeypatch)
    address = f"/curb/projects/reach?project={project_id(client)}"
    client.get(address)
    curb_live.MEMO.settle()
    assert "quokka-prod" in client.get(address).text
    other = TestClient(app, base_url=LOCAL_URL)
    assert "quokka" not in other.get(address).text.lower()


def test_hiding_ends_the_reveal_at_once(page, monkeypatch):
    client, _, _ = page
    ready(client)
    reveal(client, monkeypatch)
    address = f"/curb/projects/reach?project={project_id(client)}"
    client.get(address)
    curb_live.MEMO.settle()
    client.post("/curb/hide", data={"back": address})
    assert "quokka" not in client.get(address).text.lower()


def test_a_refused_prompt_keeps_names_hidden(page, monkeypatch):
    client, _, _ = page
    ready(client)
    _, _, landed = reveal(client, monkeypatch, No())
    assert "Names stay hidden: not approved" in landed and "Hide names" not in landed


def test_a_prompt_that_cannot_show_the_code_is_not_used(page, monkeypatch):
    client, _, _ = page
    ready(client)
    prompt = approve(monkeypatch, Silent())
    box = client.get("/curb?review=reveal").text
    assert "Names stay hidden on this machine" in box and "flanner curb show" in box
    client.post("/curb/reveal", data={"back": "/curb"})
    assert prompt.asked == []
    assert "Hide names" not in client.get("/curb").text


def test_a_reveal_only_sends_the_browser_back_into_curb(page, monkeypatch):
    client, _, _ = page
    ready(client)
    approve(monkeypatch)
    client.get("/curb?review=reveal")
    response = client.post(
        "/curb/reveal", data={"back": "https://evil.example/"}, follow_redirects=False
    )
    assert response.headers["location"] == "/curb"


# --- changes ----------------------------------------------------------------------------


def test_a_fix_needs_the_operating_systems_yes(page, monkeypatch):
    client, claude, _ = page
    ready(client)
    assert "Curb is read-only on this machine" in client.get("/curb/machine/fixes").text
    assert "no way to ask you" in client.post("/curb/machine/fixes/apply").text
    assert not (claude / "settings.json").exists()

    approve(monkeypatch, No())
    refused = client.post("/curb/machine/fixes/apply").text
    assert "Not approved, so nothing changed" in refused
    assert not (claude / "settings.json").exists()

    prompt = approve(monkeypatch)
    review = client.get("/curb/machine/fixes?review=fixes").text
    assert 'action="/curb/machine/fixes/apply"' in review and "Ask " in review
    landed = client.post("/curb/machine/fixes/apply").text
    deny = json.loads((claude / "settings.json").read_text(encoding="utf-8"))["permissions"][
        "deny"
    ]
    assert any(".aws" in rule for rule in deny)
    assert "Read deny rule" in prompt.asked[0]
    assert "Each file is backed up for 7 days" in landed
    assert "Nothing needs" not in landed and "backup=" in landed


def test_a_plan_the_settings_have_left_behind_is_not_written(page, monkeypatch):
    client, claude, _ = page
    ready(client)
    prompt = approve(monkeypatch)
    mine = '{"model": "opus"}'
    (claude / "settings.json").write_text(mine, encoding="utf-8")
    landed = client.post("/curb/machine/fixes/apply").text
    assert (claude / "settings.json").read_text(encoding="utf-8") == mine
    assert prompt.asked == []
    assert "not as Curb last read them" in landed


def test_an_applied_fix_can_be_undone_from_the_page(page, monkeypatch):
    client, claude, _ = page
    ready(client)
    approve(monkeypatch)
    client.post("/curb/machine/fixes/apply")
    assert (claude / "settings.json").exists()
    backup = curb_fix.latest().name
    assert f"backup={backup}" in client.get("/curb/machine/fixes").text
    approve(monkeypatch, No())
    client.post("/curb/machine/fixes/undo", data={"backup": backup})
    assert (claude / "settings.json").exists()
    approve(monkeypatch)
    client.post("/curb/machine/fixes/undo", data={"backup": backup})
    assert not (claude / "settings.json").exists()
    landed = client.post("/curb/machine/fixes/undo", data={"backup": "../../elsewhere"}).text
    assert "That backup is gone" in landed


def test_another_site_cannot_press_a_button(page, monkeypatch):
    client, claude, _ = page
    ready(client)
    approve(monkeypatch)
    response = client.post("/curb/machine/fixes/apply", headers={"Origin": "https://evil.example"})
    assert response.status_code == 403
    assert not (claude / "settings.json").exists()


def test_only_one_approval_is_asked_for_at_a_time(page, monkeypatch):
    client, claude, _ = page
    ready(client)
    prompt = approve(monkeypatch)
    with curb_live.APPROVAL:
        landed = client.post("/curb/machine/fixes/apply").text
    assert prompt.asked == [] and not (claude / "settings.json").exists()
    assert "Another approval is already waiting" in landed


def test_logging_turns_on_after_a_yes_and_the_log_can_be_checked(page, monkeypatch):
    client, claude, _ = page
    ready(client)
    assert "Turn on…" in client.get("/curb/machine/activity").text
    approve(monkeypatch, No())
    client.post("/curb/machine/activity/logging", data={"agent": "claude", "on": "1"})
    assert not (claude / "settings.json").exists()
    approve(monkeypatch)
    client.post("/curb/machine/activity/logging", data={"agent": "claude", "on": "1"})
    hooks = json.loads((claude / "settings.json").read_text(encoding="utf-8"))["hooks"]
    assert "PreToolUse" in hooks
    text = client.get("/curb/machine/activity?verify=1").text
    assert "The log is intact" in text and "Turn off…" in text


def test_the_tests_run_after_a_yes_and_show_what_the_agent_tried(page, monkeypatch):
    client, claude, _ = page
    rule = {"permissions": {"deny": ["Read(~/.aws/**)"]}}
    (claude / "settings.json").write_text(json.dumps(rule), encoding="utf-8")
    ready(client)
    before = client.get("/curb/machine/tests").text
    assert "AWS credentials file kept out of reach" in before and "Not tested" in before

    def tried(context, target, *, key, runner=None):
        outcomes = {
            curb_tester.READ_TOOL: curb_tester.BLOCKED,
            curb_tester.CAT: curb_tester.ALLOWED,
        }
        return curb_tester.Result(target, outcomes)

    monkeypatch.setattr(curb_tester, "test_target", tried)
    client.post("/curb/machine/tests/run")
    assert curb_live.TESTS.state == "idle"

    prompt = approve(monkeypatch)
    client.post("/curb/machine/tests/run")
    curb_live.TESTS.settle()
    assert "Run 1 test session(s) and plant 1 decoy(s)" in prompt.asked[0]
    after = client.get("/curb/machine/tests").text
    assert "Not proved" in after and "With cat: got through" in after
    assert "With Read tool: blocked" in after


def test_signing_is_set_up_and_a_key_replaced_after_a_yes(page, monkeypatch):
    client, claude, _ = page
    ready(client)
    assert "Set up signing for Claude Code" in client.get("/curb/projects/commits").text
    approve(monkeypatch, No())
    refused = client.post("/curb/projects/commits/signing").text
    assert not (claude / "settings.json").exists()
    # The key was made before the prompt. Signing is still on offer, because nothing uses it.
    assert "Set up signing for Claude Code" in refused

    prompt = approve(monkeypatch)
    done = client.post("/curb/projects/commits/signing").text
    env = json.loads((claude / "settings.json").read_text(encoding="utf-8"))["env"]
    assert env["GIT_CONFIG_COUNT"] and "sign commits" in prompt.asked[0]
    first = curb_attribution.keys()["keys"]["claude"]["fingerprint"]
    assert first in done and "Set up signing" not in done

    client.post("/curb/projects/commits/signing", data={"replace": "claude"})
    assert curb_attribution.keys()["keys"]["claude"]["fingerprint"] != first
    assert "Replace the Claude Code signing key" in prompt.asked[-1]


def test_forgetting_deletes_what_curb_stored_after_a_yes(page, monkeypatch):
    client, _, _ = page
    ready(client)
    curb_store.save_report("sweep", {"by_class": {"A": 1, "B": 0, "C": 0}, "findings": []})
    client.post("/curb/forget")
    assert curb_store.latest_report("sweep") is not None
    approve(monkeypatch)
    client.post("/curb/forget")
    assert curb_store.latest_report("sweep") is None


# --- leaks ------------------------------------------------------------------------------


def test_a_stored_scan_shows_counts_and_kinds_and_no_place(page):
    client, _, _ = page
    curb_store.save_report(
        "sweep",
        {
            "secrets": 2,
            "by_class": {"A": 2, "B": 0, "C": 0},
            "files_scanned": 40,
            "launches": ["Claude Code"],
            "not_checked": [],
            "findings": [
                {
                    "class": "A",
                    "category": "Claude Code transcript",
                    "rule": "kingfisher.github.2",
                },
                {"class": "A", "category": "Codex session", "rule": "kingfisher.aws.1"},
            ],
        },
    )
    text = client.get("/curb/machine/leaks").text
    assert "Rule kingfisher.github.2" in text and "Rule kingfisher.aws.1" in text
    assert "Scanned 40 files" in text and "Rotate now" in text
    assert "Scan again to see them" in text and "Remove from file" not in text


def test_the_scan_needs_its_scanner_and_says_how_to_get_it(page):
    client, _, _ = page
    text = client.get("/curb/machine/leaks").text
    assert "The leak scan cannot run" in text and "flanner[sweep]" in text
    client.post("/curb/machine/leaks/scan")
    assert not curb_live.SCAN.running and curb_live.SCAN.result is None


@pytest.fixture
def scanner(page, monkeypatch):
    """A scanner that finds the planted fake secret, in the project's .env file."""
    _, _, project = page
    (project / ".env").write_text(f"API_KEY={SECRET}\nDEBUG=1\n", encoding="utf-8")

    class Detector:
        def __call__(self, path):
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            return [
                curb_kingfisher.Match("fake.1", "Fake API key", number, SECRET)
                for number, line in enumerate(lines, start=1)
                if SECRET in line
            ]

    monkeypatch.setattr(curb_kingfisher, "unavailable", lambda: None)
    monkeypatch.setattr(curb_kingfisher, "Detector", Detector)
    return page


def test_a_scan_lists_each_secret_and_never_its_value(scanner, monkeypatch):
    client, _, project = scanner
    ready(client)
    client.post("/curb/machine/leaks/scan")
    curb_live.SCAN.settle()
    hidden = client.get("/curb/machine/leaks").text
    assert "Fake API key" in hidden and "project .env" in hidden
    assert SECRET not in hidden and str(project) not in hidden and ".env, line" not in hidden

    reveal(client, monkeypatch)
    shown = client.get("/curb/machine/leaks").text
    assert ".env, line 1" in shown and "shop-api" in shown and "Remove from file" in shown
    assert SECRET not in shown
    assert SECRET not in json.dumps(curb_store.latest_report("sweep"))


def test_removing_a_secret_needs_a_reveal_and_a_yes(scanner, monkeypatch):
    client, _, project = scanner
    ready(client)
    client.post("/curb/machine/leaks/scan")
    curb_live.SCAN.settle()
    file = curb_live.SCAN.result.leaks[0].file
    approve(monkeypatch)
    landed = client.post("/curb/machine/leaks/remove", data={"file": file}).text
    assert SECRET in (project / ".env").read_text(encoding="utf-8")
    assert "Show names and locations first" in landed

    prompt, _, _ = reveal(client, monkeypatch)
    review = client.get(f"/curb/machine/leaks?review=remove&file={file}").text
    assert "Remove 1 secret from this file?" in review and "no undo" in review
    client.post("/curb/machine/leaks/remove", data={"file": file})
    after = (project / ".env").read_text(encoding="utf-8")
    assert SECRET not in after and "CURB-SCRUBBED" in after and "DEBUG=1" in after
    assert "with no backup" in prompt.asked[-1]


# --- projects ---------------------------------------------------------------------------

WORKFLOW = """\
name: triage
on:
  issues:
    types: [opened]
jobs:
  triage:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Triage
        uses: anthropics/claude-code-action@v1
        with:
          prompt: "Triage ${{ github.event.issue.title }}"
          claude_args: "--dangerously-skip-permissions"
"""


def test_ci_findings_are_listed_exported_and_fixed_after_a_yes(page, monkeypatch):
    client, _, project = page
    workflow = project / ".github" / "workflows" / "triage.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text(WORKFLOW, encoding="utf-8")
    ready(client)
    one = project_id(client)
    text = client.get(f"/curb/projects/ci?project={one}").text
    assert ".github/workflows/triage.yml" in text and "Apply 1 safe fix" in text

    export = client.get(f"/curb/projects/ci/export?project={one}")
    assert export.headers["content-disposition"] == 'attachment; filename="curb-ci.sarif"'
    assert export.json()["runs"][0]["results"]

    client.post("/curb/projects/ci/fix", data={"project": one})
    assert "--dangerously-skip-permissions" in workflow.read_text(encoding="utf-8")
    approve(monkeypatch)
    client.post("/curb/projects/ci/fix", data={"project": one})
    assert "--dangerously-skip-permissions" not in workflow.read_text(encoding="utf-8")


def test_app_calls_are_listed_and_exported(page):
    client, _, project = page
    (project / "answer.py").write_text(
        "import anthropic\n\n\ndef answer(q):\n"
        "    client = anthropic.Anthropic()\n"
        "    return client.messages.create(model='x', messages=[q])\n",
        encoding="utf-8",
    )
    ready(client)
    one = project_id(client)
    text = client.get(f"/curb/projects/apps?project={one}").text
    assert "answer.py:6" in text and "anthropic" in text and "One call" in text
    export = client.get(f"/curb/projects/apps/export?project={one}")
    assert export.json()["runs"][0]["results"]
    assert client.get("/curb/projects/apps/export?project=nope").status_code == 404


def test_commits_refuse_a_range_that_is_not_one(page):
    client, _, _ = page
    ready(client)
    for bad in ("--output=x", "main..HEAD; rm", "a b"):
        text = client.get("/curb/projects/commits", params={"revision": bad}).text
        assert "Enter a commit or a range" in text, bad
    assert "git could not read that" in client.get("/curb/projects/commits").text


# --- team -------------------------------------------------------------------------------


def test_team_pages_say_how_to_join_until_this_machine_has(page):
    client, _, _ = page
    for address in ("/curb/team/policy", "/curb/team/devices", "/curb/team/alerts"):
        text = client.get(address).text
        assert "Join a team" in text, address
    assert client.get("/curb/team", follow_redirects=False).headers["location"] == (
        "/curb/team/policy"
    )


@pytest.fixture
def joined(page, monkeypatch):
    from flanner import session

    held = session.Session(
        endpoint="https://example.test",
        device_id="device-one",
        organization_id=ORG,
        user_id="user-one",
        entitlement="",
        keyring=dict(ISSUER_RING),
    )
    monkeypatch.setattr(session, "load", lambda: held)
    return page


def test_a_joined_device_shows_policy_devices_and_alerts(joined):
    client, _, _ = joined
    text = client.get("/curb/team/policy").text
    assert "No policy has arrived yet" in text and "flanner curb policy --check-in" in text

    assert "No device reports on this machine yet" in client.get("/curb/team/devices").text
    curb_store.write_state(
        "fleet-view",
        {
            "fetched_at": time.time(),
            "rows": [
                {
                    "device": "build-mac-01",
                    "device_id": "device-two",
                    "verified": False,
                    "problems": ["report 4 is replayed or out of order"],
                    "stale": True,
                    "last_report": "2026-10-01T09:00:00Z",
                    "agents": [{"agent": "codex", "version": "0.154.0"}],
                    "policy": {"applied": 7, "drift": True},
                    "severity": {"high": 1, "medium": 0, "low": 0},
                    "exposure": {"A": 3, "B": 0, "C": 0},
                }
            ],
        },
    )
    devices = client.get("/curb/team/devices").text
    assert "build-mac-01" in devices and "No longer meets version 7" in devices
    assert "Not verified" in devices and "Report 4 is replayed" in devices

    assert "No alerts yet" in client.get("/curb/team/alerts").text
    curb_alerts.raise_alerts(
        [
            {
                "type": curb_alerts.MCP_ADDED,
                "agent": "claude",
                "digest": "abc",
                "severity": "medium",
                "location": "Claude Code MCP settings",
            }
        ],
        device_id="device-one",
    )
    alerts = client.get("/curb/team/alerts").text
    assert "An MCP server was added" in alerts and "See agents" in alerts


def test_the_delegation_needs_a_yes_to_turn_on_and_none_to_turn_off(joined, monkeypatch):
    client, claude, _ = joined
    ready(client)
    client.post("/curb/team/policy/delegation", data={"on": "1"})
    assert curb_policy.load().delegated_at is None

    prompt = approve(monkeypatch)
    client.post("/curb/team/policy/delegation", data={"on": "1"})
    assert curb_policy.load().delegated_at is not None and len(prompt.asked) == 1
    hooks = json.loads((claude / "settings.json").read_text(encoding="utf-8"))["hooks"]
    assert "SessionStart" in hooks

    client.post("/curb/team/policy/delegation", data={"on": ""})
    assert curb_policy.load().delegated_at is None and len(prompt.asked) == 1


def test_a_waiting_policy_is_shown_and_applied_after_a_yes(joined, monkeypatch):
    client, claude, _ = joined
    ready(client)
    accept_authority()
    receive(policy(1))
    held = curb_live.MEMO.get("machine", lambda: None, max_age=9999).value
    curb_policy.apply_received(held.defaults, home=Path.home(), platform=sys.platform, env={})

    waiting = client.get("/curb/team/policy").text
    assert "Version 1 waits for your yes" in waiting and "Review version 1" in waiting
    assert "~/.aws" not in waiting and "1 location" in waiting
    assert "Approve policy version 1" in client.get("/curb").text

    client.post("/curb/team/policy/approve")
    assert curb_policy.load().pending is not None

    approve(monkeypatch)
    landed = client.post("/curb/team/policy/approve").text
    assert "Applied policy version 1" in landed
    assert curb_policy.load().applied["by"] == "person" and not curb_policy.load().pending
    deny = json.loads((claude / "settings.json").read_text(encoding="utf-8"))["permissions"][
        "deny"
    ]
    assert "Read(~/.aws)" in deny
