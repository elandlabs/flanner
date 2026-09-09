"""Where a coding agent keeps its skills, and which copy actually wins.

A skill is a directory holding a `SKILL.md` with YAML frontmatter. Agents
disagree about where those directories live and which one takes effect
when two share a name, so every agent gets an adapter rather than the
scanner growing a chain of special cases.

An adapter answers three questions and nothing else:

  - which roots hold skills, for a given project
  - what scope a root represents
  - which of several copies of a name is the effective one

It never executes anything it finds. A skill package can carry scripts,
and this module exists to *read about* them; running one during a scan
would turn `flanner skills scan` into arbitrary code execution on
somebody's machine.

FOUNDATION: this imports nothing from the package. It is filesystem and
JSON only, so it can be tested against a directory tree with no database
and no project.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

#: The file that makes a directory a skill package.
MANIFEST = "SKILL.md"

#: Scopes, widest last. Order matters: `resolve_precedence` prefers the
#: narrowest scope, on the same reasoning every tool with layered config
#: uses — the setting closest to the work wins.
#:
#: `admin` is a machine-wide directory an administrator deploys, which
#: Codex reads and Claude Code has no equivalent of. It sits below `user`
#: because a person's own skills are closer to their work than the
#: machine's are, and above `plugin` because a plugin is something
#: installed rather than something placed deliberately.
PLUGIN = "plugin"
ADMIN = "admin"
USER = "user"
PROJECT = "project"
SCOPE_ORDER = (PROJECT, USER, ADMIN, PLUGIN)

#: Why a root could not be read. Kept as values rather than exceptions so
#: one unreadable directory does not abort a scan of nine good ones.
UNREADABLE = "unreadable"
ABSENT = "absent"


@dataclass(frozen=True)
class Root:
    """One directory an adapter says may contain skill packages."""

    path: Path
    scope: str
    agent: str
    #: For plugin roots: which plugin, and which cached revision.
    plugin: str | None = None
    revision: str | None = None
    #: True when the agent's own config says this revision is the live one.
    #: A plugin cache keeps old revisions, and they look identical on disk.
    active: bool = True
    status: str = "ok"


@dataclass(frozen=True)
class Discovered:
    """One skill package found on disk, before anything is read from it."""

    name: str
    directory: Path
    manifest: Path
    root: Root


@dataclass
class Capability:
    """What an adapter can actually do on this machine.

    Declared rather than assumed. An adapter that cannot observe says so,
    and the surfaces above degrade to inventory with a visible status
    instead of reporting zero usage.
    """

    agent: str
    discover: bool = True
    resolve_precedence: bool = False
    observe: bool = False
    install: bool = False
    #: Why a capability is missing, for the status line.
    notes: list[str] = field(default_factory=list)


def _home() -> Path:
    """The user's home, honouring the test override the rest of flanner uses."""
    override = os.environ.get("FLANNER_SKILLS_HOME") or os.environ.get("HOME_FOR_TESTS")
    return Path(override) if override else Path.home()


class Adapter:
    """What every agent's layout has in common.

    A skill is a directory holding a manifest, whatever the agent. Only
    two things differ between agents and both are left to the subclass:
    which directories to look in, and whether two copies of a name
    compete or are simply both offered.
    """

    agent = ""

    def capability(self) -> Capability:  # pragma: no cover - subclasses answer
        raise NotImplementedError

    def roots(self, project_root: Path | None = None) -> list[Root]:  # pragma: no cover
        raise NotImplementedError

    def discover(self, root: Root) -> list[Discovered]:
        """Skill directories under one root, at most two levels down.

        Usually `skills/<name>/SKILL.md`. Some plugins interpose a version
        directory — `skills/v1/<name>/SKILL.md` — so a directory with no
        manifest of its own is opened once more. On the machine this was
        first run against that second level held 49 of 146 packages, so
        stopping at one level would have silently under-reported a third
        of the collection.

        Two levels and no further. Walking arbitrarily deep turns a stray
        `node_modules` into a minutes-long scan, and no supported agent
        nests a skill inside another skill.
        """
        if not root.path.is_dir():
            return []
        out = []
        for entry in _subdirs(root.path):
            manifest = entry / MANIFEST
            if manifest.is_file():
                out.append(
                    Discovered(name=entry.name, directory=entry, manifest=manifest, root=root)
                )
                continue
            for nested in _subdirs(entry):
                deeper = nested / MANIFEST
                if deeper.is_file():
                    out.append(
                        Discovered(name=nested.name, directory=nested, manifest=deeper, root=root)
                    )
        return out


class ClaudeCodeAdapter(Adapter):
    """Claude Code: user skills, project skills, and installed plugins.

    Three roots, and the third is the interesting one. Plugins are cached
    per revision under
    `~/.claude/plugins/cache/<marketplace>/<plugin>/<revision>/skills`, and
    old revisions are left in place. `installed_plugins.json` names the
    revision actually installed, so without reading it a scan reports the
    same skill several times and cannot say which one the agent loads.
    """

    agent = "claude-code"

    def capability(self) -> Capability:
        return Capability(
            agent=self.agent,
            discover=True,
            resolve_precedence=True,
            observe=False,
            install=False,
            notes=[
                "Observation is not implemented yet; usage is unknown rather than zero.",
            ],
        )

    # --- roots ---------------------------------------------------------------

    def roots(self, project_root: Path | None = None) -> list[Root]:
        """Every root to scan, narrowest scope first."""
        found: list[Root] = []
        if project_root is not None:
            found.append(
                Root(path=project_root / ".claude" / "skills", scope=PROJECT, agent=self.agent)
            )
        found.append(Root(path=_home() / ".claude" / "skills", scope=USER, agent=self.agent))
        found.extend(self._plugin_roots())
        return [r for r in found if r.status != ABSENT or r.path.parent.exists()]

    def _plugin_roots(self) -> list[Root]:
        base = _home() / ".claude" / "plugins"
        cache = base / "cache"
        if not cache.is_dir():
            return []

        live = self._installed_revisions(base)
        roots: list[Root] = []
        # cache/<marketplace>/<plugin>/<revision>/skills
        for marketplace in _subdirs(cache):
            for plugin in _subdirs(marketplace):
                for revision in _subdirs(plugin):
                    skills = revision / "skills"
                    if not skills.is_dir():
                        continue
                    key = (plugin.name, revision.name)
                    roots.append(
                        Root(
                            path=skills,
                            scope=PLUGIN,
                            agent=self.agent,
                            plugin=plugin.name,
                            revision=revision.name,
                            # Unknown means "no config to check", not "stale".
                            active=live.get(key, live == {}),
                        )
                    )

        # Only `<revision>/skills` becomes a root, never a sibling directory
        # beside it. Some plugins vendor a second copy of the same skills for
        # another harness (`<revision>/.openclaw/skills`); Claude Code does
        # not read those, so counting them would inflate the inventory with
        # packages this agent will never load.

        # marketplaces/<name>/skills — a marketplace checkout, which holds
        # skills directly rather than under a revision. Missing this layout
        # was worth two thirds of the packages on the machine this was
        # first run against, so it is not an edge case.
        for marketplace in _subdirs(base / "marketplaces"):
            for skills in (marketplace / "skills", *(d / "skills" for d in _subdirs(marketplace))):
                if skills.is_dir():
                    roots.append(
                        Root(
                            path=skills,
                            scope=PLUGIN,
                            agent=self.agent,
                            plugin=marketplace.name,
                            revision=None,
                            active=True,
                        )
                    )
        return roots

    def _installed_revisions(self, base: Path) -> dict[tuple[str, str], bool]:
        """Which (plugin, revision) pairs the agent says are installed.

        Returns an empty mapping when the file is missing or unreadable, and
        callers treat that as "cannot tell" rather than "none are active" —
        marking every plugin skill stale because one JSON file moved would
        be a worse answer than admitting the file was not found.
        """
        manifest = base / "installed_plugins.json"
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

        installed: dict[tuple[str, str], bool] = {}
        for spec, entries in (data.get("plugins") or {}).items():
            plugin = spec.split("@", 1)[0]
            for entry in entries if isinstance(entries, list) else []:
                revision = str(entry.get("version") or "")
                if revision:
                    installed[(plugin, revision)] = True
        return installed


class CodexAdapter(Adapter):
    """Codex: `.agents/skills` in the repository, your home, and /etc.

    Simpler than Claude Code in one way and harder in another. There is no
    plugin cache to reconcile, so a scan is three directories. But Codex
    does not resolve a name collision: its documentation says two skills
    sharing a name are not merged and both can appear in the selector. So
    this adapter declares `resolve_precedence=False`, and everything above
    it reports every copy as offered rather than inventing a winner Codex
    would not honour.

    Two roots are deliberately missing. Codex also reads `.agents/skills`
    from directories between where it was launched and the repository
    root, which a scan taking a project cannot see; and the skills bundled
    with Codex itself are on no documented path. Both are named in the
    capability notes rather than guessed at, because a skill this reports
    from the wrong place is worse than one it admits to missing.
    """

    agent = "codex"

    def capability(self) -> Capability:
        return Capability(
            agent=self.agent,
            discover=True,
            resolve_precedence=False,
            observe=False,
            install=False,
            notes=[
                "Codex does not merge two skills that share a name; both can be "
                "offered, so no copy is reported as shadowing another.",
                "Skills bundled with Codex are on no documented path and are not scanned.",
                "`.agents/skills` between the launch directory and the repository "
                "root is not scanned; the repository root is.",
                "Observation is not implemented for Codex; usage is unknown rather than zero.",
            ],
        )

    def roots(self, project_root: Path | None = None) -> list[Root]:
        """Every root to scan, narrowest scope first."""
        found: list[Root] = []
        if project_root is not None:
            found.append(
                Root(path=project_root / ".agents" / "skills", scope=PROJECT, agent=self.agent)
            )
        found.append(Root(path=_home() / ".agents" / "skills", scope=USER, agent=self.agent))
        # A machine-wide directory an administrator deploys. Absent on
        # Windows, and reported as absent rather than skipped: "we looked
        # and it was not there" is what the coverage list is for.
        found.append(Root(path=Path("/etc/codex/skills"), scope=ADMIN, agent=self.agent))
        return found


def _subdirs(path: Path) -> list[Path]:
    """Immediate subdirectories, sorted, tolerating an unreadable path."""
    try:
        return sorted((p for p in path.iterdir() if p.is_dir()), key=lambda p: p.name)
    except OSError:
        return []


#: The adapters this build knows about, in the order a listing shows them.
ADAPTERS: dict[str, Adapter] = {
    ClaudeCodeAdapter.agent: ClaudeCodeAdapter(),
    CodexAdapter.agent: CodexAdapter(),
}


def adapter_for(agent: str) -> Adapter | None:
    return ADAPTERS.get(agent)


def shadows(agent: str) -> bool:
    """Whether this agent picks one copy of a name and ignores the rest.

    Claude Code does; Codex offers both. The difference decides whether a
    second copy is a shadow to be resolved or simply another thing on the
    menu, so nothing above may assume one answer.
    """
    adapter = adapter_for(agent)
    return bool(adapter and adapter.capability().resolve_precedence)


def resolve_precedence(found: list[Discovered]) -> dict[str, Discovered]:
    """Which copy of each name the agent would actually load.

    Narrowest scope wins, and an inactive plugin revision never wins over
    an active one. Ties within a scope are left to the first sorted path so
    the answer is stable between runs; a genuine tie is reported as a
    conflict by the diagnostics rather than silently picked here.
    """
    ranked = sorted(
        found,
        key=lambda d: (
            SCOPE_ORDER.index(d.root.scope) if d.root.scope in SCOPE_ORDER else len(SCOPE_ORDER),
            not d.root.active,
            str(d.directory),
        ),
    )
    winners: dict[str, Discovered] = {}
    for item in ranked:
        winners.setdefault(item.name, item)
    return winners
