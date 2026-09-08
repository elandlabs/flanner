"""
CLI tool for Flanner

Provides command-line interface for managing the Flanner server and projects.
"""

from __future__ import annotations

import contextlib
import logging
import os
import signal
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple, NoReturn

import click
from rich.text import Text

from . import tui
from .exceptions import DatabaseError, FlannerError, StorageError

if TYPE_CHECKING:  # annotations only; `from __future__` makes them strings
    from uuid import UUID

    from sqlalchemy.orm import Session

    from .database import ProjectModel


# --- deferred database access -----------------------------------------------
#
# `flanner --version` took 1.5 seconds, and 630 ms of that was importing
# SQLAlchemy for a command that prints a string. Every command paid it,
# including the ones that never open a store.
#
# These wrappers exist so the import happens on first use instead of at
# module load. They are deliberately thin and deliberately named exactly
# like the functions they forward to, so the forty-odd call sites in this
# file did not have to change and cannot drift from the real signatures.


def init_storage(base_path: str) -> None:
    from .storage import init_storage as _init_storage

    _init_storage(base_path)


def find_git_root(start_path: str) -> str | None:
    from .git_integration import find_git_root as _find_git_root

    return _find_git_root(start_path)


def update_gitignore(repo_root: str, pattern: str, comment: str | None = None) -> bool:
    from .git_integration import update_gitignore as _update_gitignore

    return _update_gitignore(repo_root, pattern, comment)


def get_session() -> Session:
    from .database import get_session as _get_session

    return _get_session()


def init_database(db_path: str | None = None) -> None:
    from .database import init_database as _init_database

    _init_database(db_path)


def get_project_by_name(session: Session, name: str) -> ProjectModel | None:
    from .database import get_project_by_name as _by_name

    return _by_name(session, name)


def db_list_projects(session: Session) -> list[ProjectModel]:
    from .database import list_projects as _list_projects

    return _list_projects(session)


# One console for the whole CLI, carrying the palette in tui.THEME.
console = tui.console


def get_mcp_dir() -> Path:
    """Get Flanner data directory (override with FLANNER_HOME)"""
    return Path(os.environ.get("FLANNER_HOME", Path.home() / ".flanner"))


def get_pid_file() -> Path:
    """Get path to PID file"""
    return get_mcp_dir() / "server.pid"


#: The line `peer serve` prints once its endpoint is genuinely up. Read by
#: `peer start` to tell "running" from "started and then failed".
PEER_READY = "Serving plans to authorised peers"


def peer_default_port() -> int:
    """The port `peer serve --http` listens on, without importing it early."""
    from . import peer as peer_transport

    return int(peer_transport.DEFAULT_PORT)


def beyond_loopback(host: str) -> bool:
    """Whether binding here lets other machines reach the port.

    Three commands ask this and each had its own tuple, in a different
    order. A list that drifts is a bind that is quietly exposed by one
    command and warned about by another.
    """
    return host not in ("127.0.0.1", "localhost", "::1")


def get_peer_pid_file() -> Path:
    """Where `flanner peer start` records the serving process.

    Its own file, not the MCP server's. The two are different processes with
    different lifetimes, and sharing a pid file would have `flanner stop`
    kill somebody's peer server.
    """
    return get_mcp_dir() / "peer.pid"


class Sectioned(click.Group):
    """A help screen grouped by what a command is for.

    Thirty-four commands in one alphabetical list put `accept` beside
    `claude-info` and `diff` beside `devices`, which tells a reader nothing
    about which of them they need. The order they are declared in is no
    better; only the grouping carries meaning.

    The split that matters most is the first one: everything above the line
    works with no account and no network, and the sections below it talk to
    a control plane, to other machines, or to a third party. Somebody
    deciding whether flanner is safe to run on a private repository should
    be able to see that from the help text.
    """

    #: Title, then the commands under it, in the order a person meets them
    #: rather than alphabetically. A command missing from here still shows —
    #: see `format_commands` — because a help screen that silently omits a
    #: command is worse than one that is untidy.
    SECTIONS: tuple[tuple[str, tuple[str, ...]], ...] = (
        (
            "Start here",
            ("init", "list", "web", "doctor"),
        ),
        (
            "Plans on this machine (no account, no network)",
            ("sync", "history", "diff", "config", "delete", "setup-gitignore"),
        ),
        (
            "Memory on this machine (no account, no network)",
            ("mem",),
        ),
        (
            "Is a plan still true (local, reads your git history)",
            ("freshness", "why"),
        ),
        (
            "Review and retire (signed locally; syncs only if you have a team)",
            ("review", "retire"),
        ),
        (
            "Your team (talks to the control plane)",
            ("login", "accept", "whoami", "logout", "invite", "members", "devices", "join"),
        ),
        (
            "Syncing with other machines",
            ("peer", "mesh"),
        ),
        (
            "Agent integration",
            ("setup", "register", "unregister", "claude-info", "start", "stop", "status"),
        ),
        (
            "Issue trackers (talks to Jira or Linear)",
            ("jira", "linear"),
        ),
    )

    def format_commands(self, ctx: click.Context, formatter: click.HelpFormatter) -> None:
        listed: set[str] = set()
        for title, names in self.SECTIONS:
            rows = []
            for name in names:
                command = self.get_command(ctx, name)
                if command is None or command.hidden:
                    continue
                listed.add(name)
                rows.append((name, (command.get_short_help_str(66))))
            if rows:
                with formatter.section(title):
                    formatter.write_dl(rows)

        # Anything added since this list was written. Falling back rather
        # than dropping it: a new command should look out of place here,
        # not vanish.
        rest = [
            (name, self.get_command(ctx, name).get_short_help_str(66))  # type: ignore[union-attr]
            for name in sorted(self.list_commands(ctx))
            if name not in listed and not getattr(self.get_command(ctx, name), "hidden", False)
        ]
        if rest:
            with formatter.section("Other"):
                formatter.write_dl(rest)


@click.group(cls=Sectioned)
@click.version_option(package_name="flanner")
@click.option("--verbose", is_flag=True, help="Show debug output")
@click.option("--quiet", is_flag=True, help="Only show errors")
def cli(verbose: bool, quiet: bool) -> None:
    """Flanner - Manage plan files for AI assistants"""
    level = logging.DEBUG if verbose else logging.ERROR if quiet else logging.WARNING
    logging.basicConfig(
        level=level, stream=sys.stderr, format="%(levelname)s %(name)s: %(message)s"
    )
    if verbose:
        # The flag promised "debug output" and delivered a log level over
        # seven log calls, which is close to nothing. What somebody actually
        # wants when a command felt slow is where the time went, so that is
        # what it prints — after the command, once there is something to say.
        import atexit

        atexit.register(_print_breakdown)


def _print_breakdown() -> None:
    """Where this invocation spent its time, under `--verbose`.

    Registered at exit rather than printed by each command, so a command
    that raises still reports what it managed to do first — which is the
    run you most want the numbers for.
    """
    from . import observe

    lines = observe.breakdown()
    if not lines:
        return
    console.print()
    console.print("timing", style="dim")
    for line in lines:
        console.print(line, style="dim")


def _register_claude_desktop() -> None:
    """Claude Desktop reads one global config file. Put the server in it."""
    from .claude_integration import auto_register_on_init

    ok, message = auto_register_on_init()
    console.print(
        f"{'OK' if ok else 'WARN'} Claude Desktop: {message}", style="green" if ok else "yellow"
    )
    if not ok:
        console.print("  Register it later with:  flanner register", style="white")


def _register_claude_code() -> None:
    """Register at user scope, through Claude Code's own CLI.

    Its config is not ours to write: the one time this package edited an
    agent's config by hand it clobbered it. Skipped when the server is
    already there, so adopting a second repository does not pay for a
    subprocess to be told nothing changed.
    """
    import json
    import shutil
    import subprocess

    from .claude_integration import claude_code_user_config_path

    manual = "  Add it manually:  claude mcp add -s user flanner -- flanner-mcp"
    try:
        user = json.loads(claude_code_user_config_path().read_text(encoding="utf-8"))
        if "flanner" in (user.get("mcpServers") or {}):
            console.print("OK Claude Code: already registered at user scope", style="green")
            return
    except (OSError, ValueError):
        pass

    claude_bin = shutil.which("claude")
    if not claude_bin:
        console.print(
            "- Claude Code CLI not found. To use flanner there globally:", style="yellow"
        )
        console.print(manual, style="white")
        return

    try:
        proc = subprocess.run(  # noqa: S603 (fixed argv, no shell, no untrusted input)
            [claude_bin, "mcp", "add", "-s", "user", "flanner", "--", "flanner-mcp"],
            capture_output=True,
            text=True,
            # Every other subprocess call in the package is bounded; this one
            # was not. `claude` is somebody else's binary, and if it blocks on
            # a prompt or a network call it takes `flanner init` down with it,
            # during the one command a new user runs first.
            timeout=30,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        detail = "timed out after 30s" if isinstance(e, subprocess.TimeoutExpired) else str(e)
        console.print(f"WARN Claude Code: {detail}", style="yellow")
        console.print(manual, style="white")
        return

    if proc.returncode == 0:
        console.print("OK Claude Code: registered flanner-mcp at user scope", style="green")
        return
    tail = (proc.stderr or proc.stdout).strip().splitlines()[-1:] or [""]
    console.print(f"WARN Claude Code: {tail[0]}", style="yellow")
    console.print(manual, style="white")


def _register_codex() -> None:
    """Report only. Codex's registration is TOML this deliberately does not edit."""
    from .claude_integration import CODEX_SNIPPET, codex_config_path, codex_registration

    if codex_registration():
        console.print("OK Codex: registered", style="green")
        return
    console.print(f"- Codex: not registered. Add to {codex_config_path()}:", style="yellow")
    for line in CODEX_SNIPPET.splitlines():
        # markup=False: Rich reads "[mcp_servers.flanner]" as a style tag and
        # prints nothing for it, which is the one line that matters.
        console.print(f"    {line}", style="white", markup=False)


#: The agents that can be registered, named as `flanner status` names them.
#: One list, so the flag, the status table and the registration cannot come
#: to disagree about what an agent is called.
CLAUDE_DESKTOP = "claude-desktop"
CLAUDE_CODE = "claude-code"
CODEX = "codex"
AGENTS = (CLAUDE_DESKTOP, CLAUDE_CODE, CODEX)


def _agents_to_register(chosen: tuple[str, ...], skip_claude: bool) -> tuple[str, ...]:
    """Turn what was asked for into the list to actually register.

    A repeatable option rather than one flag per agent, because a flag per
    agent cannot say whether `--setup-codex` means "Codex only" or "Codex as
    well as the default". Naming any agent means those agents and no others.
    """
    if skip_claude and not chosen:
        return ()
    if not chosen:
        return AGENTS
    if "none" in chosen:
        return ()
    if "all" in chosen:
        return AGENTS
    # Filtered through AGENTS rather than returned as given, so the order is
    # the order they are reported in however the flags were typed.
    return tuple(agent for agent in AGENTS if agent in chosen)


def _register_agents_globally(agents: tuple[str, ...] = AGENTS) -> None:
    """Make flanner reachable from every project, on this machine.

    Run by `init` as well as by `setup`. The local MCP server is not a
    trimming somebody opts into later: it is how an agent reaches flanner at
    all, so the command that creates the store is the right place to wire it
    up. Every step is idempotent and reports rather than raises, because this
    sits on top of a store that has already been created and failing here
    must not undo that.

    Only the global registrations live here. Per-repository wiring is
    `_setup_agent_integration`, which runs once for each repository adopted.
    """
    from .agent_hooks import upsert_global_nudge

    if not agents:
        return

    console.print("\n[Agents] Making flanner reachable from every project...", style="cyan")
    if CLAUDE_DESKTOP in agents:
        _register_claude_desktop()
    if CLAUDE_CODE in agents:
        _register_claude_code()
    if CODEX in agents:
        _register_codex()

    # The nudge is a block in Claude's own instruction file, so it follows
    # Claude rather than being written for somebody who asked only for Codex.
    if CLAUDE_DESKTOP not in agents and CLAUDE_CODE not in agents:
        return
    changed = upsert_global_nudge()
    where = Path.home() / ".claude" / "CLAUDE.md"
    console.print(
        f"OK Global nudge {'added to' if changed else 'already in'} {where}", style="green"
    )


def _import_existing_plans(project_root: str) -> None:
    """Import the plan files already sitting in the repository.

    The same scan `flanner sync` runs. Adopting a repository that already
    holds plan files and then listing none of them is the first thing
    somebody cloning a colleague's repo would see, and it reads as flanner
    having lost them.
    """
    from .database import get_project_by_root

    session = get_session()
    project = get_project_by_root(session, project_root)
    if project is None:
        return

    totals = {"scanned": 0, "imported": 0, "skipped": 0, "error": 0}
    _sync_project(session, project, False, totals)
    console.print(
        f"OK Imported {totals['imported']} of {totals['scanned']} plan file(s)", style="green"
    )


def _ask(question: str, default: str) -> str:
    """Ask for one line, and take the default when there is nobody to ask.

    Not `click.prompt`. Click raises the same `Abort` for end-of-input and
    for Ctrl-C, which leaves the caller guessing which happened, and the
    guess this used to make was `sys.stdin.isatty()`. On Windows the null
    device reports itself as a terminal, so `flanner init < NUL` -- a
    script, a CI step, exactly the unattended case the fallback existed
    for -- guessed "a person cancelled" and died with "Aborted!".

    Reading the line here keeps the two apart with no guess at all. An
    empty string back from `readline` is end-of-input on every platform,
    and Ctrl-C is still a KeyboardInterrupt that propagates the way it
    should.
    """
    click.echo(f"{question} [{default}]: ", nl=False)
    answer = sys.stdin.readline()
    if answer == "":
        # No input stream at all. Say which name was chosen rather than
        # appearing to hang and then inventing one.
        console.print(f"No terminal to ask, so the project is named '{default}'.", style="dim")
        return default
    return answer.strip() or default


def _adopt_repository(project_root: str, plan_dir: str, force_new_project: bool) -> None:
    """Make this repository a project, or report the one already here.

    Prompts for a name only when creating, so re-running `init` on an adopted
    repository is silent and safe — which is what makes it fair to tell people
    it can be re-run at any time.
    """
    from .database import get_project_by_root

    try:
        existing = get_project_by_root(get_session(), project_root)
    except Exception as e:  # noqa: BLE001 - reported, not swallowed; see below
        # Deliberately broad, and deliberately not fatal. The store exists by
        # now, so the useful outcome is to say what failed and leave the rest
        # of `init` intact rather than abort a half-finished setup.
        console.print(f"WARN Could not check for an existing project: {e}", style="yellow")
        console.print("  Skipping project creation to be safe", style="yellow")
        return

    if existing and not force_new_project:
        console.print(f"OK Project already exists: {existing.name}", style="green")
        console.print(f"  Plan directory: {existing.plan_directory}", style="white")
        console.print(f"  Plan files: {len(existing.plan_files)}", style="white")
        console.print("\n  Tip: MCP server registration still completed above.", style="cyan")
        console.print(
            "  You can run 'flanner init' anytime to ensure everything is set up!", style="cyan"
        )
        return

    if existing:
        console.print(f"WARN Project '{existing.name}' already exists here", style="yellow")
        console.print("  Creating a new project anyway (--force-new-project)", style="yellow")

    offered = Path(project_root).name
    project_name = _ask("Enter project name", offered)

    from .server import create_project_tool

    result = create_project_tool(
        name=project_name, project_root=project_root, plan_directory=plan_dir
    )
    if result.get("error"):
        console.print(f"ERROR Error: {result['message']}", style="red")
        return

    console.print(f"OK Created project: {project_name}", style="green")
    console.print(f"OK Plan directory: {result['full_plan_path']}", style="green")
    if result.get("gitignore_updated"):
        console.print("OK Updated .gitignore to exclude plan files", style="green")
    else:
        console.print("OK .gitignore already excludes plan files", style="green")


@cli.command()
@click.option("--project-root", default=None, help="Project root path")
@click.option("--plan-dir", default=".plans", help="Plan directory name")
@click.option(
    "--setup",
    "setup_agents",
    multiple=True,
    type=click.Choice(("all", "none", *AGENTS)),
    help="Which agents to register with. Repeatable. Default: all",
)
@click.option(
    "--skip-claude", is_flag=True, help="Register with no agent at all (same as --setup none)"
)
@click.option("--sync", is_flag=True, help="Import plan files already in the repository")
@click.option(
    "--force-new-project", is_flag=True, help="Force create new project even if one exists"
)
def init(
    project_root: str | None,
    plan_dir: str,
    setup_agents: tuple[str, ...],
    skip_claude: bool,
    sync: bool,
    force_new_project: bool,
) -> None:
    """Initialize Flanner

    Creates this machine's store, registers the MCP server everywhere an
    agent looks for it, and adopts the repository you run it in. The
    registration used to be a separate `flanner setup`; it is not optional
    enough to be its own step, since without it no agent can reach flanner.
    `setup` still exists for repairing it on its own.

    `--setup` narrows which agents that means, and is repeatable, so
    `--setup codex` registers Codex and nothing else. `--sync` also imports
    the plan files already in the repository, which is what you want when
    adopting one somebody else set up.
    """
    mcp_dir = get_mcp_dir()

    # Initialize storage
    init_storage(str(mcp_dir))

    # Initialize database
    db_path = mcp_dir / "data.db"
    init_database(str(db_path))

    console.print(f"OK Initialized Flanner at {mcp_dir}", style="green")
    console.print(f"OK Database created at {db_path}", style="green")

    # The global half: Claude Desktop, Claude Code at user scope, Codex and
    # the adoption nudge. The per-repository half is _setup_agent_integration
    # below, which writes .mcp.json and the guard-write hook.
    _register_agents_globally(_agents_to_register(setup_agents, skip_claude))

    if project_root or (project_root := find_git_root(os.getcwd())):
        console.print(f"\nOK Detected git repository at: {project_root}", style="green")
        _adopt_repository(project_root, plan_dir, force_new_project)
        _setup_agent_integration(project_root)
        if sync:
            _import_existing_plans(project_root)


def _setup_agent_integration(project_root: str) -> None:
    """Wire the CLAUDE.md block, guard-write hook, and skill for this repo."""
    from .agent_hooks import wire_agent_integration
    from .database import get_project_by_root

    try:
        project = get_project_by_root(get_session(), project_root)
        if not project:
            return
        console.print("\n[Agent] Setting up coding-agent integration...", style="cyan")
        wiring = wire_agent_integration(project_root, project)
        for item in wiring.installed:
            console.print(f"OK Installed {item}", style="green")
        for reason in wiring.skipped:
            tui.warn(f"Left alone: {reason}")
    except Exception as e:
        console.print(f"WARN Could not set up agent integration: {e}", style="yellow")


# --- memory ------------------------------------------------------------------


@cli.group()
def mem() -> None:
    """Durable context: what a later session needs to know"""


def _mem_project(session: Any, project: str | None) -> Any:
    """The project a memory command is operating in, or None for personal."""
    from . import memory_ops

    if project:
        found = get_project_by_name(session, project)
        if found is None:
            _no_project(project)
        return found
    return memory_ops.resolve_project(session)


def _mem_or_exit(session: Any, memory_id: str) -> Any:
    from .database import get_memory

    try:
        found = get_memory(session, UUID(memory_id))
    except (ValueError, AttributeError):
        found = None
    if found is None:
        tui.bad(f"No memory with id {memory_id}")
        tui.hint(f"  {tui.command('flanner mem list')} shows what is there.")
        raise SystemExit(1)
    return found


@mem.command("remember")
@click.argument("content")
@click.option(
    "--category",
    type=click.Choice(
        ["fact", "decision", "preference", "constraint", "lesson", "relationship", "task_context"]
    ),
    required=True,
    help="What kind of thing this is",
)
@click.option(
    "--scope",
    type=click.Choice(["project", "personal"]),
    default="project",
    help="This repository, or you across every project",
)
@click.option("--project", default=None, help="Project name (uses current directory if omitted)")
@click.option("--title", default=None, help="Short summary (derived from the body if omitted)")
@click.option(
    "--confidence",
    type=click.Choice(["confirmed", "inferred", "speculative"]),
    default="confirmed",
    help="How much this is stood behind",
)
@click.option("--ref", "refs", multiple=True, help="What supports it; repeatable")
@click.option("--by", "created_by", default=None, help="Who is remembering (defaults to you)")
def mem_remember(
    content: str,
    category: str,
    scope: str,
    project: str | None,
    title: str | None,
    confidence: str,
    refs: tuple[str, ...],
    created_by: str | None,
) -> None:
    """Save one durable fact

    Reads the body from stdin when CONTENT is `-`, so a long memory does
    not have to survive shell quoting.

    Examples:

      flanner mem remember "Use UTC-naive timestamps in SQLite" --category decision

      flanner mem remember - --category lesson < note.txt
    """
    from . import memory_ops

    body = sys.stdin.read() if content == "-" else content
    session = _require_session()
    proj = _mem_project(session, project) if scope == "project" else None
    if scope == "project" and proj is None:
        _no_project(project)

    try:
        memory, created = memory_ops.remember(
            session,
            content=body,
            category=category,
            scope=scope,
            project=proj,
            title=title,
            confidence=confidence,
            source_refs=list(refs),
            created_by=created_by or _whoami(),
        )
    except memory_ops.SecretRejected as e:
        console.print()
        tui.bad(str(e))
        tui.hint("  Nothing was written. Remove the credential and try again.")
        console.print()
        raise SystemExit(1) from None
    except (DatabaseError, ValueError) as e:
        console.print()
        tui.bad(str(e))
        console.print()
        raise SystemExit(1) from None

    console.print()
    if created:
        tui.ok(f"Remembered: {memory.title}")
    else:
        tui.note(f"Already remembered: {memory.title}")
    console.print(f"  {tui.code(str(memory.id))}", style="muted")
    console.print(f"  {tui.code(memory.file_path)}", style="muted")
    console.print()


@mem.command("recall")
@click.argument("query")
@click.option("--project", default=None, help="Project name (uses current directory if omitted)")
@click.option("--no-personal", is_flag=True, help="Search this project only")
@click.option("--limit", default=8, help="How many results at most")
@click.option("--full", is_flag=True, help="Whole bodies rather than summaries")
@click.option("--output", type=click.Choice(["table", "json"]), default="table")
def mem_recall(
    query: str, project: str | None, no_personal: bool, limit: int, full: bool, output: str
) -> None:
    """Search what earlier sessions knew

    Every result says why it matched and who wrote it, because a memory you
    cannot judge is one you have to go and verify anyway.
    """
    from . import memory_ops

    session = _require_session()
    proj = _mem_project(session, project)
    found = memory_ops.recall(
        session,
        query=query,
        project_id=proj.id if proj else None,
        include_personal=not no_personal,
        limit=limit,
        full=full,
    )

    if output == "json":
        import json

        click.echo(json.dumps(found, indent=2))
        return

    console.print()
    if not found["memories"]:
        tui.note(f"Nothing remembered about {query!r}.")
        if found["search"] == "scan":
            console.print(
                "  This Python's SQLite has no full-text index, so this was a plain scan.",
                style="muted",
            )
        console.print()
        return

    for item in found["memories"]:
        heading = Text()
        heading.append(item["title"], style="value")
        heading.append(f"  {item['category']}", style="muted")
        console.print(heading)
        console.print(f"  {item.get('body') or item['summary']}", style="white")
        console.print(
            f"  {item['scope']} · {item['confidence']} · {item['created_by']} · "
            f"{item['match_reason']}",
            style="muted",
        )
        console.print(f"  {tui.code(item['id'])}", style="muted")
        console.print()

    if found["truncated"]:
        tui.note("More matched than are shown. Narrow the query or raise --limit.")
        console.print()


@mem.command("show")
@click.argument("memory_id")
@click.option("--output", type=click.Choice(["table", "json"]), default="table")
def mem_show(memory_id: str, output: str) -> None:
    """One memory in full, with everything that happened to it"""
    from . import memory_ops

    session = _require_session()
    _mem_or_exit(session, memory_id)
    detail = memory_ops.describe(session, UUID(memory_id))

    if output == "json":
        import json

        click.echo(json.dumps(detail, indent=2))
        return

    console.print()
    console.print(detail["title"], style="value")
    console.print()
    console.print(detail["body"], style="white")
    console.print()
    for label, key in (
        ("Scope", "scope"),
        ("Category", "category"),
        ("Status", "status"),
        ("Confidence", "confidence"),
        ("Written by", "created_by"),
        ("Written", "created_at"),
        ("File", "file_path"),
    ):
        console.print(f"{label:<12}{detail[key]}", style="muted")
    if detail["source_refs"]:
        console.print(f"{'Sources':<12}{', '.join(detail['source_refs'])}", style="muted")
    if detail["supersedes"]:
        console.print(f"{'Replaces':<12}{detail['supersedes']}", style="muted")

    if detail["events"]:
        console.print()
        for event in detail["events"]:
            console.print(f"  {event['at']}  {event['action']} by {event['actor']}", style="muted")
    console.print()


@mem.command("list")
@click.option("--scope", type=click.Choice(["project", "personal"]), default=None)
@click.option("--category", default=None, help="One of the seven categories")
@click.option("--status", default="active", help="active, superseded, forgotten, expired, or all")
@click.option("--project", default=None, help="Project name (uses current directory if omitted)")
@click.option("--output", type=click.Choice(["table", "json"]), default="table")
def mem_list(
    scope: str | None, category: str | None, status: str, project: str | None, output: str
) -> None:
    """Browse memories rather than searching them"""
    from .database import list_memories

    session = _require_session()
    proj = _mem_project(session, project) if scope != "personal" else None
    memories = list_memories(
        session,
        scope=scope,
        project_id=proj.id if proj and scope == "project" else None,
        category=category,
        status=None if status == "all" else status,
    )

    if output == "json":
        import json

        click.echo(
            json.dumps(
                [
                    {
                        "id": str(m.id),
                        "title": m.title,
                        "category": m.category,
                        "scope": m.scope,
                        "status": m.status,
                        "created_at": m.created_at.isoformat() + "Z" if m.created_at else None,
                    }
                    for m in memories
                ],
                indent=2,
            )
        )
        return

    console.print()
    if not memories:
        tui.note("Nothing remembered here yet.")
        tui.hint(f"  {tui.command('flanner mem remember')} saves the first one.")
        console.print()
        return

    table = tui.table("Memory", "Category", "Scope", "Written")
    for memory in memories:
        table.add_row(
            memory.title[:60],
            memory.category,
            memory.scope,
            memory.created_at.strftime("%Y-%m-%d") if memory.created_at else "",
        )
    console.print(table)
    console.print()


@mem.command("supersede")
@click.argument("memory_id")
@click.option("--with", "content", required=True, help="What is true instead")
@click.option("--reason", default="", help="Why it changed")
def mem_supersede(memory_id: str, content: str, reason: str) -> None:
    """Correct a memory by replacing it

    The old one stops being recalled and stays readable, pointing at what
    replaced it. A decision keeps its history that way.
    """
    result = _dispatch_or_exit(
        "memory_supersede",
        {
            "memory_id": memory_id,
            "content": content,
            "reason": reason,
            "created_by": _whoami(),
        },
    )
    console.print()
    tui.ok(result["message"])
    console.print(f"  {tui.code(result['id'])}", style="muted")
    console.print()


@mem.command("forget")
@click.argument("memory_id")
@click.option("--reason", default="", help="Why")
@click.option(
    "--purge",
    is_flag=True,
    help="Delete the file and every trace of it. Cannot be undone.",
)
def mem_forget(memory_id: str, reason: str, purge: bool) -> None:
    """Stop recalling a memory, or erase it from this machine"""
    session = _require_session()
    memory = _mem_or_exit(session, memory_id)

    if purge and not click.confirm(
        f"Permanently delete {memory.title!r} and its file?", default=False
    ):
        tui.note("Nothing was deleted.")
        return

    result = _dispatch_or_exit(
        "memory_forget",
        {"memory_id": memory_id, "reason": reason, "purge": purge, "created_by": _whoami()},
    )
    console.print()
    tui.ok(result["message"])
    console.print()


@mem.command("restore")
@click.argument("memory_id")
def mem_restore(memory_id: str) -> None:
    """Bring a forgotten or expired memory back into recall"""
    result = _dispatch_or_exit("memory_restore", {"memory_id": memory_id, "created_by": _whoami()})
    console.print()
    tui.ok(result["message"])
    console.print()


@mem.command("rebuild")
def mem_rebuild() -> None:
    """Rebuild the catalog and search index from the memory files

    The files are the record; this is what makes that true. Run it after
    restoring a backup, or after losing the database.
    """
    result = _dispatch_or_exit("memory_rebuild", {})
    console.print()
    tui.ok(f"{result['adopted']} adopted, {result['updated']} updated")
    if result["skipped"]:
        console.print(f"  {result['skipped']} file(s) were not memories", style="muted")
    for failure in result["failed"]:
        tui.warn(f"  {failure}")
    console.print(
        "  Event history is not restored: events have no file of their own.", style="muted"
    )
    console.print()


@mem.command("pending")
@click.option("--project", default=None, help="Project name (uses current directory if omitted)")
@click.option("--output", type=click.Choice(["table", "json"]), default="table")
def mem_pending(project: str | None, output: str) -> None:
    """Suggestions waiting on you

    Nothing here is being recalled. A suggestion sits out of the way until
    somebody decides on it, which is the whole point of proposing rather
    than saving.
    """
    from . import memory_ops

    session = _require_session()
    proj = _mem_project(session, project)
    waiting = memory_ops.pending(session, project_id=proj.id if proj else None)

    if output == "json":
        import json

        click.echo(json.dumps(waiting, indent=2))
        return

    console.print()
    if not waiting:
        tui.note("Nothing waiting.")
        console.print()
        return

    for item in waiting:
        console.print(item["title"], style="value")
        console.print(f"  {item['body']}", style="white")
        if item.get("why_durable"):
            console.print(f"  why: {item['why_durable']}", style="muted")
        if item.get("possible_conflict_with"):
            tui.warn(f"  may contradict {item['possible_conflict_with']}")
        console.print(
            f"  {item['category']} · {item['confidence']} · {item['created_by']}", style="muted"
        )
        console.print(f"  {tui.code(item['id'])}", style="muted")
        console.print()

    tui.hint(
        f"  {tui.command('flanner mem approve <id>')} or {tui.command('flanner mem reject <id>')}"
    )
    console.print()


@mem.command("approve")
@click.argument("memory_id")
@click.option("--edit", "content", default=None, help="Keep this text instead of what was offered")
@click.option(
    "--supersede",
    is_flag=True,
    help="This replaces the memory it was flagged as contradicting",
)
def mem_approve(memory_id: str, content: str | None, supersede: bool) -> None:
    """Keep a suggestion, and start recalling it"""
    result = _dispatch_or_exit(
        "memory_decide",
        {
            "memory_id": memory_id,
            "decision": "edit" if content else "approve",
            "content": content,
            "supersede_conflict": supersede,
            "created_by": _whoami(),
        },
    )
    console.print()
    tui.ok(result["message"])
    console.print(f"  {tui.code(result['id'])}", style="muted")
    console.print()


@mem.command("reject")
@click.argument("memory_id")
def mem_reject(memory_id: str) -> None:
    """Turn down a suggestion and remove it

    Rejected suggestions are deleted rather than kept as history. A queue
    that remembers everything anybody declined stops being a queue.
    """
    result = _dispatch_or_exit(
        "memory_decide",
        {"memory_id": memory_id, "decision": "reject", "created_by": _whoami()},
    )
    console.print()
    tui.ok(result["message"])
    console.print()


@mem.command("mode")
@click.argument(
    "capture_mode",
    required=False,
    type=click.Choice(["off", "explicit", "suggest", "auto_safe"]),
)
@click.option("--project", default=None, help="Project name (uses current directory if omitted)")
def mem_mode(capture_mode: str | None, project: str | None) -> None:
    """Show or set how much this project captures

    With no argument, says what the current mode is and where it came from.
    With one, writes it into this project's policy file.
    """
    from . import memory_ops
    from .memory_policy import POLICY_FILENAME

    session = _require_session()
    proj = _mem_project(session, project)
    if proj is None or not proj.project_root:
        _no_project(project)

    policy = memory_ops.policy_for(proj)
    if capture_mode is None:
        console.print()
        console.print(f"Capture mode  {policy.capture_mode}", style="value")
        console.print(f"  from {policy.provenance.get('capture_mode', 'default')}", style="muted")
        console.print()
        return

    path = Path(proj.project_root) / ".flanner" / POLICY_FILENAME
    _set_capture_mode(path, capture_mode)

    after = memory_ops.policy_for(proj)
    console.print()
    if after.capture_mode != capture_mode:
        # The merge refuses anything that would loosen the global policy,
        # so saying "set" here would be a lie the next run exposes.
        tui.warn(
            f"Written, but the effective mode is still {after.capture_mode}: "
            "a project may only tighten what the global policy allows."
        )
    else:
        tui.ok(f"Capture mode is now {capture_mode} for {proj.name}")
    console.print(f"  {tui.code(str(path))}", style="muted")
    console.print()


def _set_capture_mode(path: Path, capture_mode: str) -> None:
    """Write one key into a project policy file, keeping the rest.

    Rewritten with yaml rather than edited as text, because a hand-edited
    file may have comments in places no line-based edit can predict, and
    losing somebody's comments is a worse outcome than losing formatting.
    """
    import yaml

    from .memory_policy import read_file
    from .storage import atomic_write_text

    data = read_file(path)
    data.setdefault("version", 1)
    data["capture_mode"] = capture_mode
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, yaml.dump(data, default_flow_style=False, sort_keys=False))


@mem.group("policy")
def mem_policy() -> None:
    """What this project will let be remembered"""


@mem_policy.command("show")
@click.option("--project", default=None, help="Project name (uses current directory if omitted)")
@click.option("--output", type=click.Choice(["table", "json"]), default="table")
def mem_policy_show(project: str | None, output: str) -> None:
    """Every setting, and which file decided it"""
    from . import memory_ops
    from .memory_policy import explain

    session = _require_session()
    proj = _mem_project(session, project)
    policy = memory_ops.policy_for(proj)
    rows = explain(policy)

    if output == "json":
        import json

        click.echo(
            json.dumps(
                {
                    "settings": [
                        {"name": n, "value": list(v) if isinstance(v, tuple) else v, "from": src}
                        for n, v, src in rows
                    ],
                    "refused": list(policy.refused),
                },
                indent=2,
            )
        )
        return

    console.print()
    table = tui.table("Setting", "Value", "From")
    for name, value, source in rows:
        shown = ", ".join(value) if isinstance(value, tuple) else str(value)
        table.add_row(name, shown or "—", source)
    console.print(table)

    for refusal in policy.refused:
        console.print()
        tui.warn(refusal)
    console.print()


@mem_policy.command("validate")
@click.option("--project", default=None, help="Project name (uses current directory if omitted)")
def mem_policy_validate(project: str | None) -> None:
    """Check both policy files, and say everything wrong with them"""
    from . import memory_ops
    from .memory_policy import POLICY_FILENAME, read_file, validate

    session = _require_session()
    proj = _mem_project(session, project)

    problems: list[str] = []
    for label, path in (
        ("global policy", get_mcp_dir() / POLICY_FILENAME),
        (
            "project policy",
            Path(proj.project_root) / ".flanner" / POLICY_FILENAME
            if proj and proj.project_root
            else None,
        ),
    ):
        if path is None:
            continue
        try:
            problems += validate(read_file(path), where=label)
        except (DatabaseError, ValueError) as e:
            problems.append(str(e))

    console.print()
    if problems:
        for problem in problems:
            tui.bad(problem)
        console.print()
        raise SystemExit(1)

    policy = memory_ops.policy_for(proj)
    tui.ok("Both policy files are valid.")
    console.print(f"  Capture mode is {policy.capture_mode}.", style="muted")
    for refusal in policy.refused:
        tui.warn(f"  {refusal}")
    console.print()


@mem_policy.command("init")
@click.option("--project", default=None, help="Project name (uses current directory if omitted)")
@click.option("--force", is_flag=True, help="Overwrite an existing policy file")
def mem_policy_init(project: str | None, force: bool) -> None:
    """Write a commented policy file with every setting at its default"""
    from .memory_policy import POLICY_FILENAME, example
    from .storage import atomic_write_text

    session = _require_session()
    proj = _mem_project(session, project)
    if proj is None or not proj.project_root:
        _no_project(project)

    path = Path(proj.project_root) / ".flanner" / POLICY_FILENAME
    if path.exists() and not force:
        console.print()
        tui.bad(f"{path} already exists.")
        tui.hint("  Pass --force to replace it.")
        console.print()
        raise SystemExit(1)

    atomic_write_text(path, example())
    console.print()
    tui.ok(f"Wrote {path}")
    console.print(
        "  Every value in it is the default; delete what you do not change.", style="muted"
    )
    console.print()


def _dispatch_or_exit(op: str, args: dict[str, Any]) -> dict[str, Any]:
    """Run a memory write, or print why it was refused and stop."""
    _open_store()
    from .services import dispatch

    result = dispatch(op, args)
    if result.get("error"):
        console.print()
        tui.bad(result["message"])
        console.print()
        raise SystemExit(1)
    return result


def _whoami() -> str:
    """Who to record as the author of a memory written from the CLI."""
    import getpass

    try:
        return getpass.getuser()
    except Exception:  # noqa: BLE001 - a nameless user is still a user
        return "you"


@cli.group()
def hook() -> None:
    """Claude Code hook entry points (invoked by the harness, not by hand)."""


@hook.command("guard-write")
def guard_write() -> None:
    """PreToolUse guard: deny raw Writes into a flanner-managed plan directory."""
    from .agent_hooks import run_guard_write

    raw = sys.stdin.read()
    try:
        init_database()  # fresh hook process has no session yet
        output = run_guard_write(raw, get_session())
    except Exception:
        output = ""  # fail open: never block a write because the guard broke
    if output:
        click.echo(output)


def _process_alive(pid: int) -> bool:
    """Whether a process id belongs to something still running.

    Not `os.kill(pid, 0)`. That is the posix idiom, and on Windows it
    answers a different question: it reports a process as alive whenever a
    handle to it can still be opened, which stays true after the process has
    exited. Measured directly — a child run to completion, then asked about
    — the posix idiom says "running".

    That is the wrong answer in the direction that hurts. A server that
    crashed is reported as up, so `status` sends somebody looking for it and
    `start` refuses to replace it, with the only escape being to delete the
    pid file by hand. Waiting on the handle asks whether it has been
    signalled, which is the question actually being asked.
    """
    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True

    import ctypes

    SYNCHRONIZE = 0x00100000
    STILL_RUNNING = 0x00000102  # WAIT_TIMEOUT: the handle never signalled
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined,unused-ignore]  # nt only
    handle = kernel32.OpenProcess(SYNCHRONIZE, False, pid)
    if not handle:
        return False
    try:
        return bool(kernel32.WaitForSingleObject(handle, 0) == STILL_RUNNING)
    finally:
        kernel32.CloseHandle(handle)


def _running_pid(pid_file: Path) -> int | None:
    """The live pid this file records, deleting it when the process is gone.

    A pid file outliving its process is what a crash leaves behind, and
    reporting that as "running" sends people looking for something that is
    not there.
    """
    if not pid_file.exists():
        return None
    try:
        pid = int(pid_file.read_text())
    except (OSError, ValueError):
        pid_file.unlink(missing_ok=True)
        return None
    if _process_alive(pid):
        return pid
    pid_file.unlink(missing_ok=True)
    return None


def _accepting(port: int, *, timeout: float, child: Any = None) -> bool:
    """Wait until something answers on the port, or give up.

    The timeout is generous because a cold start imports the whole package
    and may create the database, which on a slow disk is tens of seconds. It
    can afford to be: a `child` that has already exited ends the wait at
    once, so the long deadline is only ever spent on a server still coming
    up, never on one that has crashed.
    """
    import socket

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if child is not None and child.poll() is not None:
            return False
        with contextlib.suppress(OSError), socket.create_connection(("127.0.0.1", port), 0.3):
            return True
        time.sleep(0.1)
    return False


def _log_mentions(path: Path, needle: str, *, since: int, timeout: float, child: Any) -> bool:
    """Wait until the child writes a known line, or dies trying.

    The port trick `_accepting` uses does not work here: the default peer
    server opens no port at all -- it dials out and answers on that
    connection, which is what makes it reachable without one. So readiness is
    read from what the child prints, which it prints only once its endpoint
    is actually up.

    `since` is the log's size before the child started, so a line from an
    earlier run cannot be mistaken for this one's.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if child.poll() is not None:
            return False
        with contextlib.suppress(OSError):
            with path.open("r", encoding="utf-8", errors="replace") as log:
                log.seek(since)
                if needle in log.read():
                    return True
        time.sleep(0.1)
    return False


def _stop_pid(pid_file: Path, what: str) -> None:
    """Stop whatever a pid file records, and say what happened."""
    pid = _running_pid(pid_file)
    if pid is None:
        tui.note("Not running.")
        return

    try:
        os.kill(pid, signal.SIGTERM)
    except OSError as e:
        tui.warn(f"Could not stop pid {pid}: {e}")
        return

    # Give it a moment to go, so `stop` followed by `start` does not collide
    # with a process that is still shutting down.
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and _process_alive(pid):
        time.sleep(0.1)

    pid_file.unlink(missing_ok=True)
    tui.ok(f"{what} stopped")


@cli.command()
@click.option(
    "--port",
    type=int,
    default=lambda: int(os.environ.get("FLANNER_MCP_PORT", "8765")),
    help="Port to serve on (env: FLANNER_MCP_PORT)",
)
def start(port: int) -> None:
    """Run the MCP server in the background

    Most clients spawn their own copy over stdio and need nothing here. This
    is for the ones that cannot: a client that only speaks http, a second
    editor that should share one server, or working with flanner on its own.

    It listens on 127.0.0.1 and nowhere else. The tools carry the full
    authority of whoever started them and there is nothing in front of them,
    so this is a local convenience and never a service to expose.
    """
    import subprocess
    import sys

    from .server import DEFAULT_HTTP_PORT  # noqa: F401  (documents the shared default)

    pid_file = get_pid_file()
    already = _running_pid(pid_file)
    if already is not None:
        tui.warn(f"Already running (pid {already}). Stop it with 'flanner stop'.")
        return

    log_path = get_mcp_dir() / "server.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    detach: dict[str, Any] = (
        {"creationflags": subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP}
        if os.name == "nt"
        else {"start_new_session": True}
    )
    with log_path.open("ab") as log:
        child = subprocess.Popen(  # noqa: S603 - fixed argv, no shell, no user input
            [sys.executable, "-m", "flanner.server", "--http", "--port", str(port)],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            **detach,
        )

    # A pid recorded before the port answers is a pid `stop` can act on even
    # if the server dies while starting, which is the case that otherwise
    # leaves an orphan nothing knows how to kill.
    pid_file.write_text(str(child.pid))

    console.print(f"Starting on port {port}...", style="muted")
    if not _accepting(port, timeout=90.0, child=child):
        pid_file.unlink(missing_ok=True)
        with contextlib.suppress(OSError):
            child.terminate()
        tui.warn(f"The server did not come up on port {port}.")
        console.print(f"  Its output: {tui.code(str(log_path))}", style="muted")
        raise SystemExit(2)

    console.print()
    tui.ok(f"MCP server running on http://127.0.0.1:{port}/mcp")
    console.print(f"  pid {child.pid}, logging to {tui.code(str(log_path))}", style="muted")
    console.print()
    console.print("Point an http MCP client at that url, or use the stdio config:", style="white")

    import json

    from .claude_integration import get_local_server_config

    console.print(
        json.dumps({"mcpServers": {"flanner": get_local_server_config()}}, indent=2),
        style="yellow",
    )
    console.print()


@cli.command()
def stop() -> None:
    """Stop the background MCP server"""
    _stop_pid(get_pid_file(), "Server")


def _server_row(pid_file: Path) -> Text:
    """Whether the background MCP server is up.

    Only the one `flanner start` runs. A client that spawns its own copy
    over stdio owns that process and does not record a pid here, so this
    saying "stopped" is not the same as flanner being unavailable.
    """
    running_pid = _running_pid(pid_file)

    if running_pid is not None:
        server = tui.dot("ok", label="running")
        server.append(f"  (pid {running_pid})", style="muted")
        return server

    server = tui.dot("unknown", label="stopped")
    server.append("  start it with ", style="muted")
    server.append("flanner start", style="accent")
    return server


def _catalog_rows(db_path: Path) -> list[tuple[str, Any]]:
    """Where the catalog is and what it holds.

    The counts get their own row rather than being appended to the path: the
    path is long enough to push them off the edge of an 80-column terminal,
    and the counts are the part worth reading.
    """
    from sqlalchemy.exc import SQLAlchemyError

    if not db_path.exists():
        return [("Database", Text("not initialized yet", style="warn"))]

    rows: list[tuple[str, Any]] = [("Database", Text(str(db_path), style="value"))]
    try:
        init_database(str(db_path))
        projects = db_list_projects(get_session())
    except (FlannerError, SQLAlchemyError):
        rows.append(("Catalog", Text("unreadable", style="bad")))
        return rows

    total_plans = sum(len(p.plan_files) for p in projects)
    catalog = Text()
    catalog.append(f"{len(projects)} project{'' if len(projects) == 1 else 's'}", style="value")
    catalog.append(f"  {tui.MIDDOT}  ", style="muted")
    catalog.append(f"{total_plans} plan{'' if total_plans == 1 else 's'}", style="value")
    rows.append(("Catalog", catalog))
    return rows


def _claude_code_row() -> Text:
    """Whether Claude Code will find the server, checked where it looks.

    Not Claude Desktop's file. `status` read claude_desktop_config.json and
    called the result "Claude Code", and neither of the things `init` and
    `setup` write for Claude Code — the project's .mcp.json, the user-scope
    entry from `claude mcp add` — lives there. A correct setup read as "not
    registered", on the command a new user runs to see whether it worked.
    """
    from .claude_integration import claude_code_registration

    where = claude_code_registration(Path.cwd())
    if where:
        row = tui.dot("ok", label="registered")
        row.append(f"  {where}", style="muted")
        return row
    row = tui.dot("unknown", label="not registered")
    row.append("  run ", style="muted")
    row.append("flanner init", style="accent")
    row.append(" in the repository, or ", style="muted")
    row.append("flanner setup", style="accent")
    return row


def _codex_row() -> Text:
    """Codex reads the AGENTS.md block, but registers MCP servers itself."""
    from .claude_integration import codex_config_path, codex_registration

    if codex_registration():
        return tui.dot("ok", label="registered")
    row = tui.dot("unknown", label="not registered")
    row.append(f"  add [mcp_servers.flanner] to {codex_config_path()}", style="muted")
    return row


def _claude_row(claude_status: dict[str, Any]) -> Text:
    """Whether Claude Desktop is registered, and whether its config still matches."""
    if claude_status["registered"] and claude_status["config_valid"]:
        return tui.dot("ok", label="registered")
    if claude_status["registered"]:
        registered = tui.dot("warn", label="registered")
        registered.append("  config is out of date", style="warn")
        return registered

    registered = tui.dot("unknown", label="not registered")
    registered.append("  run ", style="muted")
    registered.append("flanner register", style="accent")
    return registered


@cli.command()
def status() -> None:
    """Show server status"""
    from .claude_integration import check_server_status

    claude_status = check_server_status()
    rows: list[tuple[str, Any]] = [("MCP server", _server_row(get_pid_file()))]
    rows.extend(_catalog_rows(get_mcp_dir() / "data.db"))
    rows.append(("Claude Desktop", _claude_row(claude_status)))
    rows.append(("Claude Code", _claude_code_row()))
    rows.append(("Codex", _codex_row()))
    rows.append(("Desktop config", Text(str(claude_status["config_path"]), style="muted")))

    console.print()
    console.print(tui.fields(rows))
    if claude_status.get("action_needed"):
        console.print()
        tui.warn(str(claude_status["action_needed"]))
    console.print()


def _print_plans(proj: ProjectModel, project: str, output: str) -> None:
    """One project's plans, as a table or as json.

    Plans with a teammate's version waiting sort to the top. The pointer
    deliberately does not move for an arriving version, so without this the
    only sign that anything had arrived was a file appearing in the plan
    directory.
    """
    import json

    from .plan_ops import standing

    session = get_session()
    ordered = sorted(
        ((pf, standing(session, pf)) for pf in proj.plan_files),
        key=lambda pair: (pair[1].waiting is None, pair[0].name),
    )

    if output == "json":
        click.echo(
            json.dumps(
                [
                    {
                        "id": str(pf.id),
                        "name": pf.name,
                        "version": pf.current_version,
                        "owner": how.owner,
                        "waiting": how.waiting,
                        "updated_at": pf.updated_at.isoformat() if pf.updated_at else None,
                    }
                    for pf, how in ordered
                ]
            )
        )
        return

    if not proj.plan_files:
        console.print()
        tui.note(f"No plans in {project} yet. Your agents will fill this in.")
        console.print()
        return

    # The id column is gone: a uuid nobody types was eating a third of the
    # width and then being truncated anyway. The name is what every other
    # command takes as an argument.
    pending = [pf.name for pf, how in ordered if how.has_incoming]
    columns: list[Any] = ["Plan", ("Ver", {"justify": "right"}), "Owner", "Updated"]
    if pending:
        columns.append("Waiting")
    listing = tui.table(*columns)
    for pf, how in ordered:
        row = [
            Text(f"{pf.name}.md", style="value"),
            Text(f"v{pf.current_version}", style="muted"),
            Text(how.owner, style="muted"),
            Text(
                pf.updated_at.strftime("%Y-%m-%d %H:%M") if pf.updated_at else "never",
                style="muted",
            ),
        ]
        if pending:
            row.append(Text(f"v{how.waiting}", style="warn") if how.has_incoming else Text(""))
        listing.add_row(*row)
    console.print()
    console.print(listing)
    console.print()
    count = len(proj.plan_files)
    tui.note(f"{count} plan{'' if count == 1 else 's'} in {project}")
    if pending:
        tui.note(
            f"{len(pending)} with a version from a teammate waiting. "
            f"See it with: flanner history {pending[0]}"
        )
    console.print()


def _print_projects(projects: list[ProjectModel], output: str) -> None:
    """Every project on this device, as a table or as json."""
    import json

    if output == "json":
        click.echo(
            json.dumps(
                [
                    {
                        "id": str(p.id),
                        "name": p.name,
                        "plan_directory": p.plan_directory,
                        "plan_files": len(p.plan_files),
                        "created_at": p.created_at.isoformat() if p.created_at else None,
                    }
                    for p in projects
                ]
            )
        )
        return

    if not projects:
        console.print("No projects yet. Run 'flanner init' to create one.", style="yellow")
        return

    listing = tui.table("Project", ("Plans", {"justify": "right"}), "Plan directory", "Created")
    for p in projects:
        listing.add_row(
            Text(p.name, style="value"),
            Text(str(len(p.plan_files)), style="muted"),
            Text(p.plan_directory, style="code"),
            Text(p.created_at.strftime("%Y-%m-%d") if p.created_at else "never", style="muted"),
        )
    console.print()
    console.print(listing)
    console.print()


@cli.command("list")
@click.option("--project", default=None, help="Project name")
@click.option(
    "--output",
    type=click.Choice(["table", "json"]),
    default="table",
    help="Output format",
)
def list_cmd(project: str | None, output: str) -> None:
    """List all projects or plan files"""
    session = _require_session()
    if not project:
        _print_projects(db_list_projects(session), output)
        return

    proj = get_project_by_name(session, project)
    if not proj:
        console.print(f"ERROR Project '{project}' not found", style="red")
        raise SystemExit(1)
    _print_plans(proj, project, output)


@cli.command()
@click.argument("project_name")
@click.option("--project-root", default=None, help="New project root path")
@click.option("--plan-dir", default=None, help="New plan directory")
@click.option("--auto-gitignore", default=None, type=bool, help="Enable/disable auto .gitignore")
def config(
    project_name: str, project_root: str | None, plan_dir: str | None, auto_gitignore: bool | None
) -> None:
    """Configure a project's settings"""

    session = _require_session()

    # Get project
    project = get_project_by_name(session, project_name)
    if not project:
        console.print(f"ERROR Project '{project_name}' not found", style="red")
        raise SystemExit(1)

    # Use server tool to update
    from .server import configure_project_tool

    result = configure_project_tool(
        # str(): the tool expects a string UUID; passing the raw uuid.UUID crashed in UUID()
        project_id=str(project.id),
        project_root=project_root,
        plan_directory=plan_dir,
        auto_gitignore=auto_gitignore,
    )

    if result.get("error"):
        console.print(f"ERROR Error: {result['message']}", style="red")
    else:
        console.print(f"OK Project '{project_name}' updated", style="green")
        if plan_dir:
            console.print(f"  Plan directory: {plan_dir}", style="white")
        if auto_gitignore is not None:
            console.print(f"  Auto .gitignore: {auto_gitignore}", style="white")


@cli.command()
@click.argument("project_name")
def setup_gitignore(project_name: str) -> None:
    """Manually update .gitignore for a project"""

    session = _require_session()

    # Get project
    project = get_project_by_name(session, project_name)
    if not project:
        console.print(f"ERROR Project '{project_name}' not found", style="red")
        raise SystemExit(1)

    if not project.project_root:
        console.print("ERROR Project has no project_root configured", style="red")
        raise SystemExit(1)

    # Update .gitignore
    pattern = project.plan_directory.rstrip("/") + "/"
    updated = update_gitignore(project.project_root, pattern, comment="Flanner")

    if updated:
        console.print(f"OK Added '{pattern}' to .gitignore", style="green")
    else:
        console.print(f"  '{pattern}' already in .gitignore", style="yellow")


@cli.command()
@click.argument("project_name")
@click.option("--force", is_flag=True, help="Skip confirmation prompt")
def delete(project_name: str, force: bool) -> None:
    """Delete a project and all its plan files"""

    session = _require_session()

    # Get project
    project = get_project_by_name(session, project_name)
    if not project:
        console.print(f"ERROR Project '{project_name}' not found", style="red")
        raise SystemExit(1)

    # Show project info
    console.print("\nProject to delete:", style="yellow")
    console.print(f"  Name: {project.name}", style="white")
    console.print(f"  Root: {project.project_root}", style="white")
    console.print(f"  Plan files: {len(project.plan_files)}", style="white")

    # Confirm deletion
    if not force:
        if not click.confirm(
            "\nAre you sure you want to delete this project? This cannot be undone."
        ):
            console.print("Cancelled", style="yellow")
            return

    # Delete project (cascade deletes plan files and versions)
    project_root, plan_directory = project.project_root, project.plan_directory
    _write("delete_project", project_id=str(project.id))
    console.print(f"\nOK Project '{project_name}' deleted successfully", style="green")
    console.print(
        "  Note: Plan files on disk were NOT deleted. You may want to manually remove:",
        style="cyan",
    )
    if project_root:
        console.print(f"  {project_root}/{plan_directory}/", style="cyan")


def _port_in_use(host: str, port: int) -> bool:
    """True if binding (host, port) fails because something already holds it."""
    import socket

    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as sock:
        try:
            sock.bind((host, port))
        except OSError:
            return True
    return False


def _open_browser_when_ready(host: str, port: int) -> None:
    """Open the browser once the server accepts connections (background thread)."""
    import socket
    import threading
    import time
    import webbrowser

    target = "127.0.0.1" if host in ("0.0.0.0", "::", "") else host  # noqa: S104 (comparison, not a bind)

    def _wait_and_open() -> None:
        for _ in range(100):  # up to ~10s
            with socket.socket() as probe:
                if probe.connect_ex((target, port)) == 0:
                    break
            time.sleep(0.1)
        webbrowser.open(f"http://{target}:{port}")

    threading.Thread(target=_wait_and_open, daemon=True).start()


@cli.command()
@click.option(
    "--port",
    type=int,
    default=lambda: int(os.environ.get("FLANNER_WEB_PORT", "8080")),
    help="Web server port (env: FLANNER_WEB_PORT)",
)
@click.option("--host", default="127.0.0.1", help="Web server host")
@click.option("--open-browser", is_flag=True, help="Open browser automatically")
def web(port: int, host: str, open_browser: bool) -> None:
    """Launch web interface"""

    _require_store()

    from . import web as web_module

    if beyond_loopback(host):
        console.print(
            f"WARN Binding {host} exposes the web UI beyond localhost. It has no "
            "authentication; anyone who can reach this address can read and edit "
            "your plans. Use 127.0.0.1 unless you have put auth in front of it.",
            style="yellow",
        )
        # The Host header check stands down too. It exists to stop a domain
        # pointed at 127.0.0.1 reaching a local-only tool, and no list here
        # can predict which names will reach a deliberately exposed one. The
        # cross-site check on writes still runs.
        web_module.ALLOW_ANY_HOST = True

    if _port_in_use(host, port):
        console.print(f"ERROR Port {port} is already in use on {host}.", style="red")
        console.print("  Start on a different port, for example:", style="yellow")
        console.print(f"    flanner web --port {port + 1}", style="white")
        console.print("  Or set a default port for future runs:", style="yellow")
        console.print("    PowerShell:  $env:FLANNER_WEB_PORT = '8090'", style="white")
        console.print("    bash/zsh:    export FLANNER_WEB_PORT=8090", style="white")
        console.print(
            f"  (something may already be serving at http://{host}:{port})", style="white"
        )
        raise SystemExit(1)

    console.print("\nStarting Flanner Web Interface...\n", style="cyan bold")
    console.print(f"  Server:    http://{host}:{port}", style="green")
    console.print(f"  Dashboard: http://{host}:{port}/", style="green")
    console.print(f"  Projects:  http://{host}:{port}/projects", style="green")
    console.print("\n  Press CTRL+C to stop the server\n", style="yellow")

    # Open the browser once the server is actually accepting connections, on a
    # background thread so it never delays or blocks startup.
    if open_browser:
        _open_browser_when_ready(host, port)

    # Start web server. While it runs it is the local write daemon: advertise
    # it (port + token) so the stdio MCP server forwards writes here instead of
    # mutating shared state from a second process (PRD Phase 1).
    from . import ipc

    token = ipc.new_token()
    os.environ[ipc.TOKEN_ENV] = token
    ipc.write_daemon_info(port, token)
    try:
        import uvicorn

        from .web import app

        uvicorn.run(app, host=host, port=port, log_level="info")
    except KeyboardInterrupt:
        console.print("\n\nOK Web server stopped", style="green")
    except Exception as e:
        console.print(f"\nERROR Error starting web server: {e}", style="red")
    finally:
        ipc.clear_daemon_info()


@cli.command()
def setup() -> None:
    """Re-run the global agent registration on its own.

    `flanner init` already does this, so a new machine needs no separate
    step. This is for repairing it: an editor installed later, a config
    file restored from a backup, or a registration that failed the first
    time and was left with a warning.
    """
    _register_agents_globally()
    console.print(
        "\nRestart Claude Desktop and start a fresh Claude Code session to pick up the changes.",
        style="cyan",
    )


@cli.command()
@click.option("--force", is_flag=True, help="Force update if already registered")
@click.option("--type", "server_type", default="local", help="Server type: local or cloud")
@click.option("--url", default=None, help="Server URL (for cloud type)")
@click.option("--api-key", default=None, help="API key (for cloud type)")
def register(force: bool, server_type: str, url: str | None, api_key: str | None) -> None:
    """Register MCP server with Claude Code"""
    console.print("\n[MCP] Registering MCP server with Claude Code...\n", style="cyan")

    from .claude_integration import get_claude_config_path, register_mcp_server

    # Check if Claude Code config exists
    config_path = get_claude_config_path()
    if not config_path:
        console.print("ERROR Could not find Claude Code configuration path", style="red")
        console.print("  Please ensure Claude Code is installed", style="yellow")
        return

    # Validate cloud server parameters
    if server_type == "cloud" and not url:
        console.print("ERROR Cloud server requires --url parameter", style="red")
        raise SystemExit(1)

    # Register server
    success, message = register_mcp_server(
        server_type=server_type, server_url=url, api_key=api_key, force=force
    )

    if success:
        console.print(f"OK {message}", style="green")
        console.print(f"\nConfig location: {config_path}", style="white")

        if server_type == "local":
            project_dir = Path(__file__).resolve().parent.parent
            console.print(f"Project directory: {project_dir}", style="white")

        console.print("\nNext steps:", style="cyan")
        console.print("  1. Restart Claude Code to load the new MCP server", style="white")
        console.print(
            "  2. Check Claude Code's MCP settings to verify registration", style="white"
        )
        console.print(
            "  3. Test by asking Claude to list projects or create a plan", style="white"
        )
    else:
        console.print(f"ERROR {message}", style="red")


@cli.command()
def unregister() -> None:
    """Unregister MCP server from Claude Code"""
    console.print("\n[MCP] Unregistering MCP server from Claude Code...\n", style="cyan")

    from .claude_integration import unregister_mcp_server

    # Confirm
    if not click.confirm("Are you sure you want to unregister the MCP server?"):
        console.print("Cancelled", style="yellow")
        return

    success, message = unregister_mcp_server()

    if success:
        console.print(f"OK {message}", style="green")
        console.print("\n  Restart Claude Code for changes to take effect", style="yellow")
    else:
        console.print(f"ERROR {message}", style="red")


@cli.command()
def claude_info() -> None:
    """Show Claude Code integration information"""
    console.print()

    from .claude_integration import get_claude_config_info, registration_instructions

    info = get_claude_config_info()

    registered = (
        tui.dot("ok", label="registered")
        if info["server_registered"]
        else tui.dot("unknown", label="not registered")
    )
    console.print(
        tui.fields(
            [
                ("MCP server", registered),
                ("Config file", Text(str(info["config_path"]), style="value")),
                ("File exists", Text("yes" if info["config_exists"] else "no", style="muted")),
                ("MCP servers", Text(str(info["total_servers"]), style="muted")),
            ]
        )
    )

    if info["our_server_config"]:
        console.print()
        tui.note("Current configuration")
        import json

        console.print(json.dumps(info["our_server_config"], indent=2), style="white")
    else:
        console.print()
        tui.note("Not registered. Run flanner register to add it.")
        console.print(registration_instructions(), style="white")


class _Parsed(NamedTuple):
    """One plan file on disk, after its frontmatter has been read and checked."""

    path: Path
    plan_file_id: UUID
    plan_name: str
    version: int
    created_by: str
    body: str

    @property
    def name(self) -> str:
        return self.path.name


def _version_row(fm: _Parsed) -> Any:
    """The version row for an imported file.

    Built in one place because the update and create paths write an identical
    row, and a field that drifted between them would produce two versions of
    the same file that disagree about their own content hash.
    """
    from .database import VersionModel
    from .utils import hash_content, utcnow

    return VersionModel(
        plan_file_id=fm.plan_file_id,
        version=fm.version,
        file_path=str(fm.path),
        content_hash=hash_content(fm.body),
        created_by=fm.created_by,
        created_at=utcnow(),
        notes=f"Imported version {fm.version}",
    )


def _parse_for_sync(file_path: Path) -> _Parsed | str:
    """Read one file, or say why it cannot be imported.

    Returns the parsed file, or the outcome string to report for it.
    """
    from uuid import UUID

    from .frontmatter import parse_frontmatter, validate_frontmatter

    fm_data, body = parse_frontmatter(file_path.read_text(encoding="utf-8"))
    if not fm_data.get("mcp_plan_file"):
        console.print(f"  SKIP {file_path.name} - Not an MCP plan file", style="yellow")
        return "skipped"
    if not validate_frontmatter(fm_data):
        console.print(f"  ERROR {file_path.name} - Invalid frontmatter", style="red")
        return "error"

    return _Parsed(
        path=file_path,
        plan_file_id=UUID(fm_data["plan_file_id"]),
        plan_name=fm_data["plan_name"],
        version=fm_data["version"],
        created_by=fm_data.get("created_by", "unknown"),
        body=body,
    )


def _sync_update(session: Session, existing: Any, fm: _Parsed, dry_run: bool) -> str:
    """Add a newer version of a plan the catalog already knows about."""
    from .database import get_version
    from .utils import utcnow

    old_version = existing.current_version
    if fm.version <= old_version:
        console.print(
            f"  SKIP {fm.name} - Version {fm.version} already in database "
            f"(current: v{old_version})",
            style="white",
        )
        return "skipped"
    if dry_run:
        console.print(
            f"  WOULD UPDATE {fm.name} (plan: {fm.plan_name}, v{old_version} -> v{fm.version})",
            style="green",
        )
        return "imported"
    if get_version(session, fm.plan_file_id, fm.version):
        console.print(f"  SKIP {fm.name} - Version {fm.version} already exists", style="white")
        return "skipped"

    session.add(_version_row(fm))
    existing.current_version = fm.version
    existing.updated_at = utcnow()
    session.commit()
    console.print(
        f"  OK UPDATED {fm.name} (plan: {fm.plan_name}, v{old_version} -> v{fm.version})",
        style="green",
    )
    return "imported"


def _sync_create(session: Session, proj: ProjectModel, fm: _Parsed, dry_run: bool) -> str:
    """Record a plan this catalog has never seen, keeping the id from its file."""
    from .database import PlanFileModel
    from .utils import utcnow

    if dry_run:
        console.print(
            f"  WOULD IMPORT {fm.name} (plan: {fm.plan_name}, version: {fm.version})",
            style="green",
        )
        return "imported"

    # The uuid comes from the frontmatter rather than being generated, so a
    # file synced on two machines lands as one plan rather than two.
    session.add(
        PlanFileModel(
            id=fm.plan_file_id,
            project_id=proj.id,
            name=fm.plan_name,
            description=f"Imported from {fm.name}",
            current_version=fm.version,
            auto_version=True,
            created_at=utcnow(),
            updated_at=utcnow(),
        )
    )
    session.add(_version_row(fm))
    session.commit()
    console.print(
        f"  OK IMPORTED {fm.name} (plan: {fm.plan_name}, version: {fm.version})", style="green"
    )
    return "imported"


def _sync_file(session: Session, proj: ProjectModel, file_path: Path, dry_run: bool) -> str:
    """Import one plan file into the database. Returns 'imported', 'skipped', or 'error'."""
    from . import observe
    from .database import get_plan_file

    with observe.step(f"read {file_path.name}"):
        fm = _parse_for_sync(file_path)
    if isinstance(fm, str):
        observe.count(skipped=1)
        return fm
    observe.count(imported=1)

    existing = get_plan_file(session, fm.plan_file_id)
    if existing:
        return _sync_update(session, existing, fm, dry_run)
    return _sync_create(session, proj, fm, dry_run)


def _sync_project(
    session: Session, proj: ProjectModel, dry_run: bool, totals: dict[str, int]
) -> None:
    """Sync every plan file in one project's plan directory."""
    console.print(f"\nProject: {proj.name}", style="cyan bold")
    console.print(f"Plan directory: {proj.project_root}/{proj.plan_directory}", style="white")

    if not proj.project_root:
        # Previously crashed with TypeError; skip the misconfigured project instead
        console.print("  Project has no project_root configured", style="yellow")
        return

    plan_dir = Path(proj.project_root) / proj.plan_directory
    if not plan_dir.exists():
        console.print("  Plan directory doesn't exist yet", style="yellow")
        return

    md_files = sorted(plan_dir.rglob("*.md"))  # recurse: plan names may be subpaths
    if not md_files:
        console.print("  No plan files found", style="yellow")
        return

    console.print(f"  Found {len(md_files)} file(s)\n", style="white")
    for file_path in md_files:
        totals["scanned"] += 1
        try:
            outcome = _sync_file(session, proj, file_path, dry_run)
        except Exception as e:
            session.rollback()
            console.print(f"  ERROR {file_path.name} - {e}", style="red")
            outcome = "error"
        totals[outcome] += 1


@cli.command()
@click.option(
    "--project", default=None, help="Project name to sync (syncs all projects if not specified)"
)
@click.option(
    "--dry-run", is_flag=True, help="Show what would be imported without actually importing"
)
def sync(project: str | None, dry_run: bool) -> None:
    """Scan .plans directory and import existing plan files into database"""
    console.print()

    if dry_run:
        tui.note("Dry run. Nothing will be written.")

    session = _require_session()
    init_storage(str(get_mcp_dir()))

    if project:
        project_model = get_project_by_name(session, project)
        if not project_model:
            console.print(f"ERROR Project '{project}' not found", style="red")
            raise SystemExit(1)
        projects = [project_model]
    else:
        projects = db_list_projects(session)

    if not projects:
        console.print(
            "No projects found. Create a project first with 'flanner init'", style="yellow"
        )
        return

    totals = {"scanned": 0, "imported": 0, "skipped": 0, "error": 0}
    for proj in projects:
        _sync_project(session, proj, dry_run, totals)

    # Only the counts that actually happened. A row of zeroes buries the one
    # number worth reading, which is usually "imported".
    summary = Text()
    summary.append(f"{totals['scanned']} scanned", style="value")
    for label, key, style in (
        ("imported", "imported", "ok"),
        ("skipped", "skipped", "muted"),
        ("errors", "error", "bad"),
    ):
        if totals[key]:
            summary.append(f"  {tui.MIDDOT}  ", style="muted")
            summary.append(f"{totals[key]} {label}", style=style)
    console.print()
    console.print(summary)
    console.print()

    if dry_run and totals["imported"] > 0:
        tui.note("Nothing written. Run flanner sync to apply.")


_REVIEW_STYLES = {
    "open": "cyan",
    "accepted": "green",
    "superseded": "blue",
    "rejected": "red",
    "changes_requested": "yellow",
    "withdrawn": "dim",
    "stale": "dark_orange",
}


def _note_authorization(authorization: Any) -> None:
    """Say which regime a review answer came from, wherever one is given.

    Both the status view and the moment of deciding need this, and they must
    not word it differently: somebody who saw one and then the other would
    reasonably read the difference as meaning something.

    The reason comes from the resolution rather than being restated here, so
    there is one sentence to keep true instead of two.
    """
    if not authorization.enforced:
        console.print(f"review here is advisory: {authorization.reason}", style="dim")
    elif not authorization.roles:
        console.print(f"WARN cannot authorize review: {authorization.reason}", style="yellow")


@cli.group()
def review() -> None:
    """Propose plans for review and record decisions"""


def _resolve_plan(
    session: Session, project: str | None, plan_name: str
) -> tuple[ProjectModel, Any]:
    """Find a project and one of its plans, or exit 1 explaining which failed."""
    proj = _resolve_project_or_cwd(session, project)
    if not proj:
        _no_project(project)
    plan_file = next((p for p in proj.plan_files if p.name == plan_name), None)
    if plan_file is None:
        console.print(f"ERROR Plan '{plan_name}' not found in '{proj.name}'", style="red")
        # The names, not just the failure. A plan is addressed by name, so
        # the usual cause is a typo or a half-remembered one, and the list is
        # short enough to print.
        if proj.plan_files:
            console.print(
                f"  Available plans: {', '.join(p.name for p in proj.plan_files)}", style="yellow"
            )
        raise SystemExit(1)
    return proj, plan_file


@review.command("propose")
@click.argument("plan_name")
@click.option("--project", default=None, help="Project name")
@click.option("--message", default="", help="Note for reviewers")
@click.option("--actor", default=None, help="Who is proposing (defaults to your entitlement)")
def review_propose(plan_name: str, project: str | None, message: str, actor: str | None) -> None:
    """Offer a plan's newest version for review"""
    from .review import propose

    session = _require_session()
    proj, plan_file = _resolve_plan(session, project, plan_name)
    try:
        result = propose(session, project=proj, plan_file=plan_file, message=message, actor=actor)
    except (ValueError, PermissionError) as e:
        console.print(f"ERROR {e}", style="red")
        raise SystemExit(1) from None

    console.print(f"\nOK Proposed '{plan_name}' for review", style="green")
    console.print(f"  proposal: {result.event.event_id}", style="cyan")
    console.print(f"  version:  {result.event.payload['target_artifact_id']}", style="dim")


@review.command("decide")
@click.argument("plan_name")
@click.argument(
    "decision", type=click.Choice(["approve", "reject", "request_changes", "withdraw"])
)
@click.option("--proposal", default=None, help="Proposal id (defaults to the only open one)")
@click.option("--project", default=None, help="Project name")
@click.option("--actor", default=None, help="Who is deciding (defaults to your entitlement)")
def review_decide(
    plan_name: str, decision: str, proposal: str | None, project: str | None, actor: str | None
) -> None:
    """Approve, reject, request changes on, or withdraw a proposal"""
    from . import authz
    from .review import decide, status

    session = _require_session()
    proj, plan_file = _resolve_plan(session, project, plan_name)

    if proposal is None:
        open_ones = [
            view
            for view in status(session, plan_file=plan_file, project=proj).proposals.values()
            if view.state in ("open", "stale", "changes_requested")
        ]
        if len(open_ones) != 1:
            console.print(
                f"ERROR {len(open_ones)} proposals are open; name one with --proposal.",
                style="red",
            )
            raise SystemExit(1)
        proposal = open_ones[0].proposal_id

    try:
        result = decide(
            session,
            project=proj,
            plan_file=plan_file,
            proposal_id=proposal,
            action=decision,
            actor=actor,
        )
    except ValueError as e:
        console.print(f"ERROR {e}", style="red")
        raise SystemExit(1) from None

    console.print(f"\nOK Recorded {decision} on '{plan_name}'", style="green")
    if result.advanced_baseline:
        console.print("  the accepted baseline now points at this version", style="green")
    else:
        console.print(f"  baseline unchanged: {result.reason}", style="yellow")

    # Said here and not only in `review status`, because this is the moment
    # that reads as an authorization. Somebody can approve without ever
    # having run status, and "Recorded approve" on its own does not
    # distinguish a decision that binds from one that is a rehearsal.
    _note_authorization(authz.resolve(proj))


def _print_comments(session: Session, plan_file: Any) -> None:
    """Notes teammates left, with whether each still finds its text."""
    from .anchors import AMBIGUOUS, STRANDED, Anchor, resolve
    from .assurance import load_comments
    from .database import get_version
    from .storage import load_plan_file

    notes = load_comments(session, str(plan_file.id))
    if not notes:
        return

    current = get_version(session, plan_file.id, None)
    body = ""
    if current is not None:
        try:
            _, body = load_plan_file(current.file_path)
        except (FileNotFoundError, OSError):
            body = ""

    console.print()
    heading = Text()
    heading.append(f"{len(notes)} comment{'' if len(notes) == 1 else 's'}", style="value")
    console.print(heading)
    console.print()
    listing = tui.table(
        "By", ("On", {"overflow": "fold"}), ("Note", {"overflow": "fold"}), "Anchor"
    )
    # A table rather than a chain of elifs: every branch answered the same
    # question and only the words differed, and "anchored" is the default
    # because a status this version does not name is a working anchor.
    marks = {
        STRANDED: ("lost its place", "bad"),
        AMBIGUOUS: ("several matches", "warn"),
        "moved": ("text changed", "warn"),
    }
    for event in notes:
        payload = event.payload
        anchor_data = payload.get("anchor") or {}
        state = resolve(Anchor.from_dict(anchor_data), body) if body else None
        if state is None:
            mark = Text("unknown", style="muted")
        else:
            label, style = marks.get(state.status, ("anchored", "ok"))
            mark = Text(label, style=style)
        listing.add_row(
            Text(str(event.actor or "unknown"), style="muted"),
            Text(str(anchor_data.get("quote") or "")[:40], style="muted"),
            Text(str(payload.get("body") or ""), style="value"),
            mark,
        )
    console.print(listing)
    console.print()


def _print_external_review(session: Session, plan_file: Any) -> None:
    """Notes imported from outside, kept apart from the proposals.

    Separate because they did not come from a device this team can verify,
    and must not read as though they had.
    """
    from .assurance import load_external_reviews

    imported = load_external_reviews(session, str(plan_file.id))
    if not imported:
        return
    total = sum(len(e.payload.get("notes") or []) for e in imported)
    console.print()
    heading = Text()
    heading.append(f"{total} note{'' if total == 1 else 's'} from outside", style="value")
    heading.append("   unverified", style="warn")
    console.print(heading)
    console.print()
    outside = tui.table("From", ("On", {"overflow": "fold"}), ("Note", {"overflow": "fold"}))
    for event in imported:
        who = str(event.payload.get("reviewer") or "unnamed")
        for note in event.payload.get("notes") or []:
            outside.add_row(
                Text(who, style="muted"),
                Text(str(note.get("quote", ""))[:38], style="muted"),
                Text(str(note.get("body", "")), style="value"),
            )
    console.print(outside)
    console.print()


@review.command("status")
@click.argument("plan_name")
@click.option("--project", default=None, help="Project name")
def review_status(plan_name: str, project: str | None) -> None:
    """Show a plan's proposals and its accepted baseline"""
    from . import authz
    from .review import status

    session = _require_session()
    proj, plan_file = _resolve_plan(session, project, plan_name)
    state = status(session, plan_file=plan_file, project=proj)

    _note_authorization(authz.resolve(proj))

    if state.conflicted:
        console.print(
            "WARN the accepted baseline is contested; merge before implementing", style="red"
        )
    elif state.accepted_artifact_id:
        console.print(f"accepted: {state.accepted_artifact_id}", style="green")
    else:
        console.print("accepted: nothing approved yet", style="yellow")

    if not state.proposals:
        console.print("\nNo proposals recorded.", style="dim")
        # Outside review can exist with no proposal at all, and is the
        # whole point of having sent a packet, so it is not skipped here.
        _print_comments(session, plan_file)
        _print_external_review(session, plan_file)
        return

    table = tui.table("Proposal", "State", "Proposer", "Approvals")
    for view in state.proposals.values():
        style = _REVIEW_STYLES.get(view.state, "white")
        table.add_row(
            view.proposal_id[:19] + "...",
            f"[{style}]{view.state}[/{style}]",
            view.proposer,
            ", ".join(view.approvals) or "--",
        )
    console.print(table)

    _print_comments(session, plan_file)
    _print_external_review(session, plan_file)


def _memory_findings(session: Any) -> list[Any]:
    """Where the memory catalog and the memory files disagree.

    Returned as the same `Finding` the plan reconciler produces, so the
    doctor's table, its json and its exit code need no second shape. Memory
    findings are reported, never repaired: a file somebody edited by hand
    is that person's memory, and `flanner mem rebuild` is the deliberate
    way to accept it.
    """
    from . import database as database_module
    from . import memory_ops
    from .database import get_memory
    from .reconcile import Finding

    findings: list[Any] = []

    if not database_module.SEARCH_INDEX_AVAILABLE:
        findings.append(
            Finding(
                kind="search_index_unavailable",
                plan="(memory)",
                detail=(
                    "this Python's SQLite has no FTS5, so memory search falls back "
                    "to a slower scan with no ranking"
                ),
            )
        )

    try:
        drifted = memory_ops.drift(session)
    except Exception as e:  # noqa: BLE001 - a doctor that crashes diagnoses nothing
        return [*findings, Finding(kind="mem_unreadable_file", plan="(memory)", detail=str(e))]

    for kind, memory_id, detail in drifted:
        memory = get_memory(session, memory_id)
        findings.append(
            Finding(
                kind=kind,
                plan=memory.title[:48] if memory else str(memory_id),
                detail=detail,
                path=detail if kind != "mem_unreadable_file" else None,
            )
        )
    return findings


_FINDING_STYLES = {
    "missing_file": "red",
    "hash_mismatch": "yellow",
    "unreadable_file": "red",
    "orphan_file": "cyan",
    "unknown_plan": "yellow",
    "stale_current_version": "cyan",
    "no_project_root": "red",
    "signature_invalid": "red",
    "artifact_missing": "red",
    "unverified_signer": "blue",
    # Memory. Same shape and the same colours, because a person reading
    # this table should not have to learn which half of the product a row
    # came from before knowing how worried to be.
    "mem_missing_file": "red",
    "mem_unreadable_file": "red",
    "mem_hash_mismatch": "yellow",
    "search_index_unavailable": "yellow",
}


@dataclass(frozen=True)
class EnrollmentCheck:
    """One statement about where this device and project stand with a team.

    Separate from a reconcile finding because the two answer different
    questions. A finding is about a plan file; this is about whether team
    features can work here at all, which is the question somebody actually
    has when review or sync is not behaving.
    """

    code: str
    level: str  # ok | info | action | problem
    detail: str
    fix: str = ""


_ENROLLMENT_STYLES = {"ok": "green", "info": "dim", "action": "yellow", "problem": "red"}


def _clock_check(endpoint: str) -> list[EnrollmentCheck]:
    """Compare this machine's clock against the control plane's.

    Peers refuse each other's requests once their clocks are further apart
    than `device_auth.MAX_SKEW`, and the refusal arrives mid-sync with a
    number rather than a cause. This turns it into a line read during setup.

    The control plane is used as the reference because it is the one clock
    both devices already agree to talk to, and its `Date` header comes free
    with a request the device makes anyway. Silent when the network is
    unavailable: a diagnostic that fails because a laptop is on a train
    should say nothing, not raise an alarm.
    """
    import email.utils
    import urllib.error
    import urllib.request
    from datetime import datetime, timezone

    from .device_auth import MAX_SKEW

    url = endpoint.rstrip("/") + "/health"
    if not url.startswith(("http://", "https://")):
        return []
    try:
        request = urllib.request.Request(url, method="HEAD")  # noqa: S310 - scheme checked above
        with urllib.request.urlopen(request, timeout=5) as response:  # noqa: S310 - scheme checked before the call
            served = response.headers.get("Date")
    except (urllib.error.URLError, OSError, ValueError):
        return []
    if not served:
        return []

    try:
        theirs = email.utils.parsedate_to_datetime(served)
    except (TypeError, ValueError):
        return []
    if theirs.tzinfo is None:
        theirs = theirs.replace(tzinfo=timezone.utc)

    drift = abs((datetime.now(timezone.utc) - theirs).total_seconds())
    allowed = MAX_SKEW.total_seconds()
    if drift <= allowed / 2:
        return []
    level = "problem" if drift > allowed else "action"
    return [
        EnrollmentCheck(
            "clock_drift",
            level,
            f"This machine's clock is {int(drift)}s from the server's. Peers refuse "
            f"each other past {int(allowed)}s, so syncing will fail.",
            "w32tm /resync /force   # Windows, as administrator",
        )
    ]


def _entitlement_check(verdict: Any) -> EnrollmentCheck:
    """Where the entitlement stands, and what that permits.

    Grace is deliberately its own state rather than a flavour of failure:
    reads keep working, pushes do not, and telling somebody "expired" when
    they can still pull would send them chasing the wrong problem.
    """
    from . import entitlements

    # Against the module constant, not a literal. The first version compared
    # with "VALID" while the constant is "valid", so a healthy entitlement was
    # reported as being in grace — a doctor that lies about the thing it
    # exists to check.
    if verdict.status == entitlements.VALID:
        return EnrollmentCheck("entitlement_valid", "ok", "Entitlement is current.")
    if verdict.usable:
        return EnrollmentCheck(
            "entitlement_grace",
            "action",
            f"Entitlement is in grace: {verdict.reason or 'not renewed recently'}. "
            "Reads still work; pushing is refused until it renews.",
            "flanner whoami --refresh",
        )
    return EnrollmentCheck(
        "entitlement_expired",
        "problem",
        f"Entitlement is {verdict.status}: {verdict.reason or 'no longer valid'}. "
        "Team features are off until it renews.",
        "flanner whoami --refresh",
    )


def _binding_check(bound: str | None, granted: dict[str, str]) -> EnrollmentCheck:
    """Whether this repository is joined to a workspace the account may enter.

    Four outcomes, and the last is the one worth having: a project bound to a
    workspace the account cannot enter is invisible everywhere else. `whoami`
    lists the grants, `join` reports the binding, and neither notices that the
    two disagree.
    """
    if not granted:
        return EnrollmentCheck(
            "no_grants",
            "action",
            "No workspace access granted yet. An admin has to grant it, and it "
            "arrives when the entitlement next renews.",
            "flanner whoami --refresh",
        )
    listed = ", ".join(sorted(granted))
    if not bound:
        return EnrollmentCheck(
            "not_bound",
            "action",
            "This project is not bound to a workspace, so review here does not "
            f"count for the team. You may enter: {listed}.",
            "flanner join <workspace-id>",
        )
    if bound in granted:
        return EnrollmentCheck("bound", "ok", f"Bound to {bound} as {granted[bound]}.")
    return EnrollmentCheck(
        "bound_without_grant",
        "problem",
        f"Bound to workspace {bound}, which this account may not enter. "
        f"Access covers: {listed}. Either an admin revoked it, or the id is wrong.",
        "flanner join <workspace-id>  # or --clear to unbind",
    )


def _enrollment_report(project: Any) -> list[EnrollmentCheck]:
    """Where this device stands: enrolled, entitled, granted, and bound.

    Four separate things, in the order they gate each other. Reporting them
    apart matters because the failures look identical from the outside — a
    push that does nothing is the same silence whether the device was never
    enrolled, the entitlement lapsed, an admin has not granted a workspace
    yet, or this repository was never joined to one.

    The last check is the one worth having. A project bound to a workspace
    the account may not enter is invisible in every other command: `whoami`
    lists the grants, `join` reports the binding, and neither notices that
    they disagree.
    """
    from . import session as cache

    bound = getattr(project, "workspace_id", None)
    current = cache.load()

    if current is None:
        checks = [
            EnrollmentCheck(
                "not_enrolled",
                "info",
                "Not enrolled with a team. Local plan work needs no account.",
                "flanner accept <token> --as your-handle",
            )
        ]
        if bound:
            checks.append(
                EnrollmentCheck(
                    "bound_without_account",
                    "problem",
                    f"This project is bound to workspace {bound}, but the device is not "
                    "enrolled, so review here counts for nobody.",
                    "flanner accept <token> --as your-handle",
                )
            )
        return checks

    checks = [
        EnrollmentCheck(
            "enrolled",
            "ok",
            f"Enrolled as {current.user_id} in {current.organization_id}.",
        )
    ]
    checks.extend(_clock_check(current.endpoint))

    verdict = current.status()
    checks.append(_entitlement_check(verdict))

    capabilities = verdict.claims.workspace_capabilities if verdict.claims else ()
    granted = {c.workspace_id: c.role for c in capabilities}
    checks.append(_binding_check(bound, granted))
    return checks


def _print_report() -> None:
    """A summary somebody can paste into an issue without reading it first.

    Bug reports arrive as "it did not work", and the conversation that
    follows is six questions any of which this could have answered. What is
    here is what a maintainer asks for: versions, platform, how the store is
    configured, and how big it is.

    **Scrubbed, because this one is pasted.** A local log holding a project
    name is fine; a paste into a public issue is not. So paths are reported
    by shape rather than by value — that the store exists and how large it
    is, never where somebody's employer's repository lives on disk. Plan
    names and contents never appear at all.
    """
    import platform
    import sys as _sys

    from sqlalchemy.exc import SQLAlchemyError

    from . import __version__

    lines = [
        f"flanner       {__version__}",
        f"python        {_sys.version.split()[0]} ({platform.python_implementation()})",
        f"platform      {platform.system()} {platform.machine()}",
    ]

    store = Path(get_mcp_dir()) / "data.db"
    if store.exists():
        lines.append(f"store         present, {store.stat().st_size // 1024} KB")
    else:
        lines.append("store         not initialized")

    try:
        # Opened here rather than assumed: this command deliberately does not
        # go through `_require_session`, because a report is most wanted
        # exactly when the store will not open, and refusing to print one
        # then would be the worst possible time to refuse.
        if store.exists():
            init_database(str(store))
        projects = db_list_projects(get_session())
        plans = sum(len(p.plan_files) for p in projects)
        lines.append(f"catalog       {len(projects)} project(s), {plans} plan(s)")
    except (FlannerError, SQLAlchemyError) as e:
        lines.append(f"catalog       unreadable: {type(e).__name__}")

    # Whether the mesh transport installed, not whether it worked. A
    # platform without a wheel is the single most common cause of "peer
    # commands do nothing" and is invisible from the error message.
    try:
        import importlib.util

        has_iroh = importlib.util.find_spec("iroh") is not None
    except (ImportError, ValueError):  # pragma: no cover - defensive
        has_iroh = False
    lines.append(f"mesh          {'iroh present' if has_iroh else 'iroh absent (address-only)'}")

    log = Path(get_mcp_dir()) / "mcp.log"
    if log.exists():
        recent = [
            line for line in log.read_text(encoding="utf-8").splitlines() if "failed" in line
        ]
        lines.append(f"mcp failures  {len(recent)} in the current log")

    console.print()
    console.print("Paste this into the issue:", style="dim")
    console.print()
    for line in lines:
        console.print(f"  {line}")
    console.print()
    tui.note("No paths, plan names, or plan contents are included.")
    console.print()


def _doctor_json(project_name: str, findings: list[Any], enrollment: list[Any]) -> None:
    """The machine-readable report.

    An object, not the bare array this used to print. The array could only
    ever describe plan files, and "this device is not enrolled" is not a
    plan file. A caller reading the enrollment state should not have to
    filter it out of a list of missing-file findings.
    """
    import json

    click.echo(
        json.dumps(
            {
                "project": project_name,
                "catalog": [
                    {
                        "kind": f.kind,
                        "plan": f.plan,
                        "detail": f.detail,
                        "path": f.path,
                        "repairable": f.repairable,
                    }
                    for f in findings
                ],
                "enrollment": [
                    {"code": c.code, "level": c.level, "detail": c.detail, "fix": c.fix}
                    for c in enrollment
                ],
            },
            indent=2,
        )
    )


def _print_doctor_advice(findings: list[Any], *, repair: bool) -> None:
    """What the findings mean and what to do next.

    Findings that are only informational are counted separately, because a
    report made entirely of "could not verify this on this device" is a
    clean bill of health and must not read as a list of problems.
    """
    unchecked = [f for f in findings if f.informational]
    if unchecked and len(unchecked) == len(findings):
        console.print(
            f"\nNo problems found. {len(unchecked)} item(s) could not be verified on "
            "this device; the note above says why.",
            style="green",
        )
        return

    if repair:
        fixed = sum(1 for f in findings if f.repairable)
        console.print(f"\nRepaired {fixed} of {len(findings)} findings.", style="green")
        remaining = [f for f in findings if not f.repairable and not f.informational]
        if remaining:
            console.print(
                f"{len(remaining)} need a human: files are missing or were edited outside "
                "flanner, so no automatic fix is safe.",
                style="yellow",
            )
    elif any(f.repairable for f in findings):
        console.print(
            "\nRun 'flanner doctor --repair' to fix the repairable ones.", style="yellow"
        )


@cli.command()
@click.option("--project", default=None, help="Project name")
@click.option("--repair", is_flag=True, help="Adopt orphan files and fix stale version counters")
@click.option(
    "--output",
    type=click.Choice(["table", "json"]),
    default="table",
    help="Output format",
)
@click.option(
    "--report",
    is_flag=True,
    help="Print a scrubbed summary to paste into a bug report",
)
def doctor(project: str | None, repair: bool, output: str, report: bool) -> None:
    """Check the catalog against the plan files on disk"""
    from .reconcile import reconcile_project

    if report:
        _print_report()
        return

    session = _require_session()
    proj = _resolve_project_or_cwd(session, project)
    if not proj:
        _no_project(project)

    from . import observe

    with observe.step("reconcile catalog"):
        findings = reconcile_project(session, proj, repair=repair)
    with observe.step("check memory"):
        findings += _memory_findings(session)
    with observe.step("check enrollment"):
        enrollment = _enrollment_report(proj)
    observe.count(findings=len(findings))

    if output == "json":
        _doctor_json(proj.name, findings, enrollment)
        return

    if not findings:
        console.print(
            f"OK Catalog, files, and signatures all agree for '{proj.name}'", style="green"
        )
        _print_enrollment(enrollment)
        return

    table = tui.table("Issue", "Plan", "Detail")
    for finding in findings:
        style = _FINDING_STYLES.get(finding.kind, "white")
        table.add_row(f"[{style}]{finding.kind}[/{style}]", finding.plan, finding.detail)
    console.print(table)
    _print_doctor_advice(findings, repair=repair)
    _print_enrollment(enrollment)


def _print_enrollment(checks: list[EnrollmentCheck]) -> None:
    """The team half of the report, printed whichever way the catalog went.

    Always printed, including when everything is fine. A check that only
    appears on failure cannot be used to confirm success, and confirming
    success is most of what somebody wants after running four setup commands.
    """
    console.print("\nTeam")
    for check in checks:
        style = _ENROLLMENT_STYLES.get(check.level, "white")
        console.print(f"  {check.detail}", style=style)
        if check.fix:
            console.print(f"    {tui.command(check.fix)}", style="dim")


_FRESHNESS_STYLES = {"fresh": "green", "aging": "yellow", "suspect": "dark_orange", "stale": "red"}


@cli.command()
@click.argument("plan_name", required=False)
@click.option("--project", default=None, help="Project name")
@click.option(
    "--output",
    type=click.Choice(["table", "json"]),
    default="table",
    help="Output format",
)
def freshness(plan_name: str | None, project: str | None, output: str) -> None:
    """Freshness status for plans, with the evidence behind it"""
    import json as json_module

    from .database import list_plan_files as db_list_plan_files

    session = _require_session()
    proj = _resolve_project_or_cwd(session, project)
    if not proj:
        _no_project(project)

    plans = db_list_plan_files(session, proj.id)
    if plan_name:
        plans = [p for p in plans if p.name == plan_name]
        if not plans:
            console.print(f"ERROR Plan '{plan_name}' not found in '{proj.name}'", style="red")
            raise SystemExit(1)
    if not plans:
        console.print(f"No plan files found for project '{proj.name}'", style="yellow")
        return
    if not proj.project_root:
        console.print(f"ERROR Project '{proj.name}' has no project_root configured", style="red")
        raise SystemExit(1)

    results = _freshness_results(session, proj, plans)

    if output == "json":
        click.echo(
            json_module.dumps(
                [{"plan": p.name, "version": v.version, **e} for p, v, e in results],
                indent=2,
            )
        )
        return

    # One plan named: the full case for the verdict, which is what somebody
    # asking about a single plan wants. The table is for scanning.
    if plan_name and len(results) == 1:
        _print_freshness_detail(*results[0])
        return

    _print_freshness_table(results)


def _freshness_results(
    session: Session, proj: ProjectModel, plans: list[Any]
) -> list[tuple[Any, Any, dict[str, Any]]]:
    """Judge each plan's newest version against the repository.

    A plan whose file is gone is reported as stale rather than skipped. It is
    the most stale a plan can be, and dropping it from the list would make the
    worst case invisible in the summary.
    """
    from .database import get_version
    from .freshness import compute_freshness
    from .storage import load_plan_file

    results: list[tuple[Any, Any, dict[str, Any]]] = []
    for plan in plans:
        version_obj = get_version(session, plan.id, None)
        if not version_obj:
            continue
        try:
            _, body = load_plan_file(version_obj.file_path)
        except FileNotFoundError:
            results.append(
                (plan, version_obj, {"status": "stale", "reasons": ["plan file missing on disk"]})
            )
            continue
        evidence = compute_freshness(proj.project_root or "", body, version_obj.created_at)
        results.append((plan, version_obj, evidence))
    return results


# Label and evidence key, in the order a reader wants them: what it was
# anchored to, how far the code has moved since, then the citations.
_FRESHNESS_DETAIL = (
    ("Anchor", "anchored_at_commit"),
    ("Commits since", "commits_since_anchor"),
    ("Age (days)", "age_days"),
    ("Dead refs", "invalid_refs"),
    ("Cited paths", "referenced_paths"),
    ("Cited symbols", "referenced_symbols"),
)


def _print_freshness_detail(plan: Any, version_obj: Any, evidence: dict[str, Any]) -> None:
    """The whole case for one plan's verdict, evidence included."""
    console.print()
    headline = Text()
    headline.append_text(tui.dot(evidence["status"]))
    headline.append("  ")
    headline.append(f"{plan.name}.md", style="value")
    headline.append(f"  v{version_obj.version}", style="muted")
    console.print(headline)
    console.print()

    for reason in evidence["reasons"]:
        tui.bad(reason) if evidence["status"] == "stale" else tui.warn(reason)
    if not evidence["reasons"]:
        tui.ok("nothing has drifted since this was written")

    rows = []
    for label, key in _FRESHNESS_DETAIL:
        got = evidence.get(key)
        if got in (None, [], ""):
            continue
        shown = ", ".join(str(x) for x in got) if isinstance(got, list) else str(got)
        rows.append((label, Text(shown, style="code")))
    if rows:
        console.print()
        console.print(tui.fields(rows))
    console.print()


def _print_freshness_table(results: list[tuple[Any, Any, dict[str, Any]]]) -> None:
    """Every plan at a glance, and a pointer at the worst one.

    The summary names an example rather than only counting, because "3 stale"
    leaves somebody to find which three.
    """
    listing = tui.table(
        "Plan",
        ("Ver", {"justify": "right"}),
        "Status",
        ("Evidence", {"overflow": "fold"}),
    )
    counts: dict[str, int] = {}
    for plan, version_obj, evidence in results:
        status = evidence["status"]
        counts[status] = counts.get(status, 0) + 1
        listing.add_row(
            Text(f"{plan.name}.md", style="value"),
            Text(f"v{version_obj.version}", style="muted"),
            tui.dot(status),
            Text(evidence["reasons"][0] if evidence["reasons"] else "", style="muted"),
        )
    console.print()
    console.print(listing)
    console.print()

    summary = tui.tally(counts)
    worst = next((s for s in reversed(tui.FRESHNESS_ORDER) if counts.get(s)), None)
    if worst and worst != "fresh":
        example = next(p.name for p, _, e in results if e["status"] == worst)
        summary.append(f"  {tui.DASH} run ", style="muted")
        summary.append(f"flanner freshness {example}", style="accent")
        summary.append(" for the full evidence", style="muted")
    console.print(summary)
    console.print()


@cli.group()
def jira() -> None:
    """JIRA integration commands"""
    pass


@jira.command("config")
@click.argument("project_name")
@click.option("--url", required=True, help="JIRA base URL (e.g., https://company.atlassian.net)")
@click.option("--project-key", default=None, help="Default JIRA project key (e.g., PROJ)")
def jira_config(project_name: str, url: str, project_key: str | None) -> None:
    """Configure JIRA integration for a project"""
    from .jira_utils import is_valid_jira_url, normalize_jira_url

    _require_store()

    # Validate JIRA URL
    if not is_valid_jira_url(url):
        console.print(f"ERROR Invalid JIRA URL format: {url}", style="red")
        console.print("  Expected format: https://company.atlassian.net", style="yellow")
        raise SystemExit(1)

    session = _require_session()

    # Get project
    project = get_project_by_name(session, project_name)
    if not project:
        console.print(f"ERROR Project '{project_name}' not found", style="red")
        raise SystemExit(1)

    # Create or update JIRA config
    try:
        normalized_url = normalize_jira_url(url)
        result = _write(
            "configure_jira",
            project_id=str(project.id),
            jira_url=normalized_url,
            jira_project_key=project_key,
        )

        console.print(
            f"\nOK JIRA configuration updated for project '{project_name}'", style="green"
        )
        console.print(f"  JIRA URL: {result['jira_url']}", style="white")
        if result.get("jira_project_key"):
            console.print(f"  Default Project Key: {result['jira_project_key']}", style="white")
    except Exception as e:
        console.print(f"ERROR Failed to configure JIRA: {e}", style="red")


@jira.command("link")
@click.argument("plan_name")
@click.option("--issue", required=True, help="JIRA issue key (e.g., PROJ-123)")
@click.option("--type", "issue_type", default=None, help="Issue type (Epic, Story, Task, etc.)")
@click.option("--notes", default=None, help="Notes about the link")
@click.option(
    "--project", default=None, help="Project name (uses current directory if not specified)"
)
def jira_link(
    plan_name: str, issue: str, issue_type: str | None, notes: str | None, project: str | None
) -> None:
    """Link a plan file to a JIRA issue"""
    from .database import get_jira_config
    from .jira_utils import format_jira_issue_key, generate_jira_issue_url, is_valid_jira_issue_key

    _require_store()

    # Validate issue key
    formatted_issue = format_jira_issue_key(issue)
    if not is_valid_jira_issue_key(formatted_issue):
        console.print(f"ERROR Invalid JIRA issue key format: {issue}", style="red")
        console.print(
            "  Expected format: PROJECT-123 (uppercase letters, dash, numbers)", style="yellow"
        )
        raise SystemExit(1)

    session = _require_session()

    proj, plan_file = _resolve_plan(session, project, plan_name)

    # Create link
    try:
        _write(
            "link_plan_to_jira",
            plan_file_id=str(plan_file.id),
            jira_issue_key=formatted_issue,
            issue_type=issue_type,
            notes=notes,
            created_by="user",
        )

        console.print(f"\nOK Linked '{plan_name}' to {formatted_issue}", style="green")

        # Show URL if JIRA config exists
        jira_config = get_jira_config(session, proj.id)
        if jira_config:
            url = generate_jira_issue_url(jira_config.jira_url, formatted_issue)
            console.print(f"  URL: {url}", style="cyan")

        if issue_type:
            console.print(f"  Type: {issue_type}", style="white")
        if notes:
            console.print(f"  Notes: {notes}", style="white")

    except ValueError as e:
        console.print(f"ERROR {e}", style="red")
    except Exception as e:
        console.print(f"ERROR Failed to create link: {e}", style="red")


@jira.command("unlink")
@click.argument("plan_name")
@click.option(
    "--issue", default=None, help="JIRA issue key to unlink (unlinks all if not specified)"
)
@click.option("--all", "unlink_all", is_flag=True, help="Unlink all JIRA issues")
@click.option("--project", default=None, help="Project name")
def jira_unlink(plan_name: str, issue: str | None, unlink_all: bool, project: str | None) -> None:
    """Unlink a plan file from JIRA issue(s)"""
    from .jira_utils import format_jira_issue_key

    session = _require_session()

    proj, plan_file = _resolve_plan(session, project, plan_name)

    # Unlink. A missing link is a warning here, not a failure, so these go
    # through dispatch directly rather than the exit-on-error helper.
    from .services import dispatch

    try:
        if unlink_all or not issue:
            result = dispatch("unlink_jira_issue", {"plan_file_id": str(plan_file.id)})
            if result.get("error"):
                console.print(f"ERROR {result['message']}", style="red")
                raise SystemExit(1)
            count = result.get("count", 0)
            if count > 0:
                console.print(
                    f"\nOK Unlinked {count} JIRA issue(s) from '{plan_name}'", style="green"
                )
            else:
                console.print(f"\n No JIRA links found for '{plan_name}'", style="yellow")
        else:
            formatted_issue = format_jira_issue_key(issue)
            result = dispatch(
                "unlink_jira_issue",
                {"plan_file_id": str(plan_file.id), "jira_issue_key": formatted_issue},
            )
            if result.get("success"):
                console.print(f"\nOK Unlinked '{plan_name}' from {formatted_issue}", style="green")
            else:
                console.print(f"\nERROR Link to {formatted_issue} not found", style="yellow")

    except Exception as e:
        console.print(f"ERROR Failed to unlink: {e}", style="red")


@jira.command("links")
@click.option("--project", default=None, help="Project name (shows all projects if not specified)")
def jira_links(project: str | None) -> None:
    """List all JIRA links"""
    from .database import get_jira_config, list_all_jira_links

    session = _require_session()

    # Get projects
    if project:
        proj = get_project_by_name(session, project)
        if not proj:
            console.print(f"ERROR Project '{project}' not found", style="red")
            raise SystemExit(1)
        projects = [proj]
    else:
        projects = db_list_projects(session)

    if not projects:
        console.print("No projects found", style="yellow")
        return

    for proj in projects:
        links = list_all_jira_links(session, proj.id)

        if not links:
            if len(projects) == 1:
                console.print(f"\nNo JIRA links found for project '{proj.name}'", style="yellow")
            continue

        console.print(f"\n{proj.name}:", style="cyan bold")

        table = tui.table("Plan File", "JIRA Issue", "Type", "Created")

        # Get JIRA config for URL generation
        get_jira_config(session, proj.id)

        for link in links:
            issue_key = link["jira_issue_key"]
            table.add_row(
                link["plan_file_name"],
                issue_key,
                link["jira_issue_type"] or "--",
                link["created_at"].strftime("%Y-%m-%d") if link["created_at"] else "N/A",
            )

        console.print(table)


@jira.command("show")
@click.argument("plan_name")
@click.option("--project", default=None, help="Project name")
def jira_show(plan_name: str, project: str | None) -> None:
    """Show detailed JIRA links for a plan file"""
    from .database import get_jira_config, get_jira_links
    from .jira_utils import generate_jira_issue_url

    session = _require_session()

    proj, plan_file = _resolve_plan(session, project, plan_name)

    # Get links
    links = get_jira_links(session, plan_file.id)

    if not links:
        console.print(f"\nNo JIRA links found for '{plan_name}'", style="yellow")
        return

    console.print(f"\nPlan: {plan_name}", style="cyan bold")
    console.print("JIRA Links:\n", style="white")

    # Get JIRA config
    jira_config = get_jira_config(session, proj.id)

    for link in links:
        console.print(f"  - {link.jira_issue_key}", style="green")
        if link.jira_issue_type:
            console.print(f"    Type: {link.jira_issue_type}", style="white")

        if jira_config:
            url = generate_jira_issue_url(jira_config.jira_url, link.jira_issue_key)
            console.print(f"    URL: {url}", style="cyan")

        if link.notes:
            console.print(f"    Notes: {link.notes}", style="white")

        linked_at = link.created_at.strftime("%Y-%m-%d %H:%M") if link.created_at else "N/A"
        console.print(
            f"    Linked: {linked_at} by {link.created_by}",
            style="dim",
        )
        console.print()


def _write(op: str, **args: Any) -> dict[str, Any]:
    """Run one write operation through the shared service layer.

    Routes to the local daemon when one is running, so the CLI cannot mutate
    shared state behind its back (PRD Phase 1 single-writer discipline), and
    executes in-process otherwise. Reports the operation's own message and
    exits 1 on failure, so every CLI write fails the same way.
    """
    from .services import dispatch

    result = dispatch(op, args)
    if result.get("error"):
        console.print(f"ERROR {result['message']}", style="red")
        raise SystemExit(1)
    return result


def _ensure_store() -> None:
    """Create the machine-wide store if it is not there yet.

    Idempotent. `init` does this too, along with adopting a repository and
    registering the MCP server; this is only the part every command needs.
    """
    mcp_dir = get_mcp_dir()
    init_storage(str(mcp_dir))
    init_database(str(mcp_dir / "data.db"))


def _require_store() -> None:
    """Refuse, in one voice, when this machine has no store yet.

    Eleven commands wrote this refusal out themselves, so improving it
    meant improving it eleven times, and the wording had already drifted
    from what `init` actually does.
    """
    if (get_mcp_dir() / "data.db").exists():
        return
    # Naming the command was not enough. People reach this by following our
    # own instructions, so it says what the command does and that hitting
    # it once is expected.
    tui.bad("This machine has no flanner store yet.")
    tui.note("`flanner init` creates it, and adopts the repository you run it in.")
    tui.hint(f"  {tui.command('flanner init')}")
    raise SystemExit(1)


def _open_store() -> None:
    """Make the local catalog usable in this process, or refuse.

    Separate from `_require_session` for the commands that hand `get_session`
    to something else — a background thread, or a server that opens one per
    request — rather than opening one here. `peer serve` did neither and so
    initialised nothing, which its own catch-up thread then discovered.
    """
    _require_store()
    init_database(str(get_mcp_dir() / "data.db"))


def _session_failed(error: Any) -> NoReturn:
    """Report a refusal from the control plane, and say what to do about it.

    This is what the refusal codes bought. The message was always the
    server's sentence, which describes what happened; the next step depends
    on which thing happened, and until there was a code the only way to pick
    one was to match on that sentence.

    Advice lives here rather than on the server on purpose: what to do about
    a lapsed subscription is different for the person who pays and the person
    who does not, and the client is the end that knows which it is talking to.
    """
    from flanner import refusals

    console.print(f"ERROR {error}", style="red")

    advice = {
        refusals.CLOCK_SKEW: (
            "This machine's clock disagrees with the server. Sync it and try "
            "again; see docs/clock-skew.md if it will not sync."
        ),
        refusals.SUBSCRIPTION_INACTIVE: (
            "The team's subscription does not cover this. An admin can fix it from the console."
        ),
        refusals.NOT_ADMIN: "This needs an organization admin. Ask one to do it.",
        refusals.DEVICE_UNKNOWN: (
            "This device is not enrolled, or was revoked. Run 'flanner login' "
            "with a fresh enrolment code."
        ),
        refusals.CODE_UNUSABLE: (
            "That code is unknown, already used, or expired. Ask for a new one."
        ),
        refusals.THROTTLED: "Too many attempts. The message above says how long to wait.",
        refusals.UPSTREAM_UNAVAILABLE: (
            "Something the control plane needs is down. Try again shortly."
        ),
        refusals.NOT_CONFIGURED: "This deployment has not enabled that feature.",
    }.get(getattr(error, "code", refusals.UNKNOWN))

    if advice:
        tui.hint(advice)
    elif getattr(error, "retryable", False):
        tui.hint("This one is worth trying again.")

    raise SystemExit(1)


def _require_session() -> Session:
    """Open the flanner database, or refuse if there is not one yet."""
    _open_store()
    return get_session()


def _no_project(project: str | None) -> NoReturn:
    """Explain which lookup failed, then exit.

    Telling somebody to "pass --project" when they just passed --project is
    the kind of message that makes a tool feel like it is not listening. The
    name they gave is the useful thing to echo back.

    Three different situations used to share one message. Standing in a
    repository that has simply never been adopted is by far the most common,
    and it was being told to "run this from inside a project" — advice to go
    somewhere else, when the answer is to adopt where you already are. It is
    what somebody following the join sequence meets if they reach for
    `flanner join` before `flanner init`.
    """
    if project:
        console.print(f"ERROR No project named '{project}'.", style="red")
        tui.hint("Run flanner list to see the projects this machine knows about.")
        raise SystemExit(1)

    git_root = find_git_root(os.getcwd())
    if git_root:
        console.print("ERROR This repository has not been adopted by flanner yet.", style="red")
        tui.note(f"Found a git repository at {git_root}, but no project for it.")
        tui.hint(f"  {tui.command('flanner init')}   adopt it, then run this again")
    else:
        console.print("ERROR Not inside a git repository.", style="red")
        tui.note("flanner works per repository, and finds one by looking for its git root.")
        tui.hint("Change to a repository first, or name a project with --project.")
    raise SystemExit(1)


def _resolve_project_or_cwd(session: Session, project: str | None) -> ProjectModel | None:
    """Find a project by name, or by the git root of the current directory."""
    if project:
        return get_project_by_name(session, project)
    from .database import get_project_by_root

    git_root = find_git_root(os.getcwd())
    return get_project_by_root(session, git_root) if git_root else None


@cli.group()
def linear() -> None:
    """Linear integration commands"""
    pass


@linear.command("config")
@click.argument("project_name")
@click.option("--workspace", required=True, help="Linear workspace slug or URL (e.g. acme)")
def linear_config(project_name: str, workspace: str) -> None:
    """Configure Linear integration for a project"""
    from .linear_utils import is_valid_linear_workspace, normalize_linear_workspace

    if not is_valid_linear_workspace(workspace):
        console.print(f"ERROR Invalid Linear workspace: {workspace}", style="red")
        console.print("  Expected a slug like 'acme' or a linear.app URL", style="yellow")
        raise SystemExit(1)

    session = _require_session()
    proj = get_project_by_name(session, project_name)
    if not proj:
        console.print(f"ERROR Project '{project_name}' not found", style="red")
        raise SystemExit(1)

    try:
        slug = normalize_linear_workspace(workspace)
        result = _write("configure_linear", project_id=str(proj.id), workspace=slug)
        console.print(
            f"\nOK Linear configuration updated for project '{project_name}'", style="green"
        )
        console.print(f"  Workspace: {result['workspace']}", style="white")
    except Exception as e:
        console.print(f"ERROR Failed to configure Linear: {e}", style="red")


@linear.command("auth")
def linear_auth() -> None:
    """Verify LINEAR_API_KEY and print the MCP server config snippet.

    Reads the key from the environment only (never a flag, never stored), checks
    it against Linear, and shows the config block to give the AI agent the same
    access.
    """
    import json

    from .claude_integration import get_local_server_config
    from .exceptions import LinearError
    from .linear_api import fetch_viewer, get_api_key

    api_key = get_api_key()
    if not api_key:
        console.print("ERROR LINEAR_API_KEY is not set", style="red")
        console.print(
            "  Create a personal API key at https://linear.app/settings/api, then set it:",
            style="yellow",
        )
        console.print('  PowerShell:  setx LINEAR_API_KEY "lin_api_..."', style="white")
        console.print("  bash/zsh:    export LINEAR_API_KEY=lin_api_...", style="white")
        raise SystemExit(1)

    try:
        viewer = fetch_viewer(api_key)
    except LinearError as e:
        console.print(f"ERROR Linear rejected the key: {e}", style="red")
        raise SystemExit(1) from e

    who = viewer.get("name") or "unknown user"
    email = viewer.get("email")
    console.print(f"\nOK Authenticated with Linear as {who}", style="green")
    if email:
        console.print(f"  {email}", style="white")

    console.print(
        "\nYour terminal is ready. To give the AI agent (MCP server) the same access,",
        style="white",
    )
    console.print(
        "add LINEAR_API_KEY to its env in your Claude Code MCP settings:\n", style="white"
    )
    config = get_local_server_config()
    config["env"] = {"LINEAR_API_KEY": "lin_api_...  (paste your key)"}
    console.print(json.dumps({"mcpServers": {"flanner": config}}, indent=2), style="yellow")


@linear.command("link")
@click.argument("plan_name")
@click.option("--issue", required=True, help="Linear issue id (e.g., ENG-123)")
@click.option("--notes", default=None, help="Notes about the link")
@click.option("--project", default=None, help="Project name (uses current directory if omitted)")
@click.option("--no-verify", "no_verify", is_flag=True, help="Skip Linear API verification")
@click.option("--attach-url", default=None, help="URL to attach to the Linear issue")
def linear_link_cmd(
    plan_name: str,
    issue: str,
    notes: str | None,
    project: str | None,
    no_verify: bool,
    attach_url: str | None,
) -> None:
    """Link a plan file to a Linear issue.

    With LINEAR_API_KEY set, the issue is verified and its title/state cached
    (unless --no-verify). A missing issue aborts; a network error links anyway.
    """
    from .linear_utils import (
        format_linear_issue_id,
        is_valid_linear_issue_id,
    )

    issue_id = format_linear_issue_id(issue)
    if not is_valid_linear_issue_id(issue_id):
        console.print(f"ERROR Invalid Linear issue id: {issue}", style="red")
        console.print("  Expected format: ENG-123 (team key, dash, number)", style="yellow")
        raise SystemExit(1)

    session = _require_session()
    proj = _resolve_project_or_cwd(session, project)
    if not proj:
        console.print(
            "ERROR Project not found. Specify --project or run from project directory", style="red"
        )
        raise SystemExit(1)

    plan_file = next((pf for pf in proj.plan_files if pf.name == plan_name), None)
    if not plan_file:
        console.print(f"ERROR Plan '{plan_name}' not found in project '{proj.name}'", style="red")
        raise SystemExit(1)

    # Issue verification, URL attachment, and the write all happen in the
    # shared service, so the CLI and the MCP tool cannot drift apart; it
    # reports back whatever it managed to observe.
    result = _write(
        "link_plan_to_linear",
        plan_file_id=str(plan_file.id),
        linear_issue_id=issue_id,
        notes=notes,
        verify=not no_verify,
        attach_url=attach_url,
        created_by="user",
    )

    if result.get("warning"):
        console.print(f"  WARN {result['warning']}", style="yellow")
    elif attach_url and result.get("issue_title"):
        console.print(f"  Attached {attach_url} to {issue_id}", style="white")

    console.print(f"\nOK Linked '{plan_name}' to {issue_id}", style="green")
    if result.get("linear_url"):
        console.print(f"  URL: {result['linear_url']}", style="cyan")
    if result.get("issue_title"):
        console.print(f"  Issue: [{result['issue_state']}] {result['issue_title']}", style="white")
    if notes:
        console.print(f"  Notes: {notes}", style="white")


@linear.command("unlink")
@click.argument("plan_name")
@click.option("--issue", default=None, help="Issue id to unlink (unlinks all if omitted)")
@click.option("--all", "unlink_all", is_flag=True, help="Unlink all Linear issues")
@click.option("--project", default=None, help="Project name")
def linear_unlink(
    plan_name: str, issue: str | None, unlink_all: bool, project: str | None
) -> None:
    """Unlink a plan file from Linear issue(s)"""
    from .linear_utils import format_linear_issue_id

    session = _require_session()
    proj = _resolve_project_or_cwd(session, project)
    if not proj:
        console.print("ERROR Project not found", style="red")
        raise SystemExit(1)

    plan_file = next((pf for pf in proj.plan_files if pf.name == plan_name), None)
    if not plan_file:
        console.print(f"ERROR Plan '{plan_name}' not found", style="red")
        raise SystemExit(1)

    # As with jira unlink, a missing link is a warning rather than a failure,
    # so this uses dispatch directly instead of the exit-on-error helper.
    from .services import dispatch

    try:
        if unlink_all or not issue:
            result = dispatch("unlink_linear_issue", {"plan_file_id": str(plan_file.id)})
            if result.get("error"):
                console.print(f"ERROR {result['message']}", style="red")
                raise SystemExit(1)
            count = result.get("count", 0)
            if count > 0:
                console.print(
                    f"\nOK Unlinked {count} Linear issue(s) from '{plan_name}'", style="green"
                )
            else:
                console.print(f"\n No Linear links found for '{plan_name}'", style="yellow")
        else:
            issue_id = format_linear_issue_id(issue)
            result = dispatch(
                "unlink_linear_issue",
                {"plan_file_id": str(plan_file.id), "linear_issue_id": issue_id},
            )
            if result.get("success"):
                console.print(f"\nOK Unlinked '{plan_name}' from {issue_id}", style="green")
            else:
                console.print(f"\nERROR Link to {issue_id} not found", style="yellow")
    except Exception as e:
        console.print(f"ERROR Failed to unlink: {e}", style="red")


@linear.command("links")
@click.option("--project", default=None, help="Project name (shows all projects if omitted)")
def linear_links(project: str | None) -> None:
    """List all Linear links"""
    from .database import list_all_linear_links

    session = _require_session()
    if project:
        proj = get_project_by_name(session, project)
        if not proj:
            console.print(f"ERROR Project '{project}' not found", style="red")
            raise SystemExit(1)
        projects = [proj]
    else:
        projects = db_list_projects(session)

    if not projects:
        console.print("No projects found", style="yellow")
        return

    for proj in projects:
        links = list_all_linear_links(session, proj.id)
        if not links:
            if len(projects) == 1:
                console.print(f"\nNo Linear links found for project '{proj.name}'", style="yellow")
            continue

        console.print(f"\n{proj.name}:", style="cyan bold")
        table = tui.table("Plan File", "Linear Issue", "State", "Created")
        for link in links:
            table.add_row(
                link["plan_file_name"],
                link["linear_issue_id"],
                link["issue_state"] or "--",
                link["created_at"].strftime("%Y-%m-%d") if link["created_at"] else "N/A",
            )
        console.print(table)


@linear.command("show")
@click.argument("plan_name")
@click.option("--project", default=None, help="Project name")
def linear_show(plan_name: str, project: str | None) -> None:
    """Show detailed Linear links for a plan file"""
    from .database import get_linear_config, get_linear_links
    from .linear_utils import generate_linear_issue_url

    session = _require_session()
    proj = _resolve_project_or_cwd(session, project)
    if not proj:
        console.print("ERROR Project not found", style="red")
        raise SystemExit(1)

    plan_file = next((pf for pf in proj.plan_files if pf.name == plan_name), None)
    if not plan_file:
        console.print(f"ERROR Plan '{plan_name}' not found", style="red")
        raise SystemExit(1)

    links = get_linear_links(session, plan_file.id)
    if not links:
        console.print(f"\nNo Linear links found for '{plan_name}'", style="yellow")
        return

    console.print(f"\nPlan: {plan_name}", style="cyan bold")
    console.print("Linear Links:\n", style="white")
    config = get_linear_config(session, proj.id)
    for link in links:
        console.print(f"  - {link.linear_issue_id}", style="green")
        if link.issue_state or link.issue_title:
            console.print(
                f"    Issue: [{link.issue_state or '?'}] {link.issue_title or ''}", style="white"
            )
        if config:
            url = generate_linear_issue_url(config.workspace, link.linear_issue_id)
            console.print(f"    URL: {url}", style="cyan")
        if link.notes:
            console.print(f"    Notes: {link.notes}", style="white")


@linear.command("refresh")
@click.argument("plan_name")
@click.option("--project", default=None, help="Project name")
def linear_refresh(plan_name: str, project: str | None) -> None:
    """Re-fetch title/state from Linear for a plan's links (needs LINEAR_API_KEY)"""
    from .database import get_linear_links, update_linear_link_cache
    from .exceptions import LinearError
    from .linear_api import fetch_issue_by_identifier, get_api_key

    api_key = get_api_key()
    if not api_key:
        console.print("ERROR LINEAR_API_KEY is not set; nothing to refresh", style="red")
        raise SystemExit(1)

    session = _require_session()
    proj = _resolve_project_or_cwd(session, project)
    if not proj:
        console.print("ERROR Project not found", style="red")
        raise SystemExit(1)

    plan_file = next((pf for pf in proj.plan_files if pf.name == plan_name), None)
    if not plan_file:
        console.print(f"ERROR Plan '{plan_name}' not found", style="red")
        raise SystemExit(1)

    links = get_linear_links(session, plan_file.id)
    if not links:
        console.print(f"\nNo Linear links found for '{plan_name}'", style="yellow")
        return

    console.print(f"\nRefreshing {len(links)} link(s) for '{plan_name}':", style="cyan")
    for link in links:
        try:
            fetched = fetch_issue_by_identifier(link.linear_issue_id, api_key)
            if fetched is None:
                console.print(f"  {link.linear_issue_id}: not found", style="yellow")
                continue
            update_linear_link_cache(session, link.id, fetched["title"], fetched["state"])
            console.print(
                f"  {link.linear_issue_id}: [{fetched['state']}] {fetched['title']}", style="green"
            )
        except LinearError as e:
            console.print(f"  {link.linear_issue_id}: {e}", style="red")


if __name__ == "__main__":
    cli()


# --- account -----------------------------------------------------------------
# Team features need a signed entitlement; local plan work never does. These
# commands are the only ones in the CLI that talk to the control plane.

_ENTITLEMENT_STYLE = {
    "valid": "green",
    "in_grace": "yellow",
    "expired": "red",
    "untrusted_key": "red",
    "bad_signature": "red",
    "malformed": "dim",
}


@cli.command()
@click.argument("code")
@click.option("--endpoint", default=None, help="Control plane URL (defaults to Flanner Mesh)")
@click.option("--label", default=None, help="Name for this device (defaults to the hostname)")
def login(code: str, endpoint: str | None, label: str | None) -> None:
    """Enroll this device with an enrollment code from your team console"""
    from . import account
    from . import session as session_cache

    try:
        current = account.login(
            code, endpoint=endpoint or session_cache.DEFAULT_ENDPOINT, label=label
        )
    except account.SessionError as e:
        _session_failed(e)

    # Same reason `accept` does it: this is the moment the machine commits to
    # being used with a team, and the next thing printed is `flanner join`,
    # which refuses when nothing has made a database yet. The two commands
    # enrol a device identically, so leaving only one of them to create the
    # store made the same instruction work or fail depending on which one you
    # had been sent to.
    _ensure_store()

    console.print(f"OK Enrolled as {current.user_id} ({current.device_id})", style="green")
    _what_next(current)
    _print_entitlement(current)


@cli.command()
def logout() -> None:
    """Forget this device's session (the device keeps its identity)"""
    from . import session as account

    if account.clear():
        console.print("OK Signed out on this device", style="green")
        console.print(
            "This device is still enrolled. Revoke it from the team console to end its access.",
            style="dim",
        )
    else:
        console.print("Not signed in on this device", style="dim")


@cli.command()
@click.option("--refresh", "do_refresh", is_flag=True, help="Renew the entitlement first")
@click.option(
    "--output",
    type=click.Choice(["table", "json"]),
    default="table",
    help="Output format",
)
def whoami(do_refresh: bool, output: str) -> None:
    """Show this device's identity and what it is currently entitled to

    `--output json` is the machine-readable form. This is the command a
    setup script asks "am I actually set up?", and parsing the table for an
    answer means parsing prose that exists to be read by a person.
    """
    import json as json_module

    from . import account
    from . import identity as device
    from . import session as cache

    if output == "table":
        console.print(f"Device  {device.device_id()}")
        _print_store()

    current: cache.Session | None
    if do_refresh:
        # An explicit --refresh is a request, not a heuristic. ensure_fresh
        # would skip the call while the held entitlement is still valid,
        # which is exactly when someone runs this to pick up a new grant.
        try:
            current = account.refresh()
        except account.SessionError as e:
            console.print(f"WARN could not renew: {e}", style="yellow")
            current = cache.load()

        # The device directory too, not just the entitlement. Six error
        # messages across the CLI and the transport recommend this command
        # as the fix for "cannot resolve that device", and until now it
        # renewed the entitlement and deliberately preserved the stale — and
        # on a fresh install, empty — key cache. So the recommended recovery
        # could not recover the thing it was recommended for.
        try:
            learned = account.fetch_device_keys()
            current = cache.load() or current
            console.print(f"Peers   {len(learned)} device key(s) known", style="dim")
        except account.SessionError as e:
            console.print(f"WARN could not refresh device keys: {e}", style="yellow")
    else:
        current = cache.load()
    if output == "json":
        click.echo(json_module.dumps(_whoami_report(device.device_id(), current), indent=2))
        return

    if current is None:
        console.print("Account not signed in", style="dim")
        console.print("Local plan work needs no account. Run 'flanner login' to join a team.")
        return

    console.print(f"Account {current.user_id} in {current.organization_id}")
    console.print(f"Server  {current.endpoint}")
    _print_entitlement(current)


def _whoami_report(device_id: str, current: Any) -> dict[str, Any]:
    """The same facts the table shows, in a shape a script can branch on.

    `signed_in` is stated rather than left to be inferred from a null, so a
    caller does not have to decide whether a missing account means "local
    only" or "something went wrong reading it".
    """
    if current is None:
        return {"device_id": device_id, "signed_in": False}

    verdict = current.status()
    capabilities = verdict.claims.workspace_capabilities if verdict.claims else ()
    return {
        "device_id": device_id,
        "signed_in": True,
        "user_id": current.user_id,
        "organization_id": current.organization_id,
        "endpoint": current.endpoint,
        "entitlement": {
            "status": verdict.status,
            "usable": verdict.usable,
            "reason": verdict.reason or None,
            "expires_at": verdict.claims.expires_at if verdict.claims else None,
        },
        "workspaces": [{"workspace_id": c.workspace_id, "role": c.role} for c in capabilities],
    }


def _print_store() -> None:
    """What this device holds, and the fact that it never sheds it.

    The Settings page in the web UI has said this since retirement landed.
    The CLI had not, and a CLI-only user is the common case, so the decision
    to keep everything was invisible to the people living with it.

    Skipped rather than reported as zero when there is no database. A fresh
    install holding nothing is a different claim from a store that has been
    measured, and "0 B" would read as the second.
    """
    from .database import list_artifacts

    db_path = get_mcp_dir() / "data.db"
    if not db_path.exists():
        return

    init_database(str(db_path))
    rows = list_artifacts(get_session())
    held = sum(len(row.payload or "") for row in rows)

    console.print(f"Holds   {len(rows)} artifacts, {tui.size(held)}")
    console.print(
        "        never pruned; retiring a plan hides it and erases nothing",
        style="dim",
    )


def _print_entitlement(current: Any) -> None:
    """Report what the held entitlement allows, and where it stands."""
    verdict = current.status()
    style = _ENTITLEMENT_STYLE.get(verdict.status, "dim")
    console.print(f"Access  {verdict.status}", style=style)
    if verdict.reason:
        console.print(f"        {verdict.reason}", style=style)
    if verdict.claims is None:
        return

    console.print(f"Expires {verdict.claims.expires_at}")
    capabilities = verdict.claims.workspace_capabilities
    if not capabilities:
        console.print("No workspace access granted yet", style="dim")
        return
    table = tui.table("Workspace", "Role")
    for capability in capabilities:
        table.add_row(capability.workspace_id, capability.role)
    console.print(table)


def _report_adoption(report: Any) -> None:
    """What joining did to the plans that were already here.

    Says explicitly that earlier history stays local. Somebody who joins a
    workspace and sees their plans appear will assume the versions came too,
    and finding out later that they did not is worse than being told now.
    """
    if report.moved:
        console.print(
            f"Brought {report.moved} plan(s) into the workspace: " + ", ".join(report.adopted),
            style="green",
        )
        console.print(
            "      Their current content syncs from now on. Earlier history"
            " stays on this machine, because it was signed for a workspace"
            " nobody else can verify.",
            style="dim",
        )
    if report.already_there:
        console.print(f"already in this workspace: {len(report.already_there)}", style="dim")
    for name, why in report.skipped:
        console.print(f"skipped {name}: {why}", style="dim")


def _report_access(proj: ProjectModel) -> None:
    """What this device may now do in the workspace it just joined.

    Joining binds the project; access is granted per person, so a successful
    join with no access yet is normal and has to read that way rather than
    as a failure.
    """
    from . import authz

    authorization = authz.resolve(proj)
    if authorization.roles:
        console.print(f"You hold: {authorization.roles[authorization.actor]}", style="green")
    else:
        console.print(f"No access yet: {authorization.reason}", style="yellow")


@cli.command()
@click.argument("workspace_id", required=False)
@click.option("--project", default=None, help="Project name (uses current directory if omitted)")
@click.option("--clear", "clear_binding", is_flag=True, help="Leave the workspace")
@click.option(
    "--no-adopt",
    is_flag=True,
    help="Do not bring existing plans into the workspace",
)
def join(
    workspace_id: str | None, project: str | None, clear_binding: bool, no_adopt: bool
) -> None:
    """Bind a project to a control-plane workspace, making review binding

    Run `flanner init` first in a repository flanner has not seen before:
    joining binds an existing project, and does not create one.

    Until a project joins one, review runs but authorizes nothing. After it
    joins, roles come from the signed entitlement this device holds.

    Deliberately not exposed over MCP: joining or leaving a workspace changes
    who may approve a plan, which is not a decision an agent should make on
    the user's behalf.
    """
    if not workspace_id and not clear_binding:
        tui.bad("Give a workspace id, or --clear to leave.")
        # The id is not guessable and nothing prints it by accident, so a
        # refusal that does not name where to find one leaves somebody
        # searching a console they may not have access to.
        _print_workspaces_hint()
        raise SystemExit(1)

    session = _require_session()
    proj = _resolve_project_or_cwd(session, project)
    if not proj:
        _no_project(project)

    if clear_binding:
        proj.workspace_id = None
        session.commit()
        console.print(f"OK '{proj.name}' left its workspace", style="green")
        console.print("Review still runs here, but it authorizes nothing.", style="dim")
        return

    from .plan_ops import adopt_into_workspace

    # The guard at the top requires one of a workspace id or --clear, and
    # --clear has returned by now. Repeated as a real check rather than an
    # assertion, which `python -O` would strip.
    if not workspace_id:
        console.print("ERROR Give a workspace id.", style="red")
        raise SystemExit(1)

    # Access is checked before anything is written. Joining used to bind,
    # commit, re-sign every plan into the workspace as a new root, and only
    # then mention that this device holds no role there — so a mistyped id
    # cost a repository its plans' history in a workspace nobody can reach.
    # The check needs a project bound to the target, which is what the
    # unsaved probe is; the real one is not touched until it passes.
    from . import authz
    from .database import ProjectModel

    probe = authz.resolve(ProjectModel(name=proj.name, workspace_id=workspace_id))
    if not probe.roles:
        console.print(f"ERROR No access to {workspace_id}: {probe.reason}", style="red")
        console.print("Nothing was changed.", style="dim")
        console.print(
            "If you were invited to it just now, renew first:  flanner whoami --refresh",
            style="dim",
        )
        _print_workspaces_hint()
        raise SystemExit(1)

    proj.workspace_id = workspace_id
    session.commit()
    console.print(f"OK '{proj.name}' joined workspace {workspace_id}", style="green")

    if no_adopt:
        console.print("Existing plans stay local and will not sync, as asked.", style="yellow")
        console.print(
            "      Run 'flanner join' again without --no-adopt to bring them across.",
            style="dim",
        )
        return

    # A workspace id is inside the signed envelope, so joining cannot move
    # what was written before it. Each plan's current content is signed
    # afresh into the workspace instead, as a root there.
    _report_adoption(adopt_into_workspace(session, project=proj, workspace_id=workspace_id))
    _report_access(proj)


def _what_next(current: Any) -> None:
    """After enrolling, say what to do next.

    `login` printed one line and stopped. Enrolling is the middle of a
    setup, not the end of one: the device now has an identity and no
    project, and the next move differs depending on whether anybody has
    granted this account a workspace yet. Somebody setting up a team for
    the first time is exactly who has least idea what to type.
    """
    claims = current.status().claims
    grants = tuple(getattr(claims, "workspace_capabilities", ()) or ()) if claims else ()

    console.print()
    if not grants:
        tui.note("No workspace access yet, which is normal on a new account.")
        tui.hint("Create a workspace in the console, or ask an admin for access, then:")
        tui.hint(f"  {tui.command('flanner whoami --refresh')}   pick up the grant")
        console.print()
        return

    where = ", ".join(sorted(f"{g.workspace_id} ({g.role})" for g in grants))
    tui.note(f"You hold: {where}")
    tui.hint("In the repository whose plans should sync:")
    tui.hint(f"  {tui.command('flanner init')}                    adopt it")
    tui.hint(f"  {tui.command('flanner join <workspace-id>')}     bind it to the team")
    tui.hint(f"  {tui.command('flanner peer serve')}              answer teammates")
    console.print()


def _print_workspaces_hint() -> None:
    """Name the workspaces this device may enter, or say why there are none.

    Three surfaces already hold this — `whoami`, the local Mesh page and the
    console — and the one place somebody is standing when they need it
    listed none of them.
    """
    from . import session as cache

    current = cache.load()
    if current is None:
        tui.note("This device is not signed in, so it holds no workspace access.")
        tui.hint(f"Run {tui.command('flanner login <code>')} with an invitation first.")
        return

    claims = current.status().claims
    grants = getattr(claims, "workspace_capabilities", ()) if claims else ()
    if not grants:
        tui.note("Your account has no workspace access yet.")
        tui.hint("An admin grants it from the console, then run")
        tui.hint(f"  {tui.command('flanner whoami --refresh')} to pick it up.")
        return

    tui.note("Workspaces this device may enter:")
    table = tui.table("Workspace", "Role")
    for grant in grants:
        table.add_row(grant.workspace_id, grant.role)
    console.print(table)
    tui.hint(f"Also shown by {tui.command('flanner whoami')} and on the Mesh page.")


@cli.command()
@click.argument("token")
@click.option("--as", "user_id", required=True, help="The user id to join as")
@click.option("--endpoint", default=None, help="Control plane URL (defaults to Flanner Mesh)")
@click.option("--label", default=None, help="Name for this device (defaults to the hostname)")
def accept(token: str, user_id: str, endpoint: str | None, label: str | None) -> None:
    """Accept an invitation, joining a team and enrolling this device"""
    from . import account
    from . import session as session_cache

    try:
        current = account.accept_invitation(
            token,
            user_id=user_id,
            endpoint=endpoint or session_cache.DEFAULT_ENDPOINT,
            label=label,
        )
    except account.SessionError as e:
        _session_failed(e)

    # The store is machine-wide, and accepting an invitation is the moment
    # this machine commits to being used with a team. Creating it here
    # removes the failure everybody hit: the next instruction we print is
    # `flanner join`, and until now that refused because nothing had made a
    # database yet.
    #
    # It does not make `init` unnecessary. `join` also needs a project, and
    # a project is per repository — so `init` still runs once per repo, and
    # the message below says so.
    _ensure_store()

    console.print(f"OK Joined as {current.user_id} ({current.device_id})", style="green")
    _print_entitlement(current)
    console.print(
        "\nThis machine is ready. In each repository you want to share plans\n"
        "from, run:\n"
        "    flanner init          adopts that repository\n"
        "    flanner join <id>     binds it to a workspace\n\n"
        "The workspace ids are listed above, and by 'flanner whoami'.",
        style="dim",
    )


@cli.group()
def devices() -> None:
    """Manage the machines enrolled under your account"""


@devices.command("list")
@click.option(
    "--output",
    type=click.Choice(["table", "json"]),
    default="table",
    help="Output format",
)
def devices_list(output: str) -> None:
    """Show every machine enrolled under your account

    `--output json` returns the control plane's own records unflattened, so a
    caller reading them does not have to undo the table's truncation of the
    timestamps.
    """
    import json as json_module

    from . import account

    enrolled = _console_call(account.list_devices)

    if output == "json":
        click.echo(json_module.dumps(enrolled, indent=2))
        return

    if not enrolled:
        console.print("No devices enrolled.", style="dim")
        return

    table = tui.table("Device", "Name", "Enrolled", "Last seen")
    for device in enrolled:
        here = " (this one)" if device.get("this_device") else ""
        table.add_row(
            device["device_id"] + here,
            device.get("label") or "-",
            (device.get("enrolled_at") or "")[:10],
            (device.get("last_seen_at") or "never")[:10],
        )
    console.print(table)


@devices.command("add")
def devices_add() -> None:
    """Mint a code to enroll another machine under your account"""
    from . import account

    code, expires_at = _console_call(account.request_enrollment_code)
    console.print("Run this on the other machine:", style="dim")
    console.print(f"\n  flanner login {code}\n", style="cyan")
    console.print(f"The code expires at {expires_at} and works once.", style="dim")


@devices.command("revoke")
@click.argument("device_id")
def devices_revoke(device_id: str) -> None:
    """Retire a machine, so it stops receiving entitlements"""
    from . import account
    from . import identity as this

    _console_call(account.revoke_device, device_id)
    console.print(f"OK {device_id} revoked", style="green")
    if device_id == this.device_id():
        console.print("That was this machine. Run 'flanner logout' here too.", style="yellow")
    console.print("Entitlements it already holds stay valid until they expire.", style="dim")


@cli.command()
@click.argument("email")
@click.option("--admin", is_flag=True, help="Invite as an organization admin")
def invite(email: str, admin: bool) -> None:
    """Invite someone to your organization (admins only)"""
    from . import account

    token = _console_call(account.invite_member, email, admin=admin)
    console.print(f"OK Invited {email}", style="green")
    console.print("\nSend them this:", style="dim")
    console.print(f"\n  flanner accept {token} --as <their-user-id>\n", style="cyan")
    console.print("An invitation costs no seat until it is accepted.", style="dim")


@cli.command()
def members() -> None:
    """List your organization's members and seat count (admins only)"""
    from . import account

    result = _console_call(account.list_members)
    console.print(f"Seats in use: {result.get('seats', 0)}\n")

    table = tui.table("Member", "Email", "Role", "State")
    for member in result.get("members") or []:
        style = "dim" if member["state"] != "active" else None
        table.add_row(
            member.get("user_id") or "(not joined)",
            member.get("email") or "-",
            member["role"],
            member["state"],
            style=style,
        )
    console.print(table)


def _console_call(action: Any, *args: Any, **kwargs: Any) -> Any:
    """Run a control-plane call, reporting a refusal rather than a traceback."""
    from . import account

    try:
        return action(*args, **kwargs)
    except account.SessionError as e:
        _session_failed(e)


@cli.command("retire")
@click.argument("plan_name")
@click.option("--project", default=None, help="Project name (uses current directory if omitted)")
@click.option("--reason", default="", help="Why, recorded with the claim")
@click.option("--restore", is_flag=True, help="Undo a retirement instead")
@click.option("--yes", is_flag=True, help="Skip the confirmation")
def retire_plan(
    plan_name: str, project: str | None, reason: str, restore: bool, yes: bool
) -> None:
    """Ask peers to stop showing a plan, or show it again with --restore

    Not a deletion, and the command is not named one. Nothing is erased:
    every version stays in the history, every signature still verifies, and
    a teammate who was offline when you ran this keeps the content until
    they next sync. What travels is a claim that other devices honour.
    """
    from . import review as review_module
    from .assurance import retirement

    session = _require_session()
    proj, plan_file = _resolve_plan(session, project, plan_name)

    standing = retirement(session, str(plan_file.id))
    if not restore and standing.retired:
        tui.note(f"{plan_name} is already retired.")
        return
    if restore and not standing.retired:
        tui.note(f"{plan_name} is not retired.")
        return

    if not yes and not restore:
        tui.warn(f"This asks every peer to hide {tui.code(plan_name)}.")
        tui.note("Nothing is erased. Anyone already holding it keeps the bytes,")
        tui.note("and a device that never receives this claim keeps showing it.")
        if not click.confirm("Record the claim?", default=False):
            tui.note("Nothing recorded.")
            return

    try:
        review_module.retire(
            session, project=proj, plan_file=plan_file, reason=reason, restore=restore
        )
    except PermissionError as e:
        tui.bad(str(e))
        raise SystemExit(1) from None

    if restore:
        tui.ok(f"{plan_name} is visible again")
    else:
        tui.ok(f"{plan_name} retired")
        # Said on the way out as well as at the prompt, because --yes skips
        # the prompt entirely and a script is exactly where somebody would
        # assume this deleted something.
        tui.note("Nothing was erased. Anyone already holding it keeps the bytes.")
        tui.hint(f"Undo with {tui.command(f'flanner retire {plan_name} --restore')}")


@cli.group()
def peer() -> None:
    """Sync plans directly with another device"""


def _keyring_refresher() -> Any:
    """Fetch this organisation's device keys and hand back a fresh resolver.

    The composition root is the only place allowed to join these two: a
    reachability test asserts that `peer` cannot reach `account`, so the
    network call is passed in from here rather than imported down there.

    A resolver is returned rather than nothing, because the one the serving
    path already holds is bound to the session as it was before the fetch
    and would still not know the key we just learned.
    """
    from . import account
    from . import session as cache

    def refresh() -> Any:
        account.fetch_device_keys()
        renewed = cache.load()
        return renewed.resolve_device_key if renewed is not None else None

    return refresh


def _catch_up_in_background(dial: Any) -> None:
    """Pull from known peers while the server is already answering.

    In a thread on purpose. Catching up means dialling machines that are
    mostly asleep, and doing that before binding would make start-up time a
    function of how many colleagues have shut their laptops.
    """
    import threading

    from . import peer as peer_transport
    from . import session as cache

    def run() -> None:
        with get_session() as session:
            workspaces = sorted(
                {p.workspace_id for p in db_list_projects(session) if p.workspace_id}
            )
            if not workspaces:
                return

            def report(device_id: str, _workspace: str, result: Any) -> None:
                if result.accepted:
                    tui.ok(f"caught up {len(result.accepted)} from {tui.code(device_id[:12])}")

            peer_transport.catch_up(session, workspaces, cache.load, dial=dial, on_result=report)

    threading.Thread(target=run, daemon=True).start()


@peer.command("serve")
@click.option("--host", default="127.0.0.1", help="Address to listen on (--http only)")
@click.option("--port", default=None, type=int, help="Port to listen on (--http only)")
@click.option(
    "--http",
    is_flag=True,
    help="Listen on a port instead, for peers already on the same network",
)
def peer_serve(host: str, port: int | None, http: bool) -> None:
    """Serve this device's catalog to authorised peers

    Reachable without a listening port, a forwarded port or administrator
    rights: this device dials out and answers on that connection. Nothing is
    served to a caller who cannot produce a signed request and a matching
    entitlement, so being reachable grants nothing on its own.
    """
    import uvicorn

    from . import identity as device_identity
    from . import peer as peer_transport
    from . import peer_iroh
    from . import session as cache

    # Before anything else: this command hands `get_session` to a background
    # thread and to the request handler rather than opening one itself, so
    # nothing here would otherwise initialise the database. The thread then
    # died on its first query while the server reported itself as serving.
    _open_store()

    if cache.load() is None:
        console.print("ERROR Not signed in, so no peer can be authorised.", style="red")
        console.print("Run 'flanner login' first.", style="dim")
        raise SystemExit(1)

    if not http:
        endpoint = peer_iroh.shared_endpoint()
        try:
            endpoint.ready()
        except peer_transport.PeerError as e:
            console.print(f"ERROR {e}", style="red")
            console.print("Use 'flanner peer serve --http' to listen on a port.", style="dim")
            raise SystemExit(1) from None
        console.print("Serving plans to authorised peers.", style="green")
        console.print(f"This device: {device_identity.device_id()}", style="dim")
        console.print(
            "Peers pull with 'flanner peer pull <device-id>'. No port is open.",
            style="dim",
        )
        _catch_up_in_background(
            lambda device_id, workspace_id: peer_iroh.peer_for(device_id, workspace_id, cache.load)
        )
        try:
            endpoint.serve(get_session, cache.load, _keyring_refresher())
        except KeyboardInterrupt:
            endpoint.close()
        return

    listen_on = port or peer_transport.DEFAULT_PORT
    console.print(f"Serving plans to authorised peers on {host}:{listen_on}", style="green")
    console.print(
        "Callers need a signed request and an entitlement for the workspace.", style="dim"
    )
    # Loopback by default. Every caller has to produce a signature and a
    # matching entitlement, so a wider bind grants nothing on its own — but
    # `--http` is the fallback somebody reaches for on a machine where the
    # default transport could not start, which is not the moment to open a
    # port on every interface without being asked.
    if beyond_loopback(host):
        tui.warn(f"Reachable from other machines on {host}. Anyone can reach the port.")
    # Over HTTP a peer is named by address, and this device knows device ids
    # rather than addresses, so there is nobody to dial. Catching up here is
    # a manual `flanner peer pull <address>`.
    uvicorn.run(
        # get_session is already a factory returning a context-managed
        # Session, which is exactly the shape the app wants.
        peer_transport.create_peer_app(get_session, cache.load, _keyring_refresher()),
        host=host,
        port=listen_on,
        log_level="warning",
    )


@peer.command("start")
@click.option("--host", default="127.0.0.1", help="Address to listen on (--http only)")
@click.option("--port", default=None, type=int, help="Port to listen on (--http only)")
@click.option(
    "--http",
    is_flag=True,
    help="Listen on a port instead, for peers already on the same network",
)
def peer_start(host: str, port: int | None, http: bool) -> None:
    """Serve to peers in the background, and keep serving after you log out

    `flanner peer serve` holds the terminal, which is why serving ended up
    being something one person did rather than everybody. Nothing about the
    protocol made it the admin's job: any device with a role in the workspace
    can answer, and several can answer at once. The only real constraint was
    uptime, and this removes it.
    """
    import subprocess
    import sys

    from . import session as cache

    _open_store()
    if cache.load() is None:
        console.print("ERROR Not signed in, so no peer could be authorised.", style="red")
        console.print("Run 'flanner login' first.", style="dim")
        raise SystemExit(1)

    pid_file = get_peer_pid_file()
    already = _running_pid(pid_file)
    if already is not None:
        tui.warn(f"Already serving (pid {already}). Stop it with 'flanner peer stop'.")
        return

    argv = [sys.executable, "-m", "flanner", "peer", "serve"]
    if http:
        # `serve` warns about a wide bind too, but in the background its
        # output goes to the log file, so the person who typed the command
        # would never see it. A warning nobody reads is not a warning.
        if beyond_loopback(host):
            tui.warn(f"Reachable from other machines on {host}. Anyone can reach the port.")
            console.print(
                "  Callers still need a signed request and an entitlement for the\n"
                "  workspace, so the port grants nothing on its own. Bind 127.0.0.1\n"
                "  unless another machine has to reach this one directly.",
                style="muted",
            )
        argv += ["--http", "--host", host]
        if port is not None:
            argv += ["--port", str(port)]

    log_path = get_mcp_dir() / "peer.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    # Where the log already ends, so a line from a previous run cannot be
    # read as this one having started.
    written_so_far = log_path.stat().st_size if log_path.exists() else 0

    detach: dict[str, Any] = (
        {"creationflags": subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP}
        if os.name == "nt"
        else {"start_new_session": True}
    )
    with log_path.open("ab") as log:
        child = subprocess.Popen(  # noqa: S603 - fixed argv, no shell, no user input
            argv,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            **detach,
        )

    # Recorded before it is known to be up, so `peer stop` can act on a
    # process that died while starting rather than leaving an orphan.
    pid_file.write_text(str(child.pid))

    console.print("Starting...", style="muted")
    ready = (
        _accepting(port or peer_default_port(), timeout=90.0, child=child)
        if http
        else _log_mentions(log_path, PEER_READY, since=written_so_far, timeout=90.0, child=child)
    )
    if not ready:
        pid_file.unlink(missing_ok=True)
        with contextlib.suppress(OSError):
            child.terminate()
        tui.warn("The peer server did not come up.")
        console.print(f"  Its output: {tui.code(str(log_path))}", style="muted")
        raise SystemExit(2)

    console.print()
    from . import identity as device_identity

    tui.ok("Serving plans to authorised peers, in the background")
    console.print(f"  This device: {tui.code(device_identity.device_id())}", style="muted")
    console.print(f"  pid {child.pid}, logging to {tui.code(str(log_path))}", style="muted")
    console.print(f"  Stop it with {tui.command('flanner peer stop')}", style="muted")
    console.print()


@peer.command("stop")
def peer_stop() -> None:
    """Stop the background peer server"""
    _stop_pid(get_peer_pid_file(), "Peer server")


@peer.command("status")
@click.argument("device_id", required=False)
def peer_status(device_id: str | None) -> None:
    """Show how this device is reachable, or how it reaches one peer

    With no argument, what a peer sees when it tries to reach this machine.
    With a device id, whether the connection to that machine goes direct or
    through a relay. Both work; a relay is slower, and that difference is
    invisible until someone is waiting for a sync.
    """
    from . import peer as peer_transport
    from . import peer_iroh
    from . import session as cache

    try:
        if device_id:
            route = peer_iroh.route_to(device_id, cache.load)
            console.print(f"Peer       {route.device_id}")
            console.print(
                f"Connection {route.connection}",
                style="yellow" if route.relayed else "green",
            )
            if route.address:
                console.print(f"Path       {route.address}")
            if route.rtt_ms:
                console.print(f"Round trip {route.rtt_ms} ms")
            if route.relayed:
                console.print(
                    "Relayed, so slower. Usually a firewall that refuses to be\n"
                    "punched through. Nothing is broken and nothing is exposed.",
                    style="dim",
                )
            return

        status = peer_iroh.local_status(cache.load)
    except peer_transport.PeerError as e:
        console.print(f"ERROR {e}", style="red")
        if not peer_iroh.available():
            console.print(
                "Everything else works. Only reaching a peer that has no address needs it.",
                style="dim",
            )
        raise SystemExit(1) from None

    console.print(f"This device {status.device_id}", style="green")

    # Reachable and actually answering are different things, and this command
    # reported only the first. A device with a perfect address and nothing
    # serving refuses every pull, which read as the other end's fault.
    serving = _running_pid(get_peer_pid_file())
    if serving is not None:
        console.print(f"Serving     yes, in the background (pid {serving})", style="green")
    else:
        console.print("Serving     no", style="yellow")
        console.print(
            "            'flanner peer start' serves in the background,\n"
            "            'flanner peer serve' in this terminal.",
            style="dim",
        )
    console.print("Peers reach it with 'flanner peer pull <device-id>'.", style="dim")
    if status.home_relay:
        console.print(f"Home relay  {status.home_relay}")
    if status.configured_relay:
        console.print(f"Own relay   {status.configured_relay}")
    for address in status.addresses:
        console.print(f"Address     {address}")
    console.print(
        "Addresses are how peers try to reach this machine directly.\n"
        "No port is listening: this device dials out and answers there.",
        style="dim",
    )
    _print_arrivals()


def _print_arrivals(limit: int = 8) -> None:
    """What teammates' work reached this device most recently.

    The whole notification system, and deliberately so: there is no service
    that could tell anybody a plan changed without also telling us, and
    knowing which plans a team touches is exactly the metadata this design
    refuses to hold. A list you can look at when you want one is what is
    left, and it turns out to be enough.
    """
    from . import identity
    from .database import recent_arrivals
    from .utils import format_relative_time

    # `peer status` answers about reachability and works before this device
    # has a catalog at all, so an uninitialised database is a normal state
    # here rather than an error.
    if not (get_mcp_dir() / "data.db").exists():
        return
    session = _require_session()
    arrivals = recent_arrivals(session, exclude_device_id=identity.device_id(), limit=limit)
    if not arrivals:
        console.print("\nNothing has arrived from a teammate yet.", style="dim")
        return

    console.print()
    table = tui.table("Arrived", "What", "From")
    for artifact in arrivals:
        table.add_row(
            format_relative_time(artifact.received_at) if artifact.received_at else "unknown",
            artifact.artifact_type,
            # Not truncated. A device id is twenty characters and two
            # teammates' ids share a prefix, so shortening it turns the one
            # column that says who into a column that says nothing.
            artifact.actor_device_id,
        )
    console.print(table)


@peer.command("pull")
@click.argument("address")
@click.option("--project", default=None, help="Project name (uses current directory if omitted)")
def peer_pull(address: str, project: str | None) -> None:
    """Pull whatever a peer holds for this project's workspace that we lack

    Give a device id to reach a peer wherever it is, or an http address for
    one already on this network.
    """
    from . import peer as peer_transport
    from . import peer_iroh
    from . import session as cache

    session = _require_session()
    proj = _resolve_project_or_cwd(session, project)
    if not proj:
        _no_project(project)
    if not proj.workspace_id:
        console.print("ERROR This project has not joined a workspace.", style="red")
        console.print("Run 'flanner join <workspace-id>' first.", style="dim")
        raise SystemExit(1)

    from . import observe

    try:
        with observe.step("resolve peer"):
            remote = peer_iroh.peer_for(address, proj.workspace_id, cache.load)
    except peer_transport.PeerError as e:
        console.print(f"ERROR {e}", style="red")
        raise SystemExit(1) from None

    with observe.step("fetch and verify"):
        report = peer_transport.pull(
            session, address, proj.workspace_id, cache.load, remote=remote, project=proj
        )
    observe.count(
        accepted=len(report.accepted),
        already_held=len(report.already_held),
        rejected=len(report.rejected),
    )

    console.print(f"accepted: {len(report.accepted)}", style="green")
    if report.already_held:
        console.print(f"already held: {len(report.already_held)}", style="dim")
    for artifact_id, reason in report.rejected:
        console.print(f"REJECTED {artifact_id}: {reason}", style="red")
    # Stored and verified, and still not a file anyone can open. Reported on
    # its own line and as a failure: "accepted: 1" with nothing in .plans
    # was the original defect, and a count that stays green while the plan
    # is missing is the count that hid it.
    for artifact_id, reason in report.unreadable:
        console.print(f"NOT WRITTEN {artifact_id}: {reason}", style="yellow")
    if not report.ok or report.unreadable:
        raise SystemExit(1)


def _sync_report(report: Any, *, verb: str, idle: str) -> None:
    """One shape for both directions, so pull and push read the same."""
    if report.accepted:
        tui.ok(f"{verb} {len(report.accepted)}")
    else:
        tui.note(idle)
    if report.already_held:
        tui.note(f"{len(report.already_held)} already there")
    for artifact_id, reason in report.rejected:
        tui.bad(f"{tui.code(artifact_id[:12])} {reason}")
    if not report.ok:
        raise SystemExit(1)


@peer.command("push")
@click.argument("address")
@click.option("--project", default=None, help="Project name (uses current directory if omitted)")
def peer_push(address: str, project: str | None) -> None:
    """Send a peer whatever it lacks for this project's workspace

    The peer decides what it will take. It checks every artifact against
    its author's key, refuses anything your role does not cover, and may
    decline pushes entirely — all of which show up here as refusals rather
    than as failures.

    Nothing is queued for a peer that is offline. They pick it up on their
    next pull.
    """
    from . import peer as peer_transport
    from . import peer_iroh
    from . import session as cache

    session = _require_session()
    proj = _resolve_project_or_cwd(session, project)
    if not proj:
        _no_project(project)
    if not proj.workspace_id:
        tui.bad("This project has not joined a workspace.")
        tui.hint(f"Run {tui.command('flanner join <workspace-id>')} first.")
        raise SystemExit(1)

    try:
        remote = peer_iroh.peer_for(address, proj.workspace_id, cache.load)
    except peer_transport.PeerError as e:
        tui.bad(str(e))
        raise SystemExit(1) from None

    report = peer_transport.push(session, address, proj.workspace_id, cache.load, remote=remote)
    _sync_report(report, verb="sent", idle="nothing to send")


@cli.group()
def mesh() -> None:
    """Join and inspect the private network this team's devices share"""


def _runtime(url: str = "") -> Any:
    """The mesh client wrapper.

    The CLI is a composition root, so this is the one place in the client
    package that names a provider. Everything else speaks `flanner.mesh`,
    and a test allows exactly this file and the adapter itself.
    """
    from .mesh_netbird import NetBirdRuntime

    return NetBirdRuntime(management_url=url)


@mesh.command("status")
def mesh_status() -> None:
    """Show whether this device is on the team's private network"""
    runtime = _runtime()
    if not runtime.installed():
        console.print("Mesh client not installed.", style="dim")
        console.print(
            "You almost certainly do not need one. 'flanner peer pull' reaches\n"
            "devices wherever they are, without a VPN or administrator rights.\n"
            "Run 'flanner peer status' to see how this machine is reached.",
            style="dim",
        )
        return

    status = runtime.status()
    console.print(
        f"Network {'connected' if status.enrolled else 'not connected'}",
        style="green" if status.enrolled else "yellow",
    )
    if status.message:
        console.print(f"        {status.message}", style="dim")
    for endpoint in status.endpoints:
        console.print(f"Address {endpoint}")

    peers = runtime.peers()
    if not peers:
        return
    table = tui.table("Peer address", "Connection")
    for peer in peers:
        table.add_row(peer.endpoint, peer.connection)
    console.print(table)
    console.print(
        "Addresses only. Who a peer is gets settled by the signed handshake,\n"
        "never by the network.",
        style="dim",
    )


@mesh.command("connect")
def mesh_connect() -> None:
    """Connect to the team's private network, if it has one

    Named apart from `flanner join` on purpose: that one binds a repository
    to a workspace, which is what makes review count and what peer sync is
    scoped by. This one puts the machine on a VPN and touches nothing
    flanner owns. Most teams need the first and never need this.
    """
    from . import account
    from .exceptions import MeshError
    from .mesh import Enrollment

    runtime = _runtime()
    if not runtime.installed():
        console.print("ERROR The mesh client is not installed on this machine.", style="red")
        raise SystemExit(1)

    try:
        offered = account.mesh_credential()
    except account.SessionError as e:
        _session_failed(e)

    if offered is None:
        console.print("This team has no managed network.", style="yellow")
        console.print(
            "Nothing to join; syncing over your existing network still works.", style="dim"
        )
        return

    credential, management_url, expires_at = offered
    try:
        _runtime(management_url).enroll(
            Enrollment(credential=credential, device_id="", expires_at=expires_at)
        )
    except MeshError as e:
        console.print(f"ERROR {e}", style="red")
        raise SystemExit(1) from None

    console.print("OK Connected to the team's private network", style="green")


@mesh.command("leave")
def mesh_leave() -> None:
    """Disconnect this device from the private network"""
    runtime = _runtime()
    if not runtime.installed():
        console.print("Mesh client not installed.", style="dim")
        return
    runtime.leave()
    console.print("OK Disconnected", style="green")
    console.print("Plans and local work are untouched.", style="dim")


# --- history, diff and why ------------------------------------------------------
#
# Three commands the mockups show. They read what is already recorded - the
# version rows and the files they point at - so none of them needs a network
# call or a git checkout.


def _version_bodies(versions: list[Any]) -> dict[int, str]:
    """The text of each version, skipping any whose file has gone missing."""
    from .storage import load_plan_file

    bodies: dict[int, str] = {}
    for version in versions:
        try:
            bodies[version.version] = load_plan_file(version.file_path)[1]
        except (FileNotFoundError, OSError):
            continue
    return bodies


def _churn(before: str | None, after: str) -> tuple[int, int]:
    """Lines added and removed between two versions.

    The first version counts as all-added: it did not replace anything, and
    reporting +0 for a plan somebody just wrote would be a lie of omission.
    """
    import difflib

    if before is None:
        return len(after.splitlines()), 0
    added = removed = 0
    for line in difflib.unified_diff(before.splitlines(), after.splitlines(), lineterm="", n=0):
        if line.startswith("+") and not line.startswith("+++"):
            added += 1
        elif line.startswith("-") and not line.startswith("---"):
            removed += 1
    return added, removed


def _section_of(lines: list[str], index: int) -> str:
    """The nearest markdown heading at or above a line.

    A hunk header reading `@@ ## Rollout @@` says where you are in the
    document. The line numbers difflib offers instead are true and useless.
    """
    for i in range(min(index, len(lines) - 1), -1, -1):
        if lines[i].startswith("#"):
            return lines[i].strip()
    return ""


@review.command("comment")
@click.argument("plan_name")
@click.option("--on", "quote", required=True, help="The text to attach the note to")
@click.option("-m", "--message", required=True, help="The note")
@click.option("--project", default=None, help="Project name")
@click.option("--version", "wanted", default=None, type=int, help="Version to comment on")
def review_comment(
    plan_name: str, quote: str, message: str, project: str | None, wanted: int | None
) -> None:
    """Leave a note against a quotation in a plan

    The note is anchored to what it quotes, not to a line number, so it
    survives the plan being edited above it. If the quoted text is later
    rewritten the note says it lost its place rather than sliding onto a
    sentence nobody meant.
    """
    from . import review as review_module

    session = _require_session()
    proj, plan_file = _resolve_plan(session, project, plan_name)
    try:
        review_module.comment(
            session,
            project=proj,
            plan_file=plan_file,
            quote=quote,
            body=message,
            version=wanted,
        )
    except ValueError as e:
        console.print(f"ERROR {e}", style="red")
        raise SystemExit(1) from None

    console.print()
    tui.ok("Comment recorded")
    console.print()
    console.print(
        tui.fields(
            [
                ("On", Text(anchors_clip(quote), style="code")),
                ("Note", Text(message, style="value")),
            ]
        )
    )
    console.print()
    tui.hint(f"See it with flanner review status {plan_file.name}")
    console.print()


def anchors_clip(text: str) -> str:
    from .anchors import clip

    return clip(text)


@review.command("pack")
@click.argument("plan_name")
@click.option("--project", default=None, help="Project name")
@click.option("--version", "wanted", default=None, type=int, help="Version to pack")
@click.option("--output", default=None, help="Where to write it")
@click.option("--no-fonts", is_flag=True, help="Leave the typefaces out, for a smaller file")
def review_pack(
    plan_name: str, project: str | None, wanted: int | None, output: str | None, no_fonts: bool
) -> None:
    """Write a plan as one file somebody outside the team can annotate

    No server, no upload, no account: the recipient opens the file and marks
    it up. They send back the JSON it exports and you run `flanner review
    import` on it.

    The packet carries the plan and nothing else. Notes your own team has
    left stay where they are.
    """
    from pathlib import Path as _Path

    from . import packet as packet_module
    from .database import get_version
    from .storage import load_plan_file

    session = _require_session()
    proj, plan_file = _resolve_plan(session, project, plan_name)
    version = get_version(session, plan_file.id, wanted)
    if version is None:
        console.print(f"ERROR v{wanted} of '{plan_file.name}' does not exist", style="red")
        raise SystemExit(1)
    try:
        _, body = load_plan_file(version.file_path)
    except FileNotFoundError:
        console.print(f"ERROR v{version.version} is no longer on disk", style="red")
        raise SystemExit(1) from None

    built = packet_module.build(
        plan_name=plan_file.name,
        version=version.version,
        body=body,
        project_name=proj.name,
        authored_at=version.created_at,
        embed_fonts=not no_fonts,
    )
    target = _Path(output or f"{plan_file.name}.v{version.version}.review.html")
    target.write_text(built.html, encoding="utf-8")

    console.print()
    tui.ok(f"Wrote [value]{target}[/value]")
    console.print()
    console.print(
        tui.fields(
            [
                ("Plan", Text(f"{plan_file.name}.md  v{version.version}", style="value")),
                ("Size", Text(f"{built.kib} KiB", style="value")),
                ("Sections", Text(str(len(built.headings)), style="muted")),
                ("Contains", Text("this plan only, no team review", style="muted")),
            ]
        )
    )
    console.print()
    tui.hint("Send that file to your reviewer. They need nothing installed.")
    tui.hint(f"When it comes back: flanner review import <file> --project {proj.name}")
    console.print()


def _print_imported_notes(notes: list[dict[str, Any]], reviewer: str) -> None:
    """What was just recorded, and the caveat that comes with it.

    The unverified warning is not optional. These notes arrived in a file
    rather than from a device with a key, so nothing in them is signed by
    the person named, and a reader who mistook them for a real review would
    be treating an unauthenticated claim as sign-off.
    """
    console.print()
    tui.ok(f"Recorded {len(notes)} note{'' if len(notes) == 1 else 's'} from {reviewer}")
    console.print()
    listing = tui.table("On", ("Note", {"overflow": "fold"}))
    for note in notes[:12]:
        listing.add_row(
            Text(str(note.get("quote", ""))[:44], style="muted"),
            Text(str(note.get("body", "")), style="value"),
        )
    console.print(listing)
    if len(notes) > 12:
        console.print()
        tui.note(f"{len(notes) - 12} more not shown.")
    console.print()
    tui.warn("Unverified: the reviewer has no device key, so nothing here is signed by them.")
    console.print()


@review.command("import")
@click.argument("path", type=click.Path(exists=True, dir_okay=False))
@click.option("--project", default=None, help="Project name")
@click.option("--plan", "plan_override", default=None, help="Attach to this plan instead")
def review_import(path: str, project: str | None, plan_override: str | None) -> None:
    """Take back the notes a review packet exported

    The reviewer had no device key, so nothing they wrote is signed by them.
    This device signs that it received the notes: a claim about where they
    came from, never about who wrote them. They are recorded as unverified
    and shown that way.
    """
    import json as json_module
    from pathlib import Path as _Path

    from . import review as review_module

    try:
        payload = json_module.loads(_Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        console.print(f"ERROR could not read that review: {e}", style="red")
        raise SystemExit(1) from None

    header = payload.get("packet") or {}
    notes = payload.get("notes") or []
    reviewer = str(payload.get("reviewer") or "").strip() or "an unnamed reviewer"
    named = plan_override or str(header.get("plan") or "")
    if not named:
        console.print("ERROR that file does not say which plan it belongs to", style="red")
        raise SystemExit(1)
    if not notes:
        console.print()
        tui.note("That review has no notes in it. Nothing to record.")
        console.print()
        return

    session = _require_session()
    proj, plan_file = _resolve_plan(session, project, named)

    packed_at = header.get("version")
    current = plan_file.current_version
    if packed_at and packed_at != current:
        tui.warn(
            f"Written against v{packed_at}; this plan is now at v{current}. "
            "Notes are recorded against what was reviewed."
        )

    review_module.import_external(
        session,
        project=proj,
        plan_file=plan_file,
        reviewer=reviewer,
        notes=notes,
        reviewed_version=packed_at if isinstance(packed_at, int) else None,
        source=str(payload.get("source") or "packet"),
    )

    _print_imported_notes(notes, reviewer)


def _history_row(
    version: Any, bodies: dict[int, str], plan_file: Any, linked: dict[Any, Any]
) -> tuple[Text, ...]:
    """One version's row: how much changed, and anything worth saying about it.

    The change column is measured against the previous version's body rather
    than being stored, because a version adopted from disk or from a peer
    never recorded one.
    """
    from .utils import format_relative_time

    added, removed = _churn(bodies.get(version.version - 1), bodies.get(version.version, ""))
    change = Text()
    change.append(f"+{added}", style="ok")
    if removed:
        change.append(f" -{removed}", style="bad")

    note = Text(version.notes or "", style="muted")
    if version.version == 1 and not version.notes:
        note = Text("created", style="muted")
    if linked and version.version == plan_file.current_version:
        note.append("  linked ", style="muted")
        note.append(next(iter(linked.values())).linear_issue_id, style="accent")

    return (
        Text(f"v{version.version}", style="value"),
        Text(
            format_relative_time(version.created_at) if version.created_at else "unknown",
            style="muted",
        ),
        Text(version.created_by or "user", style="muted"),
        change,
        note,
    )


@cli.command()
@click.argument("plan_name")
@click.option("--project", default=None, help="Project name")
@click.option("--limit", default=0, type=int, help="Show only the newest N versions")
def history(plan_name: str, project: str | None, limit: int) -> None:
    """Every version of a plan, newest first"""
    from .database import get_linear_links, list_versions

    session = _require_session()
    proj, plan_file = _resolve_plan(session, project, plan_name)

    versions = list_versions(session, plan_file.id)
    if not versions:
        console.print()
        tui.note(f"{plan_file.name} has no versions recorded.")
        console.print()
        return

    bodies = _version_bodies(versions)
    linked = {link.plan_file_id: link for link in get_linear_links(session, plan_file.id)}

    listing = tui.table(
        ("Ver", {"justify": "right"}),
        "When",
        "By",
        ("Change", {"justify": "right"}),
        "Note",
    )
    for version in versions[:limit] if limit > 0 else versions:
        listing.add_row(*_history_row(version, bodies, plan_file, linked))

    console.print()
    console.print(listing)
    console.print()
    total = len(versions)
    tui.note(f"{total} version{'' if total == 1 else 's'} of {plan_file.name} in {proj.name}")
    if total > 1:
        newest, older = versions[0].version, versions[1].version
        tui.hint(f"Compare with flanner diff {plan_file.name} v{older} v{newest}")
    console.print()


def _pick_versions(
    from_version: str | None, to_version: str | None, numbers: list[int]
) -> tuple[int, int]:
    """Which two versions to compare, defaulting to the last two.

    Accepts `3` or `v3`, because both are what people type.

    The defaults are only computed when a version was actually omitted:
    `numbers[-2]` raises on a single-version plan, and asking for one
    explicit version there is legitimate.
    """

    def parse(raw: str | None, fallback: int) -> int:
        if raw is None:
            return fallback
        try:
            return int(raw.lstrip("vV"))
        except ValueError:
            console.print(f"ERROR '{raw}' is not a version number", style="red")
            raise SystemExit(1) from None

    return (
        parse(from_version, numbers[-2] if from_version is None else 0),
        parse(to_version, numbers[-1] if to_version is None else 0),
    )


def _print_hunk(
    group: list[tuple[str, int, int, int, int]], before: list[str], after: list[str]
) -> None:
    """One run of changed lines, with its three lines of context."""
    for tag, i1, i2, j1, j2 in group:
        if tag in ("replace", "delete"):
            for line in before[i1:i2]:
                console.print(Text(f"- {line}", style="bad"))
        if tag in ("replace", "insert"):
            for line in after[j1:j2]:
                console.print(Text(f"+ {line}", style="ok"))
        if tag == "equal":
            for line in before[i1:i2]:
                console.print(Text(f"  {line}", style="muted"))


def _print_hunks(before: list[str], after: list[str]) -> bool:
    """Every changed run, headed by the section it falls in.

    The heading names the nearest markdown heading rather than a line range,
    because "## Rollout" tells a reader what moved and "lines 240-260" does
    not. It falls back to the range when there is no heading above the change.

    Returns whether anything was printed, which is how the caller tells an
    identical pair from a changed one.
    """
    import difflib

    printed = False
    for group in difflib.SequenceMatcher(None, before, after).get_grouped_opcodes(3):
        printed = True
        section = _section_of(after, group[0][3])
        rule = Text()
        rule.append("@@ ", style="muted")
        rule.append(section or f"lines {group[0][3] + 1}-{group[-1][4]}", style="accent")
        rule.append(" @@", style="muted")
        console.print(rule)
        _print_hunk(group, before, after)
        console.print()
    return printed


@cli.command()
@click.argument("plan_name")
@click.argument("from_version", required=False)
@click.argument("to_version", required=False)
@click.option("--project", default=None, help="Project name")
def diff(
    plan_name: str, from_version: str | None, to_version: str | None, project: str | None
) -> None:
    """What changed between two versions of a plan

    With no versions given, compares the last two. Versions may be written
    as `3` or `v3`.
    """
    from .database import list_versions

    session = _require_session()
    _, plan_file = _resolve_plan(session, project, plan_name)
    versions = list_versions(session, plan_file.id)
    numbers = sorted(v.version for v in versions)

    if len(numbers) < 2 and not (from_version and to_version):
        console.print()
        tui.note(f"{plan_file.name} has only one version, so there is nothing to compare.")
        console.print()
        return

    left, right = _pick_versions(from_version, to_version, numbers)
    bodies = _version_bodies(versions)
    for wanted in (left, right):
        if wanted not in bodies:
            console.print(f"ERROR v{wanted} of '{plan_file.name}' is not on disk", style="red")
            raise SystemExit(1)

    console.print()
    header = Text()
    header.append(f"{plan_file.name}.md", style="value")
    header.append(f"  v{left} ", style="muted")
    header.append(tui.ARROW, style="muted")
    header.append(f" v{right}", style="muted")
    console.print(header)
    console.print()

    printed = _print_hunks(bodies[left].splitlines(), bodies[right].splitlines())
    if not printed:
        tui.note("No differences. The two versions have identical text.")
        console.print()
        return

    added, removed = _churn(bodies[left], bodies[right])
    summary = Text()
    summary.append(f"+{added}", style="ok")
    summary.append(" added", style="muted")
    if removed:
        summary.append(f"  {tui.MIDDOT}  ", style="muted")
        summary.append(f"-{removed}", style="bad")
        summary.append(" removed", style="muted")
    console.print(summary)
    console.print()


@cli.command()
@click.argument("plan_name")
@click.option("--project", default=None, help="Project name")
@click.pass_context
def why(ctx: click.Context, plan_name: str, project: str | None) -> None:
    """Why a plan is judged fresh, aging, suspect or stale

    The same evidence `flanner freshness <plan>` prints. Kept as its own
    command because "why is this stale" is the question people actually
    have, and it is not obvious that a command called freshness answers it.
    """
    ctx.invoke(freshness, plan_name=plan_name, project=project, output="table")


# --- worked examples ----------------------------------------------------------
#
# `--project TEXT` tells a reader the flag exists and nothing about what goes
# in it. `--project checkout-service` answers that, so these use real-looking
# values rather than <placeholders>.
#
# Attached to the built commands in one pass rather than as a decorator on
# each, so the whole set is readable together and a test can check every key
# against a command that exists.

EXAMPLES: dict[str, tuple[str, ...]] = {
    "init": (
        "flanner init                     adopt the repository you are in",
        "flanner init --plan-dir docs/plans",
        "flanner init --skip-claude       do not register the MCP server",
    ),
    "list": (
        "flanner list                     every project on this machine",
        "flanner list --project checkout-service",
    ),
    "web": (
        "flanner web                      the local UI on 8080",
        "flanner web --port 8090 --open-browser",
    ),
    "doctor": ("flanner doctor                   catalog against the files on disk",),
    "sync": (
        "flanner sync                     import .plans files already there",
        "flanner sync --dry-run           show what it would import",
    ),
    "history": ("flanner history payment-webhooks --project checkout-service",),
    "diff": (
        "flanner diff payment-webhooks 3 5 --project checkout-service",
        "flanner diff payment-webhooks 5  against the version before it",
    ),
    "freshness": (
        "flanner freshness                every plan, worst first",
        "flanner freshness payment-webhooks",
        "flanner freshness --output json  for a script",
    ),
    "why": ("flanner why payment-webhooks --project checkout-service",),
    "config": ("flanner config checkout-service --plan-dir docs/plans",),
    "delete": ("flanner delete old-service --force",),
    "setup-gitignore": ("flanner setup-gitignore checkout-service",),
    "retire": (
        "flanner retire anchor-demo --reason 'superseded by v2'",
        "flanner retire anchor-demo --restore",
    ),
    "login": (
        "flanner login K7QP2M4X           code from your team console",
        "flanner login K7QP2M4X --endpoint https://app.flanner.io",
    ),
    "accept": (
        "flanner accept Aitkkm46g6MXnk15 --as jayson \\",
        "        --endpoint https://app.flanner.io",
    ),
    "whoami": (
        "flanner whoami                   identity, access, workspaces",
        "flanner whoami --refresh         renew, to pick up a new grant now",
    ),
    "logout": ("flanner logout",),
    "invite": ("flanner invite raj@acme.test",),
    "members": ("flanner members",),
    "join": (
        "flanner join ws_f24dca1f15b391e1 bind this repository",
        "flanner join                     lists the ids you may use",
        "flanner join --clear             leave the workspace",
    ),
    "review status": ("flanner review status payment-webhooks --project checkout-service",),
    "review propose": ("flanner review propose payment-webhooks --message 'retry budget raised'",),
    "review decide": (
        "flanner review decide payment-webhooks --accept",
        "flanner review decide payment-webhooks --reject",
    ),
    "review comment": (
        "flanner review comment payment-webhooks \\",
        "        --on 'The retry budget is three attempts' \\",
        "        -m 'Is three enough under load?'",
    ),
    "review pack": ("flanner review pack payment-webhooks --out review.html",),
    "review import": ("flanner review import payment-webhooks --from review.notes.json",),
    "peer serve": (
        "flanner peer serve               reachable with no open port",
        "flanner peer serve --http --port 8776",
    ),
    "peer pull": (
        "flanner peer pull dev_7ab74afd93b09861",
        "flanner peer pull http://192.168.1.20:8776",
    ),
    "peer push": ("flanner peer push dev_7ab74afd93b09861",),
    "peer status": (
        "flanner peer status              how peers reach this machine",
        "flanner peer status dev_7ab74afd93b09861",
    ),
    "devices list": ("flanner devices list",),
    "devices revoke": ("flanner devices revoke dev_7ab74afd93b09861",),
    "mesh status": ("flanner mesh status",),
}


def _attach_examples(group: click.Group, prefix: str = "") -> None:
    r"""Hang the worked examples off each command's help screen.

    The leading ``\b`` is click's marker for "do not rewrap what follows".
    Without it the examples are reflowed into a paragraph, which turns a
    column of commands into prose and loses the alignment that makes them
    scannable.
    """
    for name, command in group.commands.items():
        path = f"{prefix}{name}"
        lines = EXAMPLES.get(path)
        if lines:
            command.epilog = "Examples:\n\n\b\n" + "\n".join(f"  {line}" for line in lines)
        if isinstance(command, click.Group):
            _attach_examples(command, f"{path} ")


def main() -> None:
    """Entry point, and the only place an exit code is decided for a fault.

    Three codes, so a script can tell the two failures apart:

    * 0 -- it worked
    * 1 -- you asked for something that cannot be done (no such project, no
      access, a workspace id that is not yours). Fix the command and retry.
    * 2 -- the machine underneath failed (disk, permissions, a store that
      will not open). Retrying the same command will not help.

    Click already produces 0 and 1. Without this wrapper the second kind
    arrived as a traceback, which tells a user nothing and a script less: an
    unreadable store and a typo in a project name exited identically.
    """
    try:
        cli.main(standalone_mode=True)
    except (DatabaseError, StorageError, OSError) as e:
        console.print(f"ERROR {e}", style="red")
        tui.note("A failure on this machine, not a problem with the command itself.")
        raise SystemExit(2) from None


_attach_examples(cli)
