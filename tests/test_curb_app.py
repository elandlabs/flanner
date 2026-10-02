"""The app audit (Curb PRD §10.13): LLM calls found by pattern, with their shapes.

The R6 criterion: the fixture app's shapes are labelled correctly, as
tests/data/curb_app/expected.json lists them.
"""

import json
from pathlib import Path

from click.testing import CliRunner

from flanner import curb_app
from flanner.cli import cli

DATA = Path(__file__).resolve().parent / "data" / "curb_app"


def labelled(calls):
    return [
        {
            "path": c.path,
            "line": c.line,
            "shape": c.shape,
            "untrusted_input": c.untrusted_input,
            "unchecked_output": c.unchecked_output,
        }
        for c in calls
    ]


def test_the_fixture_apps_shapes_are_labelled_correctly():
    calls, problems = curb_app.audit(DATA / "app")
    assert problems == []
    assert labelled(calls) == json.loads((DATA / "expected.json").read_text(encoding="utf-8"))


def test_every_result_is_assumed_and_the_risky_ones_are_flagged():
    results = curb_app.results(curb_app.audit(DATA / "app")[0])
    assert {r.properties["evidence"] for r in results} == {"assumed"}
    flagged = {(r.rule.id, r.path) for r in results if r.rule.id != "APP-S1"}
    assert flagged == {("APP-O1", "runs_output.py"), ("APP-T1", "web_tools.py")}


def test_a_file_without_an_llm_library_is_not_read_as_one(tmp_path):
    (tmp_path / "plain.py").write_text("def run(x):\n    return x.invoke()\n", encoding="utf-8")
    assert curb_app.audit(tmp_path) == ([], [])


def test_an_unparsable_file_is_listed_and_vendored_code_skipped(tmp_path):
    (tmp_path / "broken.py").write_text("import openai\ndef (:\n", encoding="utf-8")
    vendored = tmp_path / ".venv" / "lib"
    vendored.mkdir(parents=True)
    (vendored / "client.py").write_text(
        "import openai\nopenai.OpenAI().chat.completions.create()\n", encoding="utf-8"
    )
    calls, problems = curb_app.audit(tmp_path)
    assert calls == [] and len(problems) == 1 and "broken.py" in problems[0]


def test_output_through_an_f_string_into_a_shell_is_followed(tmp_path):
    (tmp_path / "agent.py").write_text(
        "import os\nimport litellm\n\n\ndef go():\n"
        "    answer = litellm.completion(model='m', messages=[])\n"
        "    text = answer.choices[0].message.content\n"
        "    os.system(f'echo {text}')\n",
        encoding="utf-8",
    )
    (call,) = curb_app.audit(tmp_path)[0]
    assert call.unchecked_output == ["os.system"]


def test_the_command_prints_shapes_and_writes_sarif(tmp_path):
    sarif = tmp_path / "app.sarif"
    result = CliRunner().invoke(cli, ["curb", "app", str(DATA / "app"), "--sarif", str(sarif)])
    assert result.exit_code == 0, result.output
    assert "web_tools.py:11 tool-using" in result.output
    assert "model output reaches subprocess.run" in result.output
    document = json.loads(sarif.read_text(encoding="utf-8"))
    levels = {r["ruleId"]: r["level"] for r in document["runs"][0]["results"]}
    assert levels["APP-O1"] == "error" and levels["APP-T1"] == "warning"
    rows = json.loads(CliRunner().invoke(cli, ["curb", "app", str(DATA / "app"), "--json"]).output)
    assert len(rows) == 9


def test_a_method_name_counts_only_with_the_library_that_defines_it(tmp_path):
    (tmp_path / "test_cli.py").write_text(
        "import mcp\nfrom click.testing import CliRunner\n\n\n"
        "def test_it(cli):\n    CliRunner().invoke(cli, ['x'])\n",
        encoding="utf-8",
    )
    assert curb_app.audit(tmp_path) == ([], [])
