"""The CI check (Curb PRD §10.12): agent steps in workflows, judged like agent launches.

The R6 criterion: fixtures modelled on PromptPwnd (E1), Clinejection (E2)
and Comment and Control (E6) are flagged. Plus each rule of `ci-r1`, the
safe fixes, the SARIF, and that no output names a secret.
"""

import json
import shutil
from pathlib import Path

import pytest
from click.testing import CliRunner

from flanner import curb_ci, curb_sarif
from flanner.cli import cli

DATA = Path(__file__).resolve().parent / "data" / "curb_ci"


def steps_in(root):
    steps, problems = curb_ci.check(root)
    assert problems == []
    return {Path(s.workflow).stem: s for s in steps}


def repo(tmp_path, text, name="agent.yml"):
    folder = tmp_path / ".github" / "workflows"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_text(text, encoding="utf-8")
    return tmp_path


def one(tmp_path, text):
    steps, problems = curb_ci.check(repo(tmp_path, text))
    assert problems == [] and len(steps) == 1
    return steps[0]


# --- the R6 fixtures ---------------------------------------------------------------------


def test_promptpwnd_clinejection_and_comment_and_control_rate_high():
    found = steps_in(DATA / "unsafe")
    assert {name: s.rule.id for name, s in found.items()} == {
        "promptpwnd-triage": "CI-H1",
        "clinejection-triage": "CI-H1",
        "comment-and-control": "CI-H1",
    }
    assert found["clinejection-triage"].who == "anyone"
    assert "event text in its prompt or environment" in found["promptpwnd-triage"].exposed_by
    assert found["comment-and-control"].unsafe and found["comment-and-control"].secrets == 1


def test_a_workflow_with_none_of_their_weaknesses_is_low():
    step = steps_in(DATA / "safe")["review"]
    assert step.rule.id == "CI-L1" and step.egress == "blocked" and not step.token


def test_no_output_names_a_secret():
    results = curb_ci.results(curb_ci.check(DATA / "unsafe")[0])
    text = json.dumps(curb_sarif.document(results, list(curb_ci.RULES.values()), version="x"))
    for name in ("NPM_TOKEN", "GEMINI_API_KEY", "ANTHROPIC_API_KEY", "GITHUB_TOKEN"):
        assert name not in text


# --- each rule ----------------------------------------------------------------------------

AGENT = """
      - uses: anthropics/claude-code-action@v1
        with:
          anthropic_api_key: ${{ secrets.ANTHROPIC_API_KEY }}
"""


def test_power_without_exposure_is_medium(tmp_path):
    step = one(
        tmp_path,
        "on: workflow_dispatch\njobs:\n  a:\n    runs-on: ubuntu-latest\n    steps:"
        + AGENT
        + "          claude_args: --dangerously-skip-permissions\n",
    )
    assert step.rule.id == "CI-M2" and not step.exposed


def test_exposure_with_shell_but_no_power_is_medium(tmp_path):
    step = one(
        tmp_path,
        "on: [issue_comment]\npermissions: read-all\njobs:\n  a:\n    runs-on: ubuntu-latest\n"
        "    steps:" + AGENT + '          claude_args: --allowedTools "Bash(npm test)"\n',
    )
    assert step.rule.id == "CI-M1" and step.shell and not step.power


def test_blocked_egress_takes_a_step_out_of_high(tmp_path):
    step = one(
        tmp_path,
        "on: issues\njobs:\n  a:\n    runs-on: ubuntu-latest\n    steps:\n"
        "      - uses: step-security/harden-runner@v2\n"
        "        with:\n          egress-policy: block\n"
        + AGENT.lstrip("\n")
        + "        env:\n          DEPLOY_KEY: ${{ secrets.DEPLOY_KEY }}\n",
    )
    assert step.exposed and step.power and step.rule.id == "CI-M2"


def test_event_text_in_a_prompt_exposes_a_step_on_any_trigger(tmp_path):
    step = one(
        tmp_path,
        "on: push\njobs:\n  a:\n    runs-on: ubuntu-latest\n    steps:\n"
        '      - run: codex exec "Explain ${{ github.event.head_commit.message }}"\n',
    )
    assert step.agent == "Codex" and step.exposed_by == ["event text in its prompt or environment"]
    assert step.who == "anyone who can start the workflow"


def test_agent_commands_are_found_as_well_as_actions(tmp_path):
    steps, _ = curb_ci.check(
        repo(
            tmp_path,
            "on: push\njobs:\n  a:\n    runs-on: ubuntu-latest\n    steps:\n"
            "      - run: npx @anthropic-ai/claude-code -p 'review'\n"
            "      - run: gemini --prompt 'summarise'\n"
            "      - run: echo claude is not run here\n",
        )
    )
    assert [s.agent for s in steps] == ["Claude Code", "Gemini CLI"]


def test_without_permissions_the_token_is_assumed_and_said_so(tmp_path):
    step = one(tmp_path, "on: push\njobs:\n  a:\n    runs-on: ubuntu-latest\n    steps:" + AGENT)
    assert step.write is None
    assert curb_ci.results([step])[0].properties["evidence"] == "assumed"


def test_an_unreadable_workflow_is_listed_and_the_rest_still_checked(tmp_path):
    root = repo(tmp_path, "on: push\njobs:\n  a:\n    runs-on: x\n    steps:" + AGENT)
    (root / ".github" / "workflows" / "broken.yml").write_text("jobs: [unclosed", encoding="utf-8")
    steps, problems = curb_ci.check(root)
    assert len(steps) == 1 and problems and "broken.yml" in problems[0]


# --- fixes ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("fixture", "gone", "said"),
    [
        (
            "clinejection-triage",
            "allowed_non_write_users",
            "only people with write access start it",
        ),
        ("comment-and-control", "--dangerously-skip-permissions", "Claude Code asks again"),
    ],
)
def test_the_safe_fixes_apply_and_the_file_still_parses(tmp_path, fixture, gone, said):
    shutil.copytree(DATA / "unsafe", tmp_path, dirs_exist_ok=True)
    path = tmp_path / ".github" / "workflows" / f"{fixture}.yml"
    assert said in curb_ci.fix(path)
    assert gone not in path.read_text(encoding="utf-8")
    assert curb_ci.fix(path) == []
    assert curb_ci.check(tmp_path)[1] == []


def test_codex_unsafe_modes_are_made_safe(tmp_path):
    root = repo(
        tmp_path,
        "on: issues\njobs:\n  a:\n    runs-on: x\n    steps:\n"
        "      - uses: openai/codex-action@v1\n        with:\n"
        "          safety-strategy: unsafe\n          sandbox: danger-full-access\n",
    )
    path = root / ".github" / "workflows" / "agent.yml"
    assert curb_ci.fix(path) == ["Codex drops sudo", "Codex keeps its sandbox"]
    step = curb_ci.check(root)[0][0]
    assert not step.unsafe


# --- the command ----------------------------------------------------------------------------


def test_the_command_writes_sarif_and_fails_at_the_threshold(tmp_path):
    sarif = tmp_path / "out.sarif"
    result = CliRunner().invoke(
        cli, ["curb", "ci", str(DATA / "unsafe"), "--sarif", str(sarif), "--fail-on", "high"]
    )
    assert result.exit_code == 1, result.output
    document = json.loads(sarif.read_text(encoding="utf-8"))
    assert document["version"] == "2.1.0"
    assert [r["level"] for r in document["runs"][0]["results"]] == ["error"] * 3
    rules = {r["id"] for r in document["runs"][0]["tool"]["driver"]["rules"]}
    assert rules == {"CI-H1", "CI-M1", "CI-M2", "CI-L1"}
    safe = CliRunner().invoke(cli, ["curb", "ci", str(DATA / "safe"), "--fail-on", "medium"])
    assert safe.exit_code == 0 and "Low" in safe.output


def test_the_command_prints_json_with_files_and_lines():
    result = CliRunner().invoke(cli, ["curb", "ci", str(DATA / "unsafe"), "--json"])
    rows = json.loads(result.output)
    assert {r["workflow"] for r in rows} == {
        ".github/workflows/clinejection-triage.yml",
        ".github/workflows/comment-and-control.yml",
        ".github/workflows/promptpwnd-triage.yml",
    }
    assert all(r["line"] > 1 and r["severity"] == "High" for r in rows)


def test_the_action_passes_inputs_through_the_environment():
    import yaml

    action = yaml.safe_load(
        (Path(__file__).resolve().parent.parent / "actions" / "curb-ci" / "action.yml").read_text(
            encoding="utf-8"
        )
    )
    for step in action["runs"]["steps"]:
        assert "${{" not in step.get("run", ""), step
