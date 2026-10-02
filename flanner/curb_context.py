"""Which launch of an agent a Curb report describes (Curb PRD §8.3).

Settings files say what an agent would apply, not what a running session
does: a session can add flags, pick another profile or start in another
folder. So every result is for a stated launch context, and the report
prints the assumption behind it.

The flag tables are the documented flags of the baseline versions in
`BASELINE`, read from their `--help`. A flag Curb does not know could
change anything, so it is recorded and the report treats every control as
unknown rather than guessing what the flag did.

Standard library only.
"""

from __future__ import annotations

import shlex
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path

CLAUDE = "claude"
CODEX = "codex"
AGENTS = (CLAUDE, CODEX)

#: The versions the R1 corpus is written against. Anything else is analysed,
#: but never as supported (PRD §8.1).
BASELINE = {CLAUDE: "2.1.287", CODEX: "0.154.0"}

LABELS = {CLAUDE: "Claude Code", CODEX: "Codex"}

ASSUMPTION = (
    "Assumes no other flags, profiles or settings files. Running sessions are not inspected."
)

# --- Claude Code 2.1.287 -----------------------------------------------------

#: Flags that take one value.
_CLAUDE_VALUE = frozenset(
    {
        "--agent",
        "--agents",
        "--append-system-prompt",
        "--autocompact",
        "--debug-file",
        "--effort",
        "--environment",
        "--fallback-model",
        "--input-format",
        "--json-schema",
        "--max-budget-usd",
        "--model",
        "-n",
        "--name",
        "--output-format",
        "--permission-mode",
        "--permission-prompts",
        "--plugin-dir",
        "--plugin-url",
        "--remote-control-session-name-prefix",
        "--session-id",
        "--setting-sources",
        "--settings",
        "--system-prompt",
        "--system-prompt-snapshot",
    }
)
#: Flags that take every value up to the next flag.
_CLAUDE_MANY = frozenset(
    {
        "--add-dir",
        "--allowedTools",
        "--allowed-tools",
        "--disallowedTools",
        "--disallowed-tools",
        "--mcp-config",
        "--betas",
        "--file",
        "--tools",
    }
)
#: Flags with no value, or an optional one that only counts when attached
#: with `=`; a separate word after them is read as the prompt.
_CLAUDE_BARE = frozenset(
    {
        "--allow-dangerously-skip-permissions",
        "--ax-screen-reader",
        "--bg",
        "--background",
        "--bare",
        "--brief",
        "--chrome",
        "--no-chrome",
        "-c",
        "--continue",
        "--dangerously-skip-permissions",
        "--desktop",
        "--disable-slash-commands",
        "--exclude-dynamic-system-prompt-sections",
        "--fork-session",
        "--forward-subagent-text",
        "--ide",
        "--include-hook-events",
        "--include-partial-messages",
        "--no-session-persistence",
        "-p",
        "--print",
        "--replay-user-messages",
        "--restricted",
        "--safe-mode",
        "--strict-mcp-config",
        "--tmux",
        "--verbose",
        "-v",
        "--version",
        "-h",
        "--help",
        "-r",
        "--resume",
        "-d",
        "--debug",
        "--cloud",
        "--from-pr",
        "--prompt-suggestions",
        "--remote-control",
        "--teleport",
        "-w",
        "--worktree",
    }
)

# --- Codex 0.154.0 ------------------------------------------------------------

_CODEX_VALUE = frozenset(
    {
        "-c",
        "--config",
        "--enable",
        "--disable",
        "--remote",
        "--remote-auth-token-env",
        "-m",
        "--model",
        "--local-provider",
        "-p",
        "--profile",
        "-s",
        "--sandbox",
        "-C",
        "--cd",
        "--add-dir",
        "-a",
        "--ask-for-approval",
        "--thread-source",
        "--output-schema",
        "--color",
        "-o",
        "--output-last-message",
    }
)
_CODEX_MANY = frozenset({"-i", "--image"})
_CODEX_BARE = frozenset(
    {
        "--strict-config",
        "--oss",
        "--approve-for-me",
        "--dangerously-bypass-approvals-and-sandbox",
        "--dangerously-bypass-hook-trust",
        "--worktree",
        "--search",
        "--no-alt-screen",
        "--skip-git-repo-check",
        "--ephemeral",
        "--ignore-user-config",
        "--ignore-rules",
        "--json",
        "--last",
        "-h",
        "--help",
        "-V",
        "--version",
    }
)
#: Subcommands that start an agent session. Anything else (`login`, `mcp`,
#: `doctor`) is not an agent run and has no reach to report.
_CODEX_RUNS = frozenset({"exec", "e", "review", "resume", "fork"})


@dataclass(frozen=True)
class LaunchContext:
    """One way of starting one agent."""

    agent: str
    cwd: Path
    #: Where the context came from: "default", "command", or a scheduled job.
    source: str = "default"
    #: The launch command as typed, for the full report only.
    command: tuple[str, ...] = ()
    unknown_flags: tuple[str, ...] = ()
    headless: bool = False

    # Claude Code
    settings: tuple[str, ...] = ()
    permission_mode: str | None = None
    prompts_denied: bool = False
    mcp_configs: tuple[str, ...] = ()
    strict_mcp: bool = False
    disallowed: tuple[str, ...] = ()
    tools: tuple[str, ...] | None = None
    restricted: bool = False
    safe_mode: bool = False
    bare: bool = False
    setting_sources: tuple[str, ...] | None = None
    add_dirs: tuple[str, ...] = ()

    # Codex
    profile: str | None = None
    overrides: tuple[tuple[str, str], ...] = ()
    sandbox: str | None = None
    approval: str | None = None
    approve_for_me: bool = False
    bypass: bool = False
    search: bool = False
    ignore_user_config: bool = False

    notes: tuple[str, ...] = field(default=())

    @property
    def label(self) -> str:
        return LABELS[self.agent]

    def describe(self) -> str:
        """One line naming the launch, without the prompt or any value that could be private."""
        flags = [word for word in self.command[1:] if word.startswith("-")]
        how = (
            f"{self.agent} {' '.join(flags)}".strip()
            if self.command
            else f"{self.agent}, no flags"
        )
        where = "from a scheduled job" if self.source.startswith("scheduled") else ""
        return " ".join(part for part in (f"{self.label} launched as `{how}`", where) if part)


class LaunchError(ValueError):
    """A launch command Curb cannot read as a Claude Code or Codex run."""


def default(agent: str, cwd: Path) -> LaunchContext:
    """The agent launched from `cwd` with no flags."""
    if agent not in AGENTS:
        raise LaunchError(f"Curb reads {LABELS[CLAUDE]} and {LABELS[CODEX]}, not {agent!r}")
    return LaunchContext(agent=agent, cwd=cwd)


def agent_of(word: str) -> str | None:
    """`claude`, `codex`, or None, from a program name or path."""
    name = Path(word.replace("\\", "/")).name.lower()
    for suffix in (".exe", ".cmd", ".bat", ".ps1"):
        name = name.removesuffix(suffix)
    return name if name in AGENTS else None


def parse(argv: Sequence[str], cwd: Path, *, source: str = "command") -> LaunchContext:
    """A launch context from a command line such as `claude --settings x.json`."""
    words = list(argv)
    if len(words) == 1 and " " in words[0]:
        words = shlex.split(words[0], posix=True)
    if not words:
        raise LaunchError("give the launch command after --, such as -- claude --settings x.json")
    agent = agent_of(words[0])
    if agent is None:
        raise LaunchError(
            f"{words[0]!r} is not a {LABELS[CLAUDE]} or {LABELS[CODEX]} command; "
            "Curb reads those two"
        )
    context = LaunchContext(agent=agent, cwd=cwd, source=source, command=tuple(words))
    if agent == CLAUDE:
        return _parse_claude(context, words[1:])
    return _parse_codex(context, words[1:])


def _split(word: str) -> tuple[str, str | None]:
    if word.startswith("--") and "=" in word:
        flag, value = word.split("=", 1)
        return flag, value
    return word, None


def _parse_claude(context: LaunchContext, words: list[str]) -> LaunchContext:
    found: dict[str, list[str]] = {}
    unknown: list[str] = []
    i = 0
    while i < len(words):
        flag, attached = _split(words[i])
        i += 1
        if not flag.startswith("-") or flag == "-":
            continue  # the prompt, or a value nobody claimed
        if flag in _CLAUDE_VALUE:
            if attached is None and i < len(words):
                attached, i = words[i], i + 1
            found.setdefault(flag, []).append(attached or "")
        elif flag in _CLAUDE_MANY:
            taken = [attached] if attached is not None else []
            while i < len(words) and not words[i].startswith("-"):
                taken.append(words[i])
                i += 1
            found.setdefault(flag, []).extend(taken)
        elif flag in _CLAUDE_BARE:
            found.setdefault(flag, []).append(attached or "")
        else:
            unknown.append(flag)

    def values(*names: str) -> tuple[str, ...]:
        out: list[str] = []
        for name in names:
            for value in found.get(name, []):
                out.extend(part for part in value.replace(",", " ").split() if part)
        return tuple(out)

    def has(*names: str) -> bool:
        return any(name in found for name in names)

    mode = found["--permission-mode"][-1] if has("--permission-mode") else None
    if mode == "manual":
        mode = "default"
    if has("--dangerously-skip-permissions"):
        mode = "bypassPermissions"
    sources = None
    if has("--setting-sources"):
        sources = tuple(part.strip() for part in found["--setting-sources"][-1].split(",") if part)
    tools: tuple[str, ...] | None = None
    if has("--tools"):
        raw = values("--tools")
        tools = () if not raw else ("default",) if raw == ("default",) else raw
    return replace(
        context,
        unknown_flags=tuple(unknown),
        headless=has("-p", "--print"),
        settings=tuple(found.get("--settings", [])),
        permission_mode=mode,
        prompts_denied=found.get("--permission-prompts", [""])[-1] == "none",
        mcp_configs=tuple(v for v in found.get("--mcp-config", []) if v),
        strict_mcp=has("--strict-mcp-config"),
        disallowed=_rules(
            found.get("--disallowedTools", []) + found.get("--disallowed-tools", [])
        ),
        tools=tools,
        restricted=has("--restricted"),
        safe_mode=has("--safe-mode"),
        bare=has("--bare"),
        setting_sources=sources,
        add_dirs=values("--add-dir"),
    )


def _rules(words: list[str]) -> tuple[str, ...]:
    """Permission rules from `--disallowedTools`, which takes commas or spaces.

    A comma inside parentheses belongs to the rule, as in `Bash(git push *)`.
    """
    rules: list[str] = []
    for word in words:
        current, depth = "", 0
        for char in word:
            if char == "(":
                depth += 1
            elif char == ")":
                depth = max(0, depth - 1)
            if char == "," and depth == 0:
                if current.strip():
                    rules.append(current.strip())
                current = ""
                continue
            current += char
        if current.strip():
            rules.append(current.strip())
    return tuple(rules)


def _parse_codex(context: LaunchContext, words: list[str]) -> LaunchContext:
    found: dict[str, list[str]] = {}
    unknown: list[str] = []
    subcommand: str | None = None
    i = 0
    while i < len(words):
        flag, attached = _split(words[i])
        i += 1
        if not flag.startswith("-"):
            if subcommand is None:
                subcommand = flag
            continue
        if flag in _CODEX_VALUE:
            if attached is None and i < len(words):
                attached, i = words[i], i + 1
            found.setdefault(flag, []).append(attached or "")
        elif flag in _CODEX_MANY:
            while i < len(words) and not words[i].startswith("-"):
                i += 1
        elif flag in _CODEX_BARE:
            found.setdefault(flag, []).append("")
        else:
            unknown.append(flag)

    if subcommand is not None and subcommand not in _CODEX_RUNS:
        # `codex "fix the build"` is a run with a prompt; `codex login` is not.
        # Only the documented subcommands are treated as not-a-run.
        if subcommand in {"login", "logout", "mcp", "plugin", "app-server", "doctor", "update"}:
            raise LaunchError(f"`codex {subcommand}` does not start an agent session")

    def last(*names: str) -> str | None:
        for name in names:
            if name in found:
                return found[name][-1]
        return None

    overrides: list[tuple[str, str]] = []
    for raw in found.get("-c", []) + found.get("--config", []):
        key, sep, value = raw.partition("=")
        if sep:
            overrides.append((key.strip(), value.strip()))
    for feature in found.get("--enable", []):
        overrides.append((f"features.{feature}", "true"))
    for feature in found.get("--disable", []):
        overrides.append((f"features.{feature}", "false"))

    cwd = context.cwd
    target = last("-C", "--cd")
    if target:
        cwd = (cwd / target).resolve() if not Path(target).is_absolute() else Path(target)

    return replace(
        context,
        cwd=cwd,
        unknown_flags=tuple(unknown),
        headless=subcommand in {"exec", "e", "review"},
        profile=last("-p", "--profile"),
        overrides=tuple(overrides),
        sandbox=last("-s", "--sandbox"),
        approval=last("-a", "--ask-for-approval"),
        approve_for_me="--approve-for-me" in found,
        bypass="--dangerously-bypass-approvals-and-sandbox" in found,
        search="--search" in found,
        ignore_user_config="--ignore-user-config" in found,
    )
