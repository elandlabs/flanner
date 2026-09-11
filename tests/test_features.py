"""The integrations flag, and what it actually covers.

Linear and Jira are built and not supported. The rule is that nothing
offers them while the flag is off: not the sidebar, not `--help`, not the
MCP tool list, not the plan viewer. The code underneath stays, and the
rest of the suite still exercises it, which is the difference between
switching a feature off and letting it rot.
"""

import importlib
import sys

import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient

from flanner import features
from flanner.web import app

LOCAL_URL = "http://127.0.0.1:8080"


@pytest.fixture
def client(db):
    return TestClient(app, base_url=LOCAL_URL, follow_redirects=False)


@pytest.fixture
def off(monkeypatch):
    monkeypatch.delenv(features.INTEGRATIONS_ENV, raising=False)


@pytest.fixture
def on(monkeypatch):
    monkeypatch.setenv(features.INTEGRATIONS_ENV, "1")


def test_off_is_the_default(off):
    assert features.integrations_enabled() is False


@pytest.mark.parametrize(
    "value,expected",
    [
        ("1", True),
        ("true", True),
        ("TRUE", True),
        ("yes", True),
        ("on", True),
        ("0", False),
        ("false", False),
        ("", False),
        ("maybe", False),
    ],
)
def test_the_switch_reads_the_obvious_spellings(monkeypatch, value, expected):
    monkeypatch.setenv(features.INTEGRATIONS_ENV, value)
    assert features.integrations_enabled() is expected


def test_the_sidebar_does_not_offer_integrations(client, off):
    body = client.get("/").text
    assert 'href="/integrations"' not in body


def test_the_sidebar_offers_them_again_when_switched_on(client, on):
    body = client.get("/").text
    assert 'href="/integrations"' in body


def test_the_page_still_answers_and_says_why(client, off):
    """A bookmarked URL that starts 404ing reads as a broken build."""
    page = client.get("/integrations")
    assert page.status_code == 200
    assert "Not yet" in page.text
    assert "empty-glyph" in page.text


def test_the_page_is_ordinary_again_when_switched_on(client, on):
    page = client.get("/integrations")
    assert page.status_code == 200
    assert "Not yet" not in page.text


def test_help_does_not_list_the_groups(off):
    from flanner.cli import cli

    result = CliRunner().invoke(cli, ["--help"])
    assert result.exit_code == 0
    assert "jira" not in result.output.lower()
    assert "linear" not in result.output.lower()


def test_a_command_refuses_and_names_the_switch(off):
    from flanner.cli import cli

    result = CliRunner().invoke(cli, ["jira", "links", "anything"])
    assert result.exit_code == 2
    assert features.INTEGRATIONS_ENV in result.output


#: The tool list is built when `flanner.server` is imported, and a module
#: imports once per process. Asking about it in-process would be asking
#: whichever state happened to be live at the first import, so these two
#: cases each get a process of their own.
_LIST_TOOLS = (
    "import asyncio, json;"
    " from flanner import server;"
    " names = [t.name for t in asyncio.run(server._mcp.list_tools())];"
    " print(json.dumps(names))"
)


def _tool_names(enabled: bool) -> list[str]:
    import json
    import os
    import subprocess

    env = dict(os.environ)
    if enabled:
        env[features.INTEGRATIONS_ENV] = "1"
    else:
        env.pop(features.INTEGRATIONS_ENV, None)
    out = subprocess.run(
        [sys.executable, "-c", _LIST_TOOLS],
        capture_output=True,
        text=True,
        env=env,
        check=True,
    )
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_the_mcp_tool_list_does_not_advertise_them():
    """A tool in the list is a promise an agent reads before it decides."""
    names = _tool_names(enabled=False)
    offered = [n for n in names if "jira" in n or "linear" in n]
    assert not offered, f"advertised with the feature off: {offered}"
    assert len(names) > 20, "the rest of the tools should still be there"


def test_the_tool_list_grows_by_exactly_the_integrations():
    off_names = set(_tool_names(enabled=False))
    on_names = set(_tool_names(enabled=True))
    added = on_names - off_names
    assert off_names < on_names, "switching it on should only add"
    assert added, "nothing came back"
    assert all("jira" in n or "linear" in n for n in added), sorted(added)


def test_the_tool_functions_are_still_importable_and_tested(off):
    """Hidden, not deleted. The suite still runs against these."""
    from flanner import server

    for name in (
        "configure_jira_tool",
        "configure_linear_tool",
        "link_plan_to_jira_tool",
        "link_plan_to_linear_tool",
    ):
        assert callable(getattr(server, name)), name


def test_nothing_else_lost_its_tools():
    names = set(_tool_names(enabled=False))
    for expected in ("create_plan_file_tool", "list_projects", "memory_remember"):
        assert expected in names, expected


def test_the_switch_is_documented_where_somebody_would_look():
    """The refusal has to name the variable, or it is a dead end."""
    assert features.INTEGRATIONS_ENV in features.INTEGRATIONS_OFF
    assert "not enabled" in features.INTEGRATIONS_OFF


def test_the_module_says_why_rather_than_just_what():
    """A flag with no reasoning gets flipped back by the next person."""
    doc = importlib.import_module("flanner.features").__doc__ or ""
    assert "Linear and Jira" in doc
    assert len(doc.split()) > 60, "the reasoning is the point of this module"
