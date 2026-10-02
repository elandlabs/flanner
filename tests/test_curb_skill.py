"""The agent-blast-radius skill (Curb PRD §10.14).

The R6 criterion: in a full skill run, planted secrets' locations and
values never reach the agent, and `flanner curb show` gives the agent only
a notice. The agent's transcript is stood in for by everything the skill's
commands print.
"""

import re

from click.testing import CliRunner

from flanner import agent_hooks, curb_window
from flanner.cli import cli
from tests.test_curb_sweep import box, swept  # noqa: F401 - fixtures

SKILL = agent_hooks.SKILLS[agent_hooks.CURB_SKILL_NAME]


def commands():
    """The commands the skill tells an agent to run, from its numbered list."""
    return re.findall(r"^\d+\. `flanner (curb [^`]+)`", SKILL, re.M)


def test_the_skill_is_installed_for_both_agents(tmp_path):
    agent_hooks.install_skill(str(tmp_path))
    for folder in agent_hooks.SKILL_DIRS:
        path = tmp_path / folder / "agent-blast-radius" / "SKILL.md"
        assert path.read_text(encoding="utf-8") == SKILL


def test_the_skill_runs_curbs_read_commands_and_none_that_write():
    assert commands() == ["curb map", "curb inventory", "curb sweep", "curb observed"]
    assert "flanner curb show" in SKILL


def test_a_full_skill_run_shows_the_agent_no_location_and_no_value(swept, monkeypatch):  # noqa: F811
    started = []
    monkeypatch.setattr(curb_window, "unavailable", lambda: None)
    monkeypatch.setattr(curb_window, "launch", lambda args: started.append(args))
    transcript = []
    for command in [*commands(), "curb show", "curb show --sweep"]:
        result = CliRunner().invoke(cli, command.split())
        transcript.append(result.output)
    seen = "\n".join(transcript)
    assert swept.secret not in seen
    for location in (swept.env_file, swept.claude / "projects", swept.home / ".aws"):
        assert str(location) not in seen
    assert len(started) == 2  # each show opened a window instead of printing
    assert "opened" in transcript[-1].lower() or "window" in transcript[-1].lower()
