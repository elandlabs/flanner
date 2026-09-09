"""Reading a skill collection, and saying what is wrong with it.

The M0 half of Flanner Skills: discover packages, record what they are,
and report defects. Nothing here observes an agent, calls a model, or
changes a file an agent owns. A scan is a read.

Two ideas carry most of the design.

**A package's identity is its bytes.** Every file in the directory is
hashed into one manifest hash, so "has this changed" is a comparison
rather than a guess, and two copies of a name with the same hash are a
duplicate while two with different hashes are a conflict.

**A finding without evidence is noise.** Each one names the path it came
from and says whether it is a defect or advice. The reader is meant to be
able to check the claim, which is the same standard freshness holds
itself to.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from . import skills_adapters as adapters
from .database import SkillModel, SkillVersionModel
from .frontmatter import parse_frontmatter
from .skills_adapters import Discovered, Root

#: Files that are never part of a package's identity. Editors and OS
#: junk change without the skill changing, and hashing them would report
#: a new version every time somebody opened the folder.
IGNORED_NAMES = frozenset({".DS_Store", "Thumbs.db", "desktop.ini"})
IGNORED_DIRS = frozenset({"__pycache__", ".git", ".venv", "node_modules"})

#: Caps, so one pathological directory cannot stall a scan. A skill is
#: instructions and a few helpers; anything past these is not one.
MAX_FILES = 500
MAX_BYTES = 32 * 1024 * 1024

#: Finding severity. `defect` is checkable and actionable; `advice` is a
#: judgement the reader may disagree with. Nothing is auto-fixed.
DEFECT = "defect"
ADVICE = "advice"


@dataclass(frozen=True)
class Finding:
    """One thing worth telling somebody about their skill collection."""

    code: str
    severity: str
    skill: str
    detail: str
    #: Where to look. A finding that cannot point at a file is not one.
    evidence: str
    remedy: str = ""


@dataclass(frozen=True)
class Package:
    """One skill package as read from disk."""

    name: str
    agent: str
    scope: str
    directory: str
    manifest_hash: str
    description: str
    file_count: int
    size_bytes: int
    plugin: str | None
    revision: str | None
    active: bool
    #: True when this is the copy the agent would load for the name.
    effective: bool
    #: Frontmatter keys that were missing or unparseable.
    problems: tuple[str, ...] = ()


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# --- reading ------------------------------------------------------------------


def package_files(directory: Path) -> tuple[list[Path], int, bool]:
    """Every file that counts toward identity, its total size, and whether
    the caps were hit. Symlinks are skipped rather than followed: a link
    out of the package is not part of it, and following one is how a scan
    walks into somebody's home directory."""
    files: list[Path] = []
    total = 0
    truncated = False
    for path in sorted(directory.rglob("*")):
        if len(files) >= MAX_FILES or total >= MAX_BYTES:
            truncated = True
            break
        if path.is_symlink() or not path.is_file():
            continue
        if path.name in IGNORED_NAMES:
            continue
        if any(part in IGNORED_DIRS for part in path.relative_to(directory).parts):
            continue
        try:
            total += path.stat().st_size
        except OSError:
            continue
        files.append(path)
    return files, total, truncated


def manifest_hash(directory: Path) -> tuple[str, int, int, bool]:
    """One hash over the whole package, plus its shape.

    Relative paths go into the digest alongside contents, so moving a file
    changes the hash. Two packages hash the same only if they would behave
    the same.
    """
    files, total, truncated = package_files(directory)
    digest = hashlib.sha256()
    for path in files:
        digest.update(str(path.relative_to(directory)).replace("\\", "/").encode("utf-8"))
        digest.update(b"\0")
        try:
            digest.update(path.read_bytes())
        except OSError:
            digest.update(b"<unreadable>")
        digest.update(b"\0")
    return f"sha256:{digest.hexdigest()}", len(files), total, truncated


def read_package(found: Discovered, effective: bool) -> Package:
    """Turn a discovered directory into a package record.

    Never raises for a bad package. A skill with unreadable frontmatter is
    still a skill on disk, and reporting it is the point; refusing to
    inventory it would hide exactly the case the reader needs.
    """
    problems: list[str] = []
    description = ""
    try:
        meta, _ = parse_frontmatter(found.manifest.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError) as exc:
        meta = {}
        problems.append(f"manifest unreadable: {exc}")

    if not meta:
        problems.append("no frontmatter")
    else:
        if not str(meta.get("name") or "").strip():
            problems.append("frontmatter has no name")
        elif str(meta["name"]).strip() != found.name:
            problems.append(f"frontmatter name is {meta['name']!r}, directory is {found.name!r}")
        description = str(meta.get("description") or "").strip()
        if not description:
            problems.append("frontmatter has no description")

    digest, count, size, truncated = manifest_hash(found.directory)
    if truncated:
        problems.append("package is unusually large; hash covers only part of it")

    return Package(
        name=found.name,
        agent=found.root.agent,
        scope=found.root.scope,
        directory=str(found.directory),
        manifest_hash=digest,
        description=description,
        file_count=count,
        size_bytes=size,
        plugin=found.root.plugin,
        revision=found.root.revision,
        active=found.root.active,
        effective=effective,
        problems=tuple(problems),
    )


def scan(project_root: Path | None = None, agent: str = "claude-code") -> list[Package]:
    """Every skill package a given agent would see for this project."""
    adapter = adapters.adapter_for(agent)
    if adapter is None:
        return []
    found: list[Discovered] = []
    for root in adapter.roots(project_root):
        found.extend(adapter.discover(root))
    winners = adapters.resolve_precedence(found)
    return [
        read_package(item, effective=winners.get(item.name) is item)
        for item in sorted(found, key=lambda d: (d.name, str(d.directory)))
    ]


def roots_status(project_root: Path | None = None, agent: str = "claude-code") -> list[Root]:
    """The roots that were looked at, so a reader can see the scan's reach."""
    adapter = adapters.adapter_for(agent)
    return adapter.roots(project_root) if adapter else []


# --- diagnostics --------------------------------------------------------------


def _places(count: int) -> str:
    """ "one place" or "3 places".

    Written out because `place(s)` in a sentence a person reads is the
    tell that nobody read it back.
    """
    return "one place" if count == 1 else f"{count} places"


def diagnose(packages: list[Package]) -> list[Finding]:
    """What is wrong, and what is merely worth a look.

    Deterministic checks only. Nothing here compares meanings or guesses
    at intent: an exact duplicate is arithmetic, and a shadowed revision
    is a fact from the agent's own config.
    """
    findings: list[Finding] = []

    for pkg in packages:
        for problem in pkg.problems:
            findings.append(
                Finding(
                    code="manifest_invalid",
                    severity=DEFECT,
                    skill=pkg.name,
                    detail=problem,
                    evidence=pkg.directory,
                    remedy=(
                        "Fix SKILL.md's frontmatter; name and description are what "
                        "an agent matches on."
                    ),
                )
            )

    by_name: dict[str, list[Package]] = {}
    for pkg in packages:
        by_name.setdefault(pkg.name, []).append(pkg)

    for name, copies in sorted(by_name.items()):
        if len(copies) < 2:
            continue
        hashes = {c.manifest_hash for c in copies}
        winner = next((c for c in copies if c.effective), copies[0])
        others = [c for c in copies if c is not winner]

        if len(hashes) == 1:
            findings.append(
                Finding(
                    code="duplicate_package",
                    severity=ADVICE,
                    skill=name,
                    detail=(
                        f"{len(copies)} identical copies. The {winner.scope} copy is the one "
                        "that loads; the rest are dead weight."
                    ),
                    evidence="; ".join(c.directory for c in others),
                    remedy="Remove the copies you did not mean to keep.",
                )
            )
        else:
            # Where the copies came from decides how bad this is. Several
            # revisions of one plugin, all differing, is how an agent stores
            # a plugin it has updated — normal, and worth no more than a
            # mention. Copies from different places competing is the case
            # that costs somebody an afternoon: they edit the one they know
            # about and the agent goes on loading the other.
            origins = {(c.scope, c.plugin) for c in copies}
            findings.append(
                Finding(
                    code="shadowed_package",
                    severity=DEFECT if len(origins) > 1 else ADVICE,
                    skill=name,
                    detail=(
                        f"{len(copies)} copies differ, from {_places(len(origins))}. "
                        f"The {winner.scope} copy wins."
                    ),
                    evidence="; ".join(f"{c.scope}: {c.directory}" for c in copies),
                    remedy=(
                        "Decide which one is real. Editing a shadowed copy is the usual "
                        "cause of 'my change did nothing'."
                    ),
                )
            )

    stale = [p for p in packages if p.scope == adapters.PLUGIN and not p.active]
    for pkg in sorted(stale, key=lambda p: (p.plugin or "", p.name)):
        findings.append(
            Finding(
                code="stale_plugin_revision",
                severity=ADVICE,
                skill=pkg.name,
                detail=(
                    f"From {pkg.plugin} revision {pkg.revision}, which the agent's config does "
                    "not list as installed. It is a leftover cache entry, not a loaded skill."
                ),
                evidence=pkg.directory,
                remedy="Nothing to do unless you are short of disk; the agent already ignores it.",
            )
        )

    return findings


def summarise(packages: list[Package], findings: list[Finding]) -> dict[str, Any]:
    """The counts a reader wants before the detail."""
    effective = [p for p in packages if p.effective]
    return {
        "packages": len(packages),
        "effective": len(effective),
        "shadowed": len(packages) - len(effective),
        "by_scope": {
            scope: sum(1 for p in packages if p.scope == scope) for scope in adapters.SCOPE_ORDER
        },
        "defects": sum(1 for f in findings if f.severity == DEFECT),
        "advice": sum(1 for f in findings if f.severity == ADVICE),
        "size_bytes": sum(p.size_bytes for p in effective),
    }


def report(
    project_root: Path | None = None,
    agent: str = "claude-code",
    packages: list[Package] | None = None,
) -> dict[str, Any]:
    """One machine-readable answer for the CLI, the web UI and MCP.

    Every surface renders this, so they cannot disagree about what was
    found. `coverage` is here because a scan that could not read a root
    must not look like a collection that has nothing in it.

    `packages` lets a caller that already scanned hand the result back in.
    Scanning hashes every file in every package, so a caller that both
    records and renders would otherwise pay for that twice.
    """
    if packages is None:
        packages = scan(project_root, agent)
    findings = diagnose(packages)
    adapter = adapters.adapter_for(agent)
    capability = adapter.capability() if adapter else None
    return {
        "agent": agent,
        "project_root": str(project_root) if project_root else None,
        "scanned_at": utcnow().isoformat().replace("+00:00", "Z"),
        "coverage": {
            "roots": [
                {
                    "path": str(r.path),
                    "scope": r.scope,
                    "exists": r.path.is_dir(),
                    "plugin": r.plugin,
                    "revision": r.revision,
                    "active": r.active,
                }
                for r in roots_status(project_root, agent)
            ],
            "observation": "unsupported",
            "notes": capability.notes if capability else ["no adapter for this agent"],
        },
        "summary": summarise(packages, findings),
        "packages": [asdict(p) for p in packages],
        "findings": [asdict(f) for f in findings],
    }


def to_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, indent=2, sort_keys=False)


# --- catalog ------------------------------------------------------------------


def record(session: Session, packages: list[Package]) -> dict[str, int]:
    """Write what was scanned into the catalog.

    The catalog is an index, not the record: packages stay authoritative
    at their own paths and are never edited here. A package whose bytes
    changed gets a new version row rather than overwriting the old one, so
    "when did this change" has an answer.
    """
    added = 0
    versions = 0
    for pkg in packages:
        skill = (
            session.query(SkillModel)
            .filter_by(name=pkg.name, agent=pkg.agent, directory=pkg.directory)
            .one_or_none()
        )
        if skill is None:
            skill = SkillModel(
                name=pkg.name,
                agent=pkg.agent,
                scope=pkg.scope,
                directory=pkg.directory,
                origin=pkg.plugin or pkg.scope,
            )
            session.add(skill)
            session.flush()
            added += 1

        skill.scope = pkg.scope
        skill.effective = pkg.effective
        skill.active = pkg.active
        skill.last_seen_at = utcnow()

        latest = (
            session.query(SkillVersionModel)
            .filter_by(skill_id=skill.id)
            .order_by(SkillVersionModel.created_at.desc())
            .first()
        )
        if latest is None or latest.manifest_hash != pkg.manifest_hash:
            session.add(
                SkillVersionModel(
                    skill_id=skill.id,
                    manifest_hash=pkg.manifest_hash,
                    description=pkg.description,
                    file_count=pkg.file_count,
                    size_bytes=pkg.size_bytes,
                )
            )
            versions += 1

    session.commit()
    return {"skills_added": added, "versions_recorded": versions}
