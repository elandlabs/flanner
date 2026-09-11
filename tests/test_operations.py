"""The operation registry agrees with what actually exists.

The site's capability tables were written by hand and drifted: they called
every Skills operation CLI-only while the web UI adopts, shares, rolls back
and installs, and they kept a tool count from before a feature was switched
off. The registry is checked against the running CLI, web app and MCP
server, so it cannot say something they do not do, or miss something they
do.
"""

import json
import os
import subprocess
import sys

import click

from flanner import operations

#: Routes a page calls for itself, not actions anybody takes.
PLUMBING = {
    "GET /projects/freshness-mix",
    "GET /nav/attention",
    "GET /events",
    "GET /plans/{plan_id}/revision",
    "GET /freshness/stream",
    "GET /api/projects",
    "GET /api/projects/{project_id}/plans",
    "GET /api/plans/{plan_file_id}",
    "POST /ipc/call",
}

_LIST_TOOLS = (
    "import asyncio, json; from flanner import server; "
    "print(json.dumps([t.name for t in asyncio.run(server._mcp.list_tools())]))"
)


def _cli_commands() -> set[str]:
    from flanner.cli import cli

    found: set[str] = set()

    def walk(group: click.Group, prefix: str) -> None:
        for name, command in group.commands.items():
            path = f"{prefix} {name}".strip()
            if isinstance(command, click.Group):
                walk(command, path)
            else:
                found.add(path)

    walk(cli, "")
    return found


def _web_routes() -> set[str]:
    from flanner.web import app

    found: set[str] = set()
    for route in app.routes:
        path = getattr(route, "path", "")
        if not path or path.startswith(("/static", "/openapi", "/docs", "/redoc")):
            continue
        for method in getattr(route, "methods", None) or ():
            if method != "HEAD":
                found.add(f"{method} {path}")
    return found


def _tools(*, integrations: bool) -> set[str]:
    """The advertised tool list, from a process of its own: it is built at import."""
    env = dict(os.environ)
    if integrations:
        env["FLANNER_INTEGRATIONS"] = "1"
    else:
        env.pop("FLANNER_INTEGRATIONS", None)
    out = subprocess.run(
        [sys.executable, "-c", _LIST_TOOLS], capture_output=True, text=True, env=env, check=True
    )
    return set(json.loads(out.stdout.strip().splitlines()[-1]))


def _named(field: str) -> list[str]:
    return [name for op in operations.OPERATIONS for name in getattr(op, field)]


def test_every_cli_command_is_registered_and_nothing_else_is():
    commands = _cli_commands()
    named = set(_named("cli"))
    assert named == commands, {
        "commands the registry misses": sorted(commands - named),
        "registry names no such command": sorted(named - commands),
    }


def test_no_command_is_claimed_by_two_operations():
    named = _named("cli")
    assert len(named) == len(set(named)), sorted({n for n in named if named.count(n) > 1})


def test_every_web_route_is_registered_or_is_plumbing():
    routes = _web_routes()
    named = set(_named("web"))
    assert named <= routes, {"registry names no such route": sorted(named - routes)}
    assert routes - PLUMBING <= named, {"routes the registry misses": sorted(routes - PLUMBING - named)}


def test_the_registry_names_exactly_the_tools_the_server_advertises():
    ungated = {name for op in operations.OPERATIONS if not op.gated for name in op.mcp}
    gated = {name for op in operations.OPERATIONS if op.gated for name in op.mcp}

    off = _tools(integrations=False)
    assert off == ungated, {
        "advertised but not registered": sorted(off - ungated),
        "registered but not advertised": sorted(ungated - off),
    }
    assert _tools(integrations=True) == ungated | gated


def test_anything_an_agent_cannot_call_says_why():
    silent = [op.action for op in operations.OPERATIONS if not op.mcp and not op.why_not_mcp]
    assert not silent, silent


def test_every_operation_uses_a_named_access_class_and_domain():
    assert {op.access for op in operations.OPERATIONS} <= set(operations.ACCESS)
    assert {op.domain for op in operations.OPERATIONS} <= set(operations.DOMAINS)


def test_the_export_is_deterministic_and_counts_the_default_tool_list():
    first = json.dumps(operations.as_json(), sort_keys=True)
    assert first == json.dumps(operations.as_json(), sort_keys=True)
    assert operations.as_json()["mcp_tool_count"] == len(_tools(integrations=False))


def test_release_markers_are_versions_and_add_up():
    """The site hides what has not shipped by these markers, so a typo in one
    would publish a tool early or hide it for good."""
    import re

    marked = [v for op in operations.OPERATIONS for v in (op.mcp_since, op.note_since) if v]
    assert all(re.fullmatch(r"\d+\.\d+\.\d+", v) for v in marked), marked
    newest = max(marked, key=lambda v: tuple(int(p) for p in v.split(".")))
    total = operations.as_json()["mcp_tool_count"]
    assert operations.released_tool_count(newest) == total
    assert operations.released_tool_count("0.0.0") < total
